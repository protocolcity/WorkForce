"""The runner engine — RUNNER_SPEC made software, one worker at a time.

Every MUST in ProtocolCity's docs/specs/RUNNER_SPEC.md maps to a step here:

  §2 identity/signing   -> _build_env (exactly one identity; ambient store
                           scoping vars stripped from the child env)
  §3 single-flight      -> _Lock (atomic mkdir, pid-based orphan reclaim, stale reclaim, SKIP on held)
  §4 preflight          -> _preflight (CLI present, desk/queue probe, disk)
  §5 credentials        -> _fetch_secret (dispatch-time keychain read; never
                           persisted, never logged)
  §6 dispatch           -> canonical-path law reads + sha256 pins (the law-
                           version amendment), model substitution, wall-clock
                           budget with hard kill; multi-pass loop (pass
                           ceiling / budget floor / empty queue / no-progress
                           stops) when the roster sets max_passes > 1
  §7 workspace safety   -> _predirty_snapshot (pre-dispatch dirty-file list,
                           exposed to guards via env)
  §8 evidence           -> Ledger events; exit codes 0=ran/skipped, 1=infra

Exit codes: 0 = dispatched or cleanly skipped; 1 = infra failure.
Dry-run performs every step except spawning the vendor CLI.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from typing import Dict, List, Optional, Tuple, Union

from .ledger import Ledger
from .roster import Worker

# Ambient desk-scoping vars a child must never inherit (§2 — the misrouting
# incident class). The worker's own identity is set explicitly afterwards.
SCRUBBED_ENV = ("TP_AGENT_ID", "TP_PRODUCT", "TP_DEFAULT_PRODUCT")
IDENTITY_ENV = "TP_AGENT_ID"
PREDIRTY_ENV = "WORKFORCE_PREDIRTY"
LOCK_GRACE_SECS = 600
_GHOST_AUDIT_TIMEOUT = 60

# Vendor-limit exit signatures. Case-insensitive substring match against
# the last 8 KB of the run output. Extend here as new vendors surface new phrases;
# policy (auto-bench, alerting) stays OUT of this table — roster config, not engine code.
_VENDOR_LIMIT_PATTERNS = (
    "402",
    "429",
    "quota",
    "rate limit",
    "rate-limit",
    "usage balance",
    "spending limit",
    "payment required",
    "credits or reached",  # grok team-spend class: "used all available credits or reached…"
)


class InfraError(RuntimeError):
    """Preflight or dispatch failure that a human must notice (exit 1)."""


class _Skip(Exception):
    """Clean, recoverable non-dispatch (exit 0)."""


def _pid_alive(pid: int) -> bool:
    """Return True if pid is currently running on this host."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, different owner


class _Lock:
    """Atomic per-worker mutex with pid-based orphan reclaim and stale fallback (§3).

    Lock dir contains a 'pid' file so a kill-9 orphan (empty dir or dead pid)
    is reclaimed immediately on the next acquire, without waiting for the
    stale-age timeout.
    """

    _PID_FILE = "pid"

    def __init__(self, root: str, worker: str, budget_secs: int, ledger: Ledger) -> None:
        self.path = os.path.join(root, "%s.lock" % worker)
        self.budget_secs = budget_secs
        self.ledger = ledger
        self.held = False
        os.makedirs(root, exist_ok=True)

    def _pid_path(self) -> str:
        return os.path.join(self.path, self._PID_FILE)

    def _write_pid(self) -> None:
        with open(self._pid_path(), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    def _reclaim(self, reason: str, **kw: object) -> None:
        self.ledger.append("WARN", reason=reason, **kw)
        try:
            os.unlink(self._pid_path())
        except OSError:
            pass
        os.rmdir(self.path)
        os.mkdir(self.path)
        self._write_pid()
        self.held = True

    def acquire(self) -> None:
        try:
            os.mkdir(self.path)
            self._write_pid()
            self.held = True
            return
        except FileExistsError:
            pass

        # Lock dir exists — check pid to distinguish orphan from live hold.
        try:
            with open(self._pid_path(), "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
            alive = _pid_alive(pid)
        except (OSError, ValueError):
            # No pid file or unreadable: orphan empty dir (crash/kill-9 before
            # pid write, or old-format lock from a pre-fix deployment).
            self._reclaim("orphan-no-pid")
            return

        if not alive:
            self._reclaim("orphan-pid-dead", pid=pid)
            return

        age = time.time() - os.stat(self.path).st_mtime
        if age > self.budget_secs + LOCK_GRACE_SECS:
            self._reclaim("stale-lock-reclaim", age_secs=int(age))
            return
        raise _Skip("lock held (age %ds)" % int(age))

    def release(self) -> None:
        if self.held:
            try:
                os.unlink(self._pid_path())
            except OSError:
                pass
            try:
                os.rmdir(self.path)
            except OSError:
                pass
            self.held = False


def _sha256(path: str) -> Tuple[str, str]:
    """Read a law file from its canonical path; return (text, sha256). §6."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_chain(paths: List[str]) -> List[Tuple[str, str]]:
    """Load §6 authority-chain files; return [(text, sha16), ...].

    Raises InfraError on any unreadable file — a partial chain must fail
    closed, never silently narrow the authority surface.
    """
    result = []
    for path in paths:
        try:
            result.append(_sha256(path))
        except OSError as exc:
            raise InfraError("authority chain file unreadable: %s" % exc)
    return result


def _free_mb(path: str) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize / (1024 * 1024))


def _dig(obj: object, dot_path: str) -> object:
    for key in dot_path.split("."):
        if not isinstance(obj, dict) or key not in obj:
            raise KeyError(dot_path)
        obj = obj[key]
    return obj


def _probe_queue(worker: Worker) -> Optional[int]:
    """GET the roster's queue probe; return ready count, or None if unprobed.

    Unreachable desk is an ERROR, not a skip — someone must notice (§4).
    """
    if not worker.queue_url:
        return None
    try:
        with urllib.request.urlopen(worker.queue_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise InfraError("desk unreachable at %s: %s" % (worker.queue_url, exc))
    try:
        return int(_dig(data, worker.queue_count_key))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        raise InfraError(
            "queue probe %s has no int at %r" % (worker.queue_url, worker.queue_count_key)
        )


def _preflight(worker: Worker) -> Optional[int]:
    if _free_mb(worker.workdir) < worker.min_free_mb:
        raise _Skip("low disk (<%dMB free)" % worker.min_free_mb)
    # a roster env PATH governs both this check and the spawn (subprocess
    # resolves argv[0] against the env it is given) — keep them in agreement
    if shutil.which(worker.command[0], path=worker.env.get("PATH")) is None:
        raise _Skip("CLI %r not installed" % worker.command[0])
    count = _probe_queue(worker)
    if count is not None and count <= 0:
        raise _Skip("queue empty")
    return count


def _fetch_secret(worker: Worker) -> Optional[str]:
    """§5 — dispatch-time keychain read. Absent item = fall back silently."""
    if not worker.keychain_service:
        return None
    out = subprocess.run(
        ["security", "find-generic-password", "-s", worker.keychain_service, "-w"],
        capture_output=True, text=True,
    )
    secret = out.stdout.strip()
    return secret or None


def _predirty_snapshot(worker: Worker, run_dir: str) -> Optional[str]:
    """§7 — record what was already dirty before this shift."""
    probe = subprocess.run(
        ["git", "-C", worker.workdir, "status", "--porcelain=v1"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return None  # not a git workdir; nothing to guard
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "predirty-%s.txt" % worker.name)
    lines = []
    for raw in probe.stdout.splitlines():
        entry = raw[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        lines.append(entry)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    return path


def _build_env(worker: Worker, predirty: Optional[str], secret: Optional[str],
               chain_paths: Optional[List[str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    for var in SCRUBBED_ENV:
        env.pop(var, None)
    # §7 predirty is engine-controlled per-shift state, not inheritable: a
    # nested shift (or a suite run inside a live shift — WorkForce dispatches
    # its own mechanic) must never see the parent's snapshot. Scrub both the
    # default var and the worker's custom one, then set this shift's below.
    env.pop(PREDIRTY_ENV, None)
    if worker.predirty_env:
        env.pop(worker.predirty_env, None)
    env[IDENTITY_ENV] = worker.identity
    if predirty:
        env[worker.predirty_env or PREDIRTY_ENV] = predirty
    if chain_paths:
        env["WORKFORCE_AUTHORITY_CHAIN_PATHS"] = ":".join(chain_paths)
    env.update(worker.env)
    if secret and worker.keychain_env:
        env[worker.keychain_env] = secret
    return env


def _usage_from_output(out_path: str, offset: int, fields: Dict[str, str]) -> Dict[str, Union[str, int, float]]:
    """Consumption telemetry: dig roster-configured dot-paths
    out of the pass's output. Scan the bounded tail reversed; take the last
    JSON-object line from which at least one configured path resolves (oc-37:
    some CLIs emit a non-usage terminal event after the usage-bearing one).
    Vendor specifics live in the roster's ``usage_fields`` — the engine stays
    host-neutral. Telemetry must never fail a shift: any problem returns {}
    and the DONE line simply carries no usage keys."""
    if not fields:
        return {}
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read(2 * 1024 * 1024)  # a usage blob lives near the end; bound the read
        for line in reversed(chunk.splitlines()):
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if not isinstance(cand, dict):
                continue
            out: Dict[str, Union[str, int, float]] = {}
            for key, dot_path in fields.items():
                if key in ("rc", "on_pass", "secs", "dry_run"):
                    continue  # engine-owned DONE keys; a colliding config is ignored, not fatal
                try:
                    val = _dig(cand, dot_path)
                except KeyError:
                    continue
                if isinstance(val, (int, float)) or isinstance(val, str):
                    out[key] = val
            if out:
                return out  # first (≥1 path) object wins; keep walking if this line was empty
        return {}
    except Exception:
        return {}


def _build_argv(worker: Worker, prompt_text: str, chain_text: str = "") -> List[str]:
    argv: List[str] = []
    for token in worker.command:
        if "{model}" in token:
            if not worker.model:
                # unpinned = explicit roster choice; drop the token AND its
                # paired flag — the shell idiom ${MODEL:+--model "$MODEL"}
                # drops both, and an orphaned --model eats the next flag
                # (incident: claude parsed '-p' as the model, 2026-07-14)
                if argv and argv[-1].startswith("-") and token == "{model}":
                    argv.pop()
                continue
            token = token.replace("{model}", worker.model)
        token = token.replace("{chain_text}", chain_text)
        token = token.replace("{prompt_text}", prompt_text)
        token = token.replace("{prompt_path}", worker.prompt)  # CLIs that take a file
        argv.append(token)
    return argv


def _classify_exit(out_path: str) -> str:
    """Return 'vendor limit: <trigger line>' or 'agent exit' for a non-zero rc.

    Scans the last 8 KB of the run output for known vendor-limit signals.
    Must never raise — a classification failure must not mask the real error.
    """
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            tail = fh.read()
        lower = tail.lower()
        for pattern in _VENDOR_LIMIT_PATTERNS:
            idx = lower.find(pattern)
            if idx == -1:
                continue
            start = lower.rfind("\n", 0, idx) + 1
            end = lower.find("\n", idx)
            line = tail[start:(end if end != -1 else None)].strip()
            return "vendor limit: " + line[:120]
    except Exception:
        pass
    return "agent exit"


def _scope_check(worker: Worker, ledger: Ledger) -> bool:
    """Return True if workdir is within the allowed scope; log SCOPE_DENY and return False if not.

    When scope_home is empty, enforcement is off (backward-compatible default).
    Allowed set = realpath(scope_home) + realpath(grant) for each perimeter_grants entry.
    The worker's workdir must equal or be a descendant of at least one allowed root.
    """
    if not worker.scope_home:
        return True
    home = os.path.realpath(worker.scope_home)
    allowed = [home] + [os.path.realpath(g) for g in worker.perimeter_grants]
    wd = os.path.realpath(worker.workdir)
    for root in allowed:
        if wd == root or wd.startswith(root + os.sep):
            return True
    ledger.append("SCOPE_DENY", workdir=wd, home=home)
    return False


def _run_ghost_audit(worker: Worker, env: Dict[str, str], ledger: Ledger) -> None:
    """Run the roster ghost_audit command (§8 SHOULD). Non-fatal.

    Writes a GHOST ledger event with rc + first-line summary. Nonzero rc or
    exec failure appends a WARN; never aborts the shift.
    """
    try:
        proc = subprocess.run(
            worker.ghost_audit,
            cwd=worker.workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=_GHOST_AUDIT_TIMEOUT,
        )
        combined = (proc.stdout + proc.stderr).strip()
        summary = combined.splitlines()[0][:120] if combined else ""
        ledger.append("GHOST", rc=proc.returncode, summary=summary)
        if proc.returncode != 0:
            ledger.append("WARN", reason="ghost-audit rc=%d" % proc.returncode)
    except subprocess.TimeoutExpired:
        ledger.append("GHOST", rc=-1, summary="timeout after %ds" % _GHOST_AUDIT_TIMEOUT)
        ledger.append("WARN", reason="ghost-audit timed out")
    except Exception as exc:
        ledger.append("GHOST", rc=-1, summary="exec error: %.80s" % exc)
        ledger.append("WARN", reason="ghost-audit exec error")


def dispatch(worker: Worker, local_root: str, dry_run: bool = False) -> int:
    """Run one shift for one worker. Returns the process exit code (0/1)."""
    ledger = Ledger(os.path.join(local_root, "ledger"), worker.name)
    lock = _Lock(os.path.join(local_root, "locks"), worker.name, worker.budget_secs, ledger)
    try:
        queue_count = _preflight(worker)
        if not _scope_check(worker, ledger):
            return 1
        lock.acquire()
    except _Skip as skip:
        ledger.append("SKIP", reason=str(skip))
        return 0
    except InfraError as exc:
        ledger.append("ERROR", reason=str(exc))
        return 1

    try:
        try:
            contract_text, contract_sha = _sha256(worker.contract)
            prompt_text, prompt_sha = _sha256(worker.prompt)
        except OSError as exc:
            ledger.append("ERROR", reason="law unreadable: %s" % exc)
            return 1

        # §6 authority-chain enforcement
        if worker.authority_chain_required and not worker.authority_chain:
            ledger.append("ERROR", reason="NO_AUTHORITY_CHAIN")
            return 1
        chain_entries: List[Tuple[str, str]] = []
        if worker.authority_chain:
            try:
                chain_entries = _load_chain(worker.authority_chain)
            except InfraError as exc:
                ledger.append("ERROR", reason=str(exc))
                return 1
        chain_text = "\n\n".join(t for t, _ in chain_entries)
        chain_shas = [sha for _, sha in chain_entries]

        predirty = None if dry_run else _predirty_snapshot(worker, os.path.join(local_root, "run"))
        secret = None if dry_run else _fetch_secret(worker)
        env = _build_env(worker, predirty, secret,
                         chain_paths=worker.authority_chain if chain_entries else None)
        argv = _build_argv(worker, prompt_text, chain_text=chain_text)

        if not dry_run and worker.ghost_audit:
            _run_ghost_audit(worker, env, ledger)

        chain_kwargs: Dict[str, object] = {"chain_len": len(chain_entries)}
        if chain_shas:
            chain_kwargs["chain_sha"] = ",".join(chain_shas)
        ledger.append(
            "START",
            identity=worker.identity, kind=worker.kind,
            model=worker.model or "default", budget_secs=worker.budget_secs,
            max_passes=worker.max_passes,
            queue=("?" if queue_count is None else queue_count),
            contract_sha=contract_sha, prompt_sha=prompt_sha,
            dry_run=int(dry_run),
            **chain_kwargs,
        )

        if dry_run:
            ledger.append("DONE", dry_run=1, argv_head=argv[0], argv_len=len(argv))
            return 0

        # §6 multi-pass: fresh passes while budget, ceiling, queue, and
        # progress allow. The deadline is the whole-shift budget, so the
        # lock's stale-reclaim math is untouched by pass count.
        # Worker output goes to a per-shift file (§8 log tail — an agent that
        # dies in 2s must leave its stderr behind); truncated each shift.
        out_dir = os.path.join(local_root, "run")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "%s.out" % worker.name)
        outfh = open(out_path, "w", encoding="utf-8")
        deadline = time.monotonic() + worker.budget_secs
        prev_queue = queue_count
        passes = 0
        while True:
            remain = deadline - time.monotonic()
            outfh.write("--- pass %d ---\n" % (passes + 1))
            outfh.flush()
            pass_offset = outfh.tell()  # telemetry reads this pass's slice only
            pass_t0 = time.monotonic()
            proc = subprocess.Popen(argv, cwd=worker.workdir, env=env,
                                    stdout=outfh, stderr=subprocess.STDOUT)
            try:
                rc = proc.wait(timeout=max(remain, 0.1))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                ledger.append("ERROR", reason="killed at budget",
                              budget_secs=worker.budget_secs, on_pass=passes + 1)
                return 1
            if rc != 0:
                ledger.append("ERROR", reason=_classify_exit(out_path),
                              rc=rc, on_pass=passes + 1)
                return 1
            passes += 1
            outfh.flush()
            usage = _usage_from_output(out_path, pass_offset, worker.usage_fields)
            ledger.append("DONE", rc=0, on_pass=passes,
                          secs=int(time.monotonic() - pass_t0), **usage)

            if passes >= worker.max_passes:
                ledger.append("STOP", reason=("single-pass complete" if worker.max_passes == 1
                                              else "max passes (%d)" % worker.max_passes))
                return 0
            if deadline - time.monotonic() < worker.min_pass_secs:
                ledger.append("STOP", reason="budget floor (<%ds left)" % worker.min_pass_secs)
                return 0
            try:
                now_queue = _probe_queue(worker)
            except InfraError as exc:
                # work already done this shift — a dead probe stops, not errors
                ledger.append("STOP", reason="queue unprobed between passes: %s" % exc)
                return 0
            if now_queue is None or now_queue <= 0:
                ledger.append("STOP", reason="queue empty")
                return 0
            if prev_queue is not None and now_queue >= prev_queue:
                # Multipass still stops — do not burn the budget on a flat or
                # restocked queue. Reason distinguishes true sit (count
                # unchanged) from restock (count rose: closed work + filed
                # follow-ups, peer growth, etc.). Health only wedges on
                # "no progress" so restock does not ship-cut a productive
                # default-lane coordinator (morgan 2026-07-16 class).
                if now_queue > prev_queue:
                    reason = "restocked (%s -> %s)" % (prev_queue, now_queue)
                else:
                    reason = "no progress (%s -> %s)" % (prev_queue, now_queue)
                ledger.append("STOP", reason=reason)
                return 0
            prev_queue = now_queue
    finally:
        try:
            outfh.close()
        except (NameError, OSError):
            pass  # dry-run/preflight paths never opened it
        lock.release()
