"""The runner engine — RUNNER_SPEC made software, one worker at a time.

Every MUST in ProtocolCity's docs/specs/RUNNER_SPEC.md maps to a step here:

  §2 identity/signing   -> _build_env (exactly one identity; ambient store
                           scoping vars stripped from the child env)
  §3 single-flight      -> _Lock (atomic mkdir, pid-based orphan reclaim, stale reclaim, SKIP on held)
  §4 preflight          -> _preflight (CLI present, desk/queue probe, disk)
                           + revalidate_ready_after_lock (wf-163 foreign-claim
                           guard: second ready look after the single-flight
                           lock so wake-on-route cannot spawn into a takeover)
  §5 credentials        -> _fetch_secret (dispatch-time keychain read; never
                           persisted, never logged)
  §6 dispatch           -> canonical-path law reads + sha256 pins (the law-
                           version amendment), model substitution, wall-clock
                           budget with hard kill; multi-pass drain loop
                           (wf-174: max_passes=0 budget-driven; N soft
                           ceiling; stops on empty / no-progress / budget
                           floor / fault / hard safety rail)
  §7 workspace safety   -> _predirty_snapshot (pre-dispatch dirty-file list,
                           exposed to guards via env); optional shift
                           worktree isolates hand cwd from primary
                           checkout dirty so same-file concurrent founder
                           WIP cannot enter hand commits; post-shift FF
                           merge into primary when both trees clean
  §8 evidence           -> Ledger events; exit codes 0=ran/skipped, 1=infra

ALWAYS_WORK §4 / wf-111 — empty runs are not failure. After N consecutive
queue-empty SKIPs (Worker.empty_run_threshold, default 3) the engine emits
one WARN health signal. Optional Worker.empty_run_backoff (seconds, default
0 = off) is enforced by the daemon tick path so cron does not thrash the
ledger; empty still never invents work.

Exit codes: 0 = dispatched or cleanly skipped; 1 = infra failure.
Dry-run performs every step except spawning the vendor CLI.
"""

import datetime
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple, Union

from .capacity import desk_writes_allowed
from .ledger import Ledger, parse_shifts
from .roster import Worker

# Desk probe timeouts. Engine dispatch may wait longer than board
# display probes; both share _http_get_json so sockets always close.
_PROBE_TIMEOUT_SECS = 10.0
_PROBE_RETRY_BACKOFF_SECS = 0.5

# Ambient desk-scoping vars a child must never inherit (§2 — the misrouting
# incident class). The worker's own identity is set explicitly afterwards.
SCRUBBED_ENV = ("TP_AGENT_ID", "TP_PRODUCT", "TP_DEFAULT_PRODUCT")
IDENTITY_ENV = "TP_AGENT_ID"
PREDIRTY_ENV = "WORKFORCE_PREDIRTY"
SHIFT_WORKDIR_ENV = "WORKFORCE_SHIFT_WORKDIR"
PRIMARY_WORKDIR_ENV = "WORKFORCE_PRIMARY_WORKDIR"
LOCK_GRACE_SECS = 600
_GHOST_AUDIT_TIMEOUT = 60
# wf-153 — branch namespace for engine-owned shift worktrees (not host paths)
_SHIFT_BRANCH_PREFIX = "workforce/shift/"

# wf-174 — hard safety rail when max_passes=0 (budget-driven drain). Prevents
# infinite re-spawn if a queue probe lies or a hand never shrinks the feed.
# Soft ceilings (max_passes>=1) still win when set; this only bounds drain mode.
MAX_PASSES_HARD = 50


def effective_pass_ceiling(worker: Worker) -> int:
    """Resolve the multipass stop ceiling for one worker.

    * ``max_passes == 0`` → budget-driven drain, capped at ``MAX_PASSES_HARD``
    * ``max_passes >= 1`` → that soft ceiling (1 = single-pass)
    """
    n = int(getattr(worker, "max_passes", 1) or 0)
    if n <= 0:
        return MAX_PASSES_HARD
    return n

# Vendor-limit exit signatures. Case-insensitive substring match
# against the last 8 KB of the run output. Extend here as new vendors surface new
# phrases; policy (auto-bench, alerting) stays OUT of this table — roster config,
# not engine code.
#
# Prefer multi-word phrases over bare "usage" / "limit" so JSON usage telemetry
# and ordinary prose do not false-classify as capacity.
_VENDOR_LIMIT_PATTERNS = (
    "402",
    "429",
    "quota",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "usage balance",
    "usage limit",          # Codex / ChatGPT usage-cap class
    "usage_limit",          # API / JSON error type
    "hit your usage",
    "hit your limit",       # Claude Code: "You've hit your limit · resets …"
    "you've hit your",
    "you have hit your",
    "5-hour limit",
    "weekly limit",
    "daily limit",
    "spending limit",
    "payment required",
    "credits or reached",  # grok team-spend class: "used all available credits or reached…"
    "out of credits",
    "resource_exhausted",
    "subscription access",  # Claude Code 403: "disabled Claude subscription access"
)

# Tier-2 host-mutation guard.
# Any pattern in _TIER2_MUTATION_BLOCK found in a worker's command argv (joined
# with spaces) causes dispatch to log HOST_MUTATION_DENY and return 1.
# _TIER2_MUTATION_ALLOW overrides the block — a command matching both lists is
# allowed (the test-label exception below is the canonical case).
# Document changes here AND in pc-676 §B so figaro/sylvester stay consistent.
#
# wf-159: live desk restart incident — kickstart / com.ticketingprotocol.* /
# com.workforce.* / wrapper serve|stop|restart were missing from the block
# list, so a hand shift argv that restarted the desk could clear dispatch.
# This is still dispatch-argv only. Mid-shift residual: host_audit.py
# reuses the same patterns via tier2_mutation_hit().
_TIER2_MUTATION_BLOCK = (
    re.compile(r"com\.protocolcity\."),                               # production suite labels
    re.compile(r"com\.ticketingprotocol\."),                          # live desk launchd
    re.compile(r"com\.workforce\."),                                  # live WorkForce daemon
    re.compile(r"\b(?:8799|8801|8797)\b"),                            # shared city ports
    re.compile(r"\.protocolcity(?:/|\\|\s|$)"),                       # service config dir
    re.compile(r"\bbrew\s+(?:install|uninstall)\b", re.IGNORECASE),   # city package mutations
    # launchd lifecycle — registration AND live restart/kill (kickstart gap = wl-372)
    re.compile(
        r"\blaunchctl\s+(?:bootstrap|bootout|load|unload|kickstart|"
        r"enable|disable|kill|start|stop)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\blocal/(?:roster|daemon)\.json\b"),                  # engine registry wiring
    # Wrapper service lifecycle helpers — hands stage + FOUNDER · host, never restart live
    re.compile(
        r"\b(?:tk|blueprint|worklane|workforce)\s+(?:serve|stop|restart)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bkillall\s+(?:-\w+\s+)*(?:ticketingprotocol|worklane|workforce|protocolcity)\b",
        re.IGNORECASE,
    ),
)

_TIER2_MUTATION_ALLOW = (
    re.compile(r"com\.protocolcity\.suite\.test"),  # isolated test label — always permitted
)


def tier2_mutation_hit(text: str) -> Optional[str]:
    """Return matching tier-2 block pattern string, or None if clear / allowlisted.

    Pure helper shared by dispatch argv guard and host-audit ghost scan.
    Input is free text (joined argv, comment body, run-log slice). No I/O.
    """
    if not text:
        return None
    for block_pat in _TIER2_MUTATION_BLOCK:
        if block_pat.search(text):
            for allow_pat in _TIER2_MUTATION_ALLOW:
                if allow_pat.search(text):
                    return None
            return block_pat.pattern
    return None


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


def _is_timeout_exc(exc: BaseException) -> bool:
    """True when *exc* is (or wraps) a client-side socket/HTTP timeout."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    if reason is not None and _is_timeout_exc(reason):  # type: ignore[arg-type]
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


def _http_get_json(url: str, timeout: float = _PROBE_TIMEOUT_SECS) -> dict:
    """GET *url* and parse JSON. Always closes the underlying socket.

    http(s) uses ``http.client`` with ``Connection: close`` and a ``finally``
    close so a client-side timeout cannot leave ESTABLISHED sockets piled up
    against a slow desk. Non-http schemes (``file://`` in tests)
    fall back to ``urllib.request.urlopen`` with a context manager.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8"))

    host = parts.hostname
    if not host:
        raise ValueError("url has no host: %s" % url)
    port = parts.port
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    headers = {"Accept": "application/json", "Connection": "close"}
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port or 443, timeout=timeout)  # type: ignore[arg-type]
    else:
        conn = http.client.HTTPConnection(
            host, port or 80, timeout=timeout)  # type: ignore[arg-type]
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise urllib.error.HTTPError(
                url, resp.status, resp.reason or "error", resp.msg, None)
        return json.loads(body.decode("utf-8"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _tasks_from_probe(data: dict) -> List[dict]:
    """Extract task dicts with ids from a ready-probe payload (host-neutral).

    Ready feeds commonly ship ``tasks`` (WorkLane); ``items`` is accepted as
    a synonym. Count-only probes (no task list) yield ``[]`` — CLAIM is a
    best-effort teaser, never required for dispatch.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("tasks")
    if raw is None:
        raw = data.get("items")
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for t in raw:
        if isinstance(t, dict) and str(t.get("id") or "").strip():
            out.append(t)
    return out


def _probe_ready(
    worker: Worker,
    timeout: float = _PROBE_TIMEOUT_SECS,
    retries: int = 1,
) -> Tuple[Optional[int], List[dict]]:
    """GET the roster's queue probe; return (ready count, task dicts).

    Count is required when a queue_url is set (same ERROR rules as before).
    Task list may be empty when the probe is count-only.
    """
    if not worker.queue_url:
        return None, []
    attempts = max(1, int(retries) + 1)
    last_exc: Optional[BaseException] = None
    data: dict = {}
    for attempt in range(attempts):
        try:
            data = _http_get_json(worker.queue_url, timeout=timeout)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts and _is_timeout_exc(exc):
                time.sleep(_PROBE_RETRY_BACKOFF_SECS)
                continue
            raise InfraError(
                "desk unreachable at %s: %s" % (worker.queue_url, exc)
            )
    if last_exc is not None:
        raise InfraError(
            "desk unreachable at %s: %s" % (worker.queue_url, last_exc)
        )
    try:
        count = int(_dig(data, worker.queue_count_key))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        raise InfraError(
            "queue probe %s has no int at %r" % (worker.queue_url, worker.queue_count_key)
        )
    return count, _tasks_from_probe(data)


def _probe_queue(
    worker: Worker,
    timeout: float = _PROBE_TIMEOUT_SECS,
    retries: int = 1,
) -> Optional[int]:
    """GET the roster's queue probe; return ready count, or None if unprobed.

    Unreachable desk is an ERROR, not a skip — someone must notice (§4).
    Transient timeouts retry once with a short backoff before ERROR so a
    single slow-desk poll does not flip health=err / Map gold.
    Non-timeout failures (HTTP 4xx, bad JSON shape) fail immediately.
    """
    count, _tasks = _probe_ready(worker, timeout=timeout, retries=retries)
    return count


def _record_claims(
    ledger: Ledger,
    tasks: List[dict],
    *,
    product: str = "",
    limit: int = 3,
) -> int:
    """Append CLAIM events for ready tasks handed to this shift.

    Caps at *limit* (scene bay teaser size). Returns how many CLAIMs written.
    """
    n = 0
    for t in tasks[: max(0, int(limit))]:
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        title = str(t.get("title") or "")
        if len(title) > 120:
            title = title[:117] + "..."
        kv: Dict[str, Union[str, int, float]] = {"ticket": tid}
        if title:
            kv["title"] = title
        if product:
            kv["product"] = product
        pri = t.get("priority")
        if pri is not None and str(pri) != "":
            try:
                kv["priority"] = int(pri)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                kv["priority"] = str(pri)
        ledger.append("CLAIM", **kv)
        n += 1
    return n


def queue_probe_count(worker: Worker) -> Optional[int]:
    """Probe queue_url; return count or None on no-URL / any error (fail open).

    Unlike _probe_queue, exceptions are swallowed — safe for the daemon tick
    path where a probe failure must never abort scheduling decisions. Returns
    None when no queue_url is set or when the probe raises.
    """
    if not worker.queue_url:
        return None
    try:
        return _probe_queue(worker)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Startup reconciliation — un-strand tickets after a dead shift
# ---------------------------------------------------------------------------
#
# Kill -9 (or host death) leaves: (1) lock dir with a dead/missing pid and
# (2) desk tickets stuck in_progress. Ready probes only count backlog, so
# the queue looks empty, adaptive backoff walks to the daily heartbeat, and
# the pre-shift ghost_audit never runs. Catch-22: the stranded claim is
# exactly what blocks recovery.
#
# On daemon start we clean orphan locks and release seated in_progress
# tickets whose latest Owner: marker is this hand. Host-neutral: desk base
# + product come from the worker's queue_url. Live locks are never touched.

_OWNER_MARKER_RE = re.compile(r"(?m)^Owner:\s*([^\s:(]+)")
_RECONCILE_HTTP_TIMEOUT = 8.0


def desk_origin_from_queue_url(queue_url: str) -> Optional[str]:
    """Scheme+netloc from a roster queue_url, or None if not http(s)."""
    if not queue_url:
        return None
    parts = urllib.parse.urlsplit(queue_url)
    if (parts.scheme or "").lower() not in ("http", "https"):
        return None
    if not parts.netloc:
        return None
    return "%s://%s" % (parts.scheme, parts.netloc)


def product_from_queue_url(queue_url: str) -> Optional[str]:
    """product= query param from a roster queue_url (host-neutral store id)."""
    if not queue_url:
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(queue_url).query)
    vals = qs.get("product") or qs.get("project") or []
    if not vals:
        return None
    product = (vals[0] or "").strip()
    return product or None


def lock_inspect(local_root: str, worker_name: str) -> Optional[dict]:
    """Describe a per-worker §3 lock, or None if no lock dir.

    Returns ``{path, pid, alive, orphan}``. ``orphan`` is True when the lock
    may be reclaimed (missing/unreadable pid, or dead pid). A live pid is
    never orphan — even if past budget+grace (dispatch's acquire path handles
    stale-live separately; startup must not kill a genuine shift).
    """
    path = os.path.join(local_root, "locks", "%s.lock" % worker_name)
    if not os.path.isdir(path):
        return None
    pid_path = os.path.join(path, "pid")
    try:
        with open(pid_path, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return {"path": path, "pid": None, "alive": False, "orphan": True}
    alive = _pid_alive(pid)
    return {"path": path, "pid": pid, "alive": alive, "orphan": not alive}


def clean_orphan_lock(
    local_root: str,
    worker_name: str,
    ledger: Optional[Ledger] = None,
    *,
    reason: str = "startup-reconcile-lock",
) -> bool:
    """Remove an orphan lock dir. Returns True if cleaned; False if absent/live."""
    state = lock_inspect(local_root, worker_name)
    if state is None or not state["orphan"]:
        return False
    path = state["path"]
    pid_path = os.path.join(path, "pid")
    try:
        os.unlink(pid_path)
    except OSError:
        pass
    try:
        # Best-effort: remove any stray files then the dir (pid is the only
        # expected inhabitant; old formats may leave empty dirs).
        for name in os.listdir(path):
            try:
                os.unlink(os.path.join(path, name))
            except OSError:
                pass
        os.rmdir(path)
    except OSError:
        return False
    if ledger is not None:
        kw: Dict[str, Union[str, int, float]] = {"reason": reason}
        if state.get("pid") is not None:
            kw["pid"] = int(state["pid"])  # type: ignore[arg-type]
        ledger.append("WARN", **kw)
    return True


def latest_owner_id(comments: List[dict]) -> Optional[str]:
    """Latest ``Owner: <id>`` marker in comment bodies (PROCESS §5 claim)."""
    owner: Optional[str] = None
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        body = c.get("body") or ""
        matches = _OWNER_MARKER_RE.findall(body)
        if matches:
            owner = matches[-1].strip()
    return owner or None


def _http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    timeout: float = _RECONCILE_HTTP_TIMEOUT,
) -> dict:
    """Small JSON HTTP helper for reconcile desk writes. Always closes."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    host = parts.hostname
    if not host:
        raise ValueError("url has no host: %s" % url)
    port = parts.port
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port or 443, timeout=timeout)  # type: ignore[arg-type]
    else:
        conn = http.client.HTTPConnection(
            host, port or 80, timeout=timeout)  # type: ignore[arg-type]
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict) and parsed:
                parsed.setdefault("ok", False)
                parsed.setdefault("error", "HTTP %d" % resp.status)
                return parsed
            return {"ok": False, "error": "HTTP %d: %s" % (resp.status, raw[:200])}
        return json.loads(raw) if raw else {}
    finally:
        try:
            conn.close()
        except Exception:
            pass



def _list_in_progress_ids(desk: str, product: str, worker_name: str) -> List[str]:
    """Task ids with status=in_progress and label worker:<name> on *product*."""
    label = "worker:%s" % worker_name
    q = urllib.parse.urlencode({
        "product": product,
        "label": label,
        "status": "in_progress",
        "limit": 50,
    })
    data = _http_json("GET", "%s/api/admin/tasks?%s" % (desk.rstrip("/"), q))
    tasks = data.get("tasks") or data.get("items") or []
    out: List[str] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        labs = [str(x) for x in (t.get("labels") or [])]
        if label not in labs:
            continue
        if str(t.get("status") or "").lower() != "in_progress":
            continue
        out.append(tid)
    return out


def _fetch_task(desk: str, product: str, task_id: str) -> Optional[dict]:
    q = urllib.parse.urlencode({"product": product})
    data = _http_json(
        "GET",
        "%s/api/admin/tasks/%s?%s"
        % (desk.rstrip("/"), urllib.parse.quote(task_id, safe=""), q),
    )
    task = data.get("task") if isinstance(data, dict) else None
    if isinstance(task, dict):
        return task
    if isinstance(data, dict) and data.get("id"):
        return data
    return None


def _release_stranded_ticket(
    desk: str,
    product: str,
    task_id: str,
    worker_name: str,
    author: str,
    *,
    dry_run: bool = False,
    source: str = "startup",
) -> dict:
    """Post Blocked:+Next step: (auto → backlog) for a stranded claim."""
    if source == "heartbeat":
        reason = (
            "Heartbeat reconciliation — empty ready probe while "
            "in_progress sat stranded after a dead shift (no live lock); "
            "ready feed could not see the claim (ghost-audit catch-22)"
        )
    else:
        reason = (
            "Startup reconciliation — shift lock orphaned (dead or missing "
            "pid after daemon/shift death); in_progress was invisible to the ready feed"
        )
    next_step = (
        "Released to backlog on the %s seat so the queue probe can see it and "
        "a fresh shift can claim" % worker_name
    )
    body = "Blocked: %s\nNext step: %s" % (reason, next_step)
    receipt: dict = {
        "ok": True,
        "task_id": task_id,
        "action": "would_release",
        "author": author,
    }
    if dry_run or not desk_writes_allowed():
        if not dry_run:
            receipt["hermetic"] = True
        receipt["body"] = body
        return receipt
    q = urllib.parse.urlencode({"product": product})
    url = "%s/api/admin/tasks/%s/comments?%s" % (
        desk.rstrip("/"),
        urllib.parse.quote(task_id, safe=""),
        q,
    )
    out = _http_json(
        "POST",
        url,
        {"body": body, "author": author},
    )
    receipt["api"] = out
    # Lifecycle should auto-move to backlog; if comment landed but status
    # stuck, force PATCH as belt-and-braces (desk versions without lifecycle).
    if out.get("ok") is False or out.get("error"):
        receipt["ok"] = False
        receipt["action"] = "comment_failed"
        receipt["error"] = out.get("error") or out
        return receipt
    receipt["action"] = "released"
    return receipt


def reconcile_dead_shift(
    worker: Worker,
    local_root: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    source: str = "startup",
) -> dict:
    """Clean an orphan lock and release this hand's stranded in_progress tickets.

    Runs only when the §3 lock is orphaned, or when *force* is True (prior
    heartbeat listed the worker in_flight and no live lock remains). Never
    acts on a live lock (genuine shift protection).

    Desk writes derive origin+product from ``worker.queue_url``. Missing or
    non-http queue_url → lock cleanup only. Returns a receipt dict.

    *source* tags ledger reasons and the desk Blocked: body
    (``startup`` | ``heartbeat``).
    """
    if source not in ("startup", "heartbeat"):
        source = "startup"
    lock_reason = "%s-reconcile-lock" % source
    release_reason = "%s-reconcile-release" % source
    ledger = Ledger(os.path.join(local_root, "ledger"), worker.name)
    receipt: dict = {
        "worker": worker.name,
        "lock_cleaned": False,
        "released": [],
        "skipped": [],
        "errors": [],
        "acted": False,
        "source": source,
    }
    state = lock_inspect(local_root, worker.name)
    if state is not None and not state["orphan"]:
        receipt["skipped"].append("lock-live")
        return receipt
    if state is None and not force:
        receipt["skipped"].append("no-orphan-lock")
        return receipt

    if state is not None and state["orphan"]:
        if dry_run:
            receipt["lock_cleaned"] = True  # would clean
            receipt["acted"] = True
        else:
            receipt["lock_cleaned"] = clean_orphan_lock(
                local_root, worker.name, ledger=ledger, reason=lock_reason,
            )
            if receipt["lock_cleaned"]:
                receipt["acted"] = True

    desk = desk_origin_from_queue_url(worker.queue_url or "")
    product = product_from_queue_url(worker.queue_url or "")
    if not desk or not product:
        if receipt["lock_cleaned"] or force:
            receipt["skipped"].append("no-desk-from-queue-url")
        return receipt

    # Only lanes claim; jobs never seat worker:<id> tickets.
    if getattr(worker, "kind", "lane") != "lane":
        receipt["skipped"].append("not-lane")
        return receipt

    try:
        task_ids = _list_in_progress_ids(desk, product, worker.name)
    except Exception as exc:
        receipt["errors"].append("list: %s" % exc)
        return receipt

    identity = (worker.identity or worker.name).strip()
    for tid in task_ids:
        try:
            task = _fetch_task(desk, product, tid)
        except Exception as exc:
            receipt["errors"].append("%s fetch: %s" % (tid, exc))
            continue
        if not task:
            receipt["skipped"].append("%s: missing" % tid)
            continue
        if str(task.get("status") or "").lower() != "in_progress":
            receipt["skipped"].append("%s: not-in-progress" % tid)
            continue
        owner = latest_owner_id(task.get("comments") or [])
        if not owner:
            receipt["skipped"].append("%s: no-owner-marker" % tid)
            continue
        # Match identity or name (Owner: salem vs identity salem).
        if owner not in (identity, worker.name):
            receipt["skipped"].append("%s: owner=%s" % (tid, owner))
            continue
        try:
            rel = _release_stranded_ticket(
                desk, product, tid, worker.name, identity,
                dry_run=dry_run, source=source,
            )
        except Exception as exc:
            receipt["errors"].append("%s release: %s" % (tid, exc))
            continue
        if rel.get("ok"):
            receipt["released"].append(tid)
            receipt["acted"] = True
            if not dry_run and not rel.get("hermetic"):
                ledger.append(
                    "WARN",
                    reason=release_reason,
                    task=tid,
                )
        else:
            receipt["errors"].append(
                "%s: %s" % (tid, rel.get("error") or rel.get("action"))
            )
    return receipt


def heartbeat_reconcile(
    worker: Worker,
    local_root: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Empty-probe belt-and-braces.

    Pre-shift ``ghost_audit`` only runs after a non-empty ready probe and lock
    acquire. A killed shift can leave ``in_progress`` with no live lock; the
    ready probe stays 0 and adaptive backoff walks to the daily heartbeat —
    so the audit never runs. Catch-22.

    On an empty (or suppressed) probe path the daemon/engine call this: if the
    §3 lock is live, no-op; otherwise force the same release path as startup
    (orphan lock clean + Owner-matched strand release). Host-neutral via
    ``queue_url``.
    """
    state = lock_inspect(local_root, worker.name)
    if state is not None and not state.get("orphan"):
        return {
            "worker": worker.name,
            "lock_cleaned": False,
            "released": [],
            "skipped": ["lock-live"],
            "errors": [],
            "acted": False,
            "source": "heartbeat",
        }
    # force=True: lock absent (already wiped) still releases stranded claims.
    return reconcile_dead_shift(
        worker, local_root, force=True, dry_run=dry_run, source="heartbeat",
    )


def startup_reconcile(
    workers: Dict[str, Worker],
    local_root: str,
    *,
    prior_in_flight: Optional[List[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Daemon-start pass: reclaim orphan locks + release stranded claims.

    Candidates: every roster worker with an orphan lock, plus names from the
    previous heartbeat's ``in_flight`` list that have no live lock (force
    path — covers the case where the lock dir was already wiped but the
    desk claim remains).
    """
    prior = set(prior_in_flight or [])
    report: dict = {
        "workers": [],
        "lock_cleaned": 0,
        "released": [],
        "errors": [],
    }
    # Union of known workers and lock-dir basenames that match roster names
    names: List[str] = sorted(set(workers) | prior)
    locks_root = os.path.join(local_root, "locks")
    if os.path.isdir(locks_root):
        for entry in os.listdir(locks_root):
            if entry.endswith(".lock"):
                names.append(entry[: -len(".lock")])
    names = sorted(set(names))

    for name in names:
        worker = workers.get(name)
        if worker is None:
            # Orphan lock for a de-hired name: clean lock only, no desk.
            if clean_orphan_lock(local_root, name) and not dry_run:
                report["lock_cleaned"] += 1
            continue
        state = lock_inspect(local_root, name)
        force = name in prior and (state is None or state.get("orphan"))
        if state is not None and not state.get("orphan") and not force:
            continue
        if state is None and not force:
            continue
        receipt = reconcile_dead_shift(
            worker, local_root, force=force, dry_run=dry_run,
        )
        report["workers"].append(receipt)
        if receipt.get("lock_cleaned"):
            report["lock_cleaned"] += 1
        report["released"].extend(receipt.get("released") or [])
        report["errors"].extend(receipt.get("errors") or [])
    return report


def _preflight(worker: Worker) -> Tuple[Optional[int], List[dict]]:
    """Pre-dispatch checks. Returns (ready count, ready task dicts)."""
    if _free_mb(worker.workdir) < worker.min_free_mb:
        raise _Skip("low disk (<%dMB free)" % worker.min_free_mb)
    # a roster env PATH governs both this check and the spawn (subprocess
    # resolves argv[0] against the env it is given) — keep them in agreement
    if shutil.which(worker.command[0], path=worker.env.get("PATH")) is None:
        raise _Skip("CLI %r not installed" % worker.command[0])
    count, tasks = _probe_ready(worker)
    if count is not None and count <= 0:
        raise _Skip("queue empty")
    return count, tasks


def _task_still_claimable(
    task: dict,
    identity: str,
) -> Tuple[bool, str]:
    """Whether a live task is still fair game for this hand.

    Policy (option a — no-steal baseline): only ``backlog`` is claimable.
    ``in_progress`` / ``in_review`` with another author's Owner marker, or
    any non-backlog status, is dropped so a wake-on-route shift never takes
    over a live claim. Self-owned stranded in_progress is also not a ready
    handoff (ghost-audit / reconcile owns that path).
    """
    status = str(task.get("status") or "").lower().strip()
    if status in ("", "backlog"):
        return True, ""
    comments = task.get("comments") if isinstance(task.get("comments"), list) else []
    owner = latest_owner_id(comments)  # type: ignore[arg-type]
    owner_l = (owner or "").strip().lower()
    self_l = (identity or "").strip().lower()
    if owner_l and self_l and owner_l == self_l:
        return False, "status=%s owner=self" % status
    if owner_l:
        return False, "status=%s owner=%s" % (status, owner_l)
    return False, "status=%s" % status


def revalidate_ready_after_lock(
    worker: Worker,
    queue_count: Optional[int],
    ready_tasks: List[dict],
    ledger: Ledger,
) -> Tuple[Optional[int], List[dict]]:
    """Second ready look after the single-flight lock.

    Wake-on-route (and clock fire) both enter ``dispatch``: preflight can see
    a backlog ticket, then another owner claims before spawn. Re-probe the
    same ``queue_url``; if the ready feed drained to zero, raise ``_Skip``
    ("queue empty") so the hand never starts. When desk origin+product are
    known and the probe listed task ids, re-fetch each and drop non-backlog
    / foreign-Owner tickets (belt-and-braces for a misconfigured feed).

    Probe or per-task fetch failures fail open (WARN + keep prior set) so a
    flaky desk does not invent empty-queue SKIPs after a green first probe.
    """
    tasks = list(ready_tasks or [])
    count = queue_count

    if worker.queue_url:
        try:
            new_count, new_tasks = _probe_ready(worker)
        except InfraError as exc:
            ledger.append(
                "WARN",
                reason="ready-revalidate failed",
                detail=str(exc)[:120],
            )
        else:
            if new_count is not None and new_count <= 0:
                raise _Skip("queue empty")
            count = new_count
            if new_tasks:
                tasks = new_tasks

    desk = desk_origin_from_queue_url(worker.queue_url or "")
    product = product_from_queue_url(worker.queue_url or "")
    if not (desk and product and tasks):
        return count, tasks

    identity = (worker.identity or worker.name or "").strip()
    kept: List[dict] = []
    dropped = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        try:
            live = _fetch_task(desk, product, tid)
        except Exception as exc:
            ledger.append(
                "WARN",
                reason="ready-revalidate task fetch failed",
                ticket=tid,
                detail=str(exc)[:80],
            )
            kept.append(t)
            continue
        if live is None:
            kept.append(t)
            continue
        ok, detail = _task_still_claimable(live, identity)
        if ok:
            kept.append(t)
            continue
        dropped += 1
        reason = (
            "skip-foreign-claim"
            if "owner=" in detail and "owner=self" not in detail
            else "skip-not-ready"
        )
        ledger.append("WARN", reason=reason, ticket=tid, detail=detail)

    if dropped and not kept:
        raise _Skip("foreign claim")
    if kept:
        tasks = kept
    return count, tasks


def fires_on_local_day(
    local_root: str,
    worker_name: str,
    now: Optional[datetime.datetime] = None,
) -> int:
    """Count START events whose host-local calendar day matches ``now``.

    Ledger stamps are UTC; the day boundary is host local wall (same seam as
    cron matching — ``schedule.host_wall``). Each START is one armed fire
    (DONE alone is not required so hung/crashed shifts still count toward the
    daily ceiling). ``max_fires_per_day`` compares this count to the roster pin.
    """
    from .schedule import host_wall

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    target_day = host_wall(now).date()
    path = os.path.join(local_root, "ledger", "%s.log" % worker_name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) < 2 or parts[1] != "START":
            continue
        ts = parts[0]
        try:
            utc = datetime.datetime.strptime(
                ts, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if host_wall(utc).date() == target_day:
            count += 1
    return count


def empty_run_streak(local_root: str, worker_name: str, limit: int = 50,
                     since_ts: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """Count consecutive queue-empty SKIPs (newest first).

    Returns ``(streak, newest_empty_ts)``. WARN/GHOST ledger rows do not
    break the streak (health signals may sit between empties). Any real
    shift outcome or non-empty SKIP resets the streak.

    ``since_ts`` (ISO UTC ``...Z``, same shape as ledger stamps) floors the
    count: empties at or before it are ignored. The daemon passes its last
    wake stamp so a wake resets the lane to base cadence.

    Used by dispatch (Nth empty → WARN) and the daemon empty-run backoff.
    """
    path = os.path.join(local_root, "ledger", "%s.log" % worker_name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return 0, None
    streak = 0
    last_ts: Optional[str] = None
    for s in parse_shifts(text, limit=limit):
        outcome = s.get("outcome") or ""
        reason = s.get("reason") or ""
        if outcome == "skip" and reason == "queue empty":
            ts = s.get("end_ts") or s.get("ts") or None
            if since_ts and ts and ts <= since_ts:
                break  # older than the wake floor — streak restarts there
            streak += 1
            if last_ts is None:
                last_ts = ts
            continue
        if outcome in ("warn", "ghost"):
            continue
        break
    return streak, last_ts


def _note_empty_run(worker: Worker, local_root: str, ledger: Ledger) -> None:
    """ALWAYS_WORK §4 — one WARN on the Nth consecutive queue-empty SKIP.

    Empty stays non-failure (caller already logged SKIP / exit 0). Threshold
    is roster-configurable; default 3. Subsequent empties past N do not
    re-WARN until a real shift resets the streak.
    """
    threshold = max(1, int(getattr(worker, "empty_run_threshold", 3) or 3))
    streak, _ = empty_run_streak(local_root, worker.name)
    if streak == threshold:
        ledger.append(
            "WARN",
            reason="empty-run threshold (%d consecutive queue empty)" % threshold,
            streak=streak,
        )


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


def _predirty_snapshot(worker: Worker, run_dir: str,
                       git_cwd: Optional[str] = None) -> Optional[str]:
    """§7 — record what was already dirty before this shift.

    ``git_cwd`` defaults to ``worker.workdir``; with shift worktrees
    pass the prepared worktree so the snapshot matches the spawn cwd.
    """
    root = git_cwd or worker.workdir
    probe = subprocess.run(
        ["git", "-C", root, "status", "--porcelain=v1"],
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


def _git(cwd: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run git -C cwd … — host-neutral helper (no hard-coded paths)."""
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, check=check,
    )


def _is_git_workdir(path: str) -> bool:
    probe = _git(path, "rev-parse", "--is-inside-work-tree")
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _shift_worktree_path(local_root: str, worker_name: str) -> str:
    return os.path.join(local_root, "worktrees", worker_name)


def _shift_branch_name(worker_name: str) -> str:
    return _SHIFT_BRANCH_PREFIX + worker_name


def _prepare_shift_workdir(
    worker: Worker, local_root: str, ledger: Ledger, dry_run: bool,
) -> str:
    """wf-153 — optional per-shift git worktree so hand cwd ≠ primary dirty tree.

    Returns the cwd for the vendor CLI. Default / non-git / dry-run without
    create: primary ``worker.workdir``. Mutates disk only when
    ``shift_worktree`` is on, not dry-run, and workdir is a git checkout.
    """
    if not getattr(worker, "shift_worktree", False):
        return worker.workdir
    if not _is_git_workdir(worker.workdir):
        ledger.append(
            "WARN",
            reason="shift_worktree set but workdir is not a git checkout; using workdir",
        )
        return worker.workdir

    wt_path = _shift_worktree_path(local_root, worker.name)
    branch = _shift_branch_name(worker.name)
    if dry_run:
        # Intent only — dry-run must not create worktrees or touch refs.
        return wt_path if os.path.isdir(wt_path) else worker.workdir

    head = _git(worker.workdir, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        ledger.append(
            "WARN",
            reason="shift_worktree: cannot read HEAD; using workdir",
        )
        return worker.workdir
    main_head = head.stdout.strip()

    if not os.path.isdir(wt_path):
        parent = os.path.dirname(wt_path)
        os.makedirs(parent, exist_ok=True)
        # -B: create or reset the shift branch to primary HEAD at first attach.
        add = _git(
            worker.workdir, "worktree", "add", "-B", branch, wt_path, main_head,
        )
        if add.returncode != 0:
            err = (add.stderr or add.stdout or "worktree add failed").strip()
            ledger.append(
                "WARN",
                reason="shift_worktree create failed: %.120s; using workdir" % err,
            )
            return worker.workdir
        return wt_path

    if not _is_git_workdir(wt_path):
        ledger.append(
            "WARN",
            reason="shift_worktree path exists but is not a git worktree; using workdir",
        )
        return worker.workdir

    porcelain = _git(wt_path, "status", "--porcelain=v1")
    dirty = bool((porcelain.stdout or "").strip()) if porcelain.returncode == 0 else True
    if dirty:
        # Crash recovery / in-flight hand WIP — never reset.
        ledger.append("WARN", reason="shift worktree dirty; preserving for rescue")
        return wt_path

    # Clean tree: bring primary HEAD into the shift branch without dropping
    # unmerged shift commits (ff-only no-ops when we are strictly ahead).
    ff = _git(wt_path, "merge", "--ff-only", main_head)
    if ff.returncode != 0:
        merge = _git(wt_path, "merge", "--no-edit", main_head)
        if merge.returncode != 0:
            _git(wt_path, "merge", "--abort")
            ledger.append(
                "WARN",
                reason="shift worktree could not merge primary HEAD; using as-is",
            )
    return wt_path


def _worktree_dirty(cwd: str) -> bool:
    """True when porcelain status is non-empty or git status fails."""
    porcelain = _git(cwd, "status", "--porcelain=v1")
    if porcelain.returncode != 0:
        return True
    return bool((porcelain.stdout or "").strip())


def _finalize_shift_workdir(
    worker: Worker, shift_cwd: str, ledger: Ledger,
) -> Dict[str, Union[str, int]]:
    """wf-153 slice 3 — after a successful shift, FF-merge shift → primary.

    When both trees are clean and primary HEAD is an ancestor of the shift
    HEAD, fast-forward primary to the shift tip. Never force, never reset.
    Dirt or non-ff history → WARN and leave the shift branch for rescue.
    Does not push (PROCESS §5.1.3 land-on-main stays the hand's job).

    Returns optional ledger kvs to attach to the shift's STOP line when an
    FF landed (empty dict otherwise). Failures log WARN only.
    """
    empty: Dict[str, Union[str, int]] = {}
    if not getattr(worker, "shift_worktree", False):
        return empty
    if not shift_cwd:
        return empty
    try:
        same = os.path.realpath(shift_cwd) == os.path.realpath(worker.workdir)
    except OSError:
        same = shift_cwd == worker.workdir
    if same:
        return empty  # isolation fell back to primary cwd — nothing to land
    if not _is_git_workdir(shift_cwd) or not _is_git_workdir(worker.workdir):
        return empty

    if _worktree_dirty(shift_cwd):
        ledger.append(
            "WARN",
            reason="shift finalize: shift dirty; leave branch for rescue",
        )
        return empty
    if _worktree_dirty(worker.workdir):
        ledger.append(
            "WARN",
            reason="shift finalize: primary dirty; leave branch for rescue",
        )
        return empty

    shift_head = _git(shift_cwd, "rev-parse", "HEAD")
    if shift_head.returncode != 0 or not (shift_head.stdout or "").strip():
        ledger.append("WARN", reason="shift finalize: cannot read shift HEAD")
        return empty
    shift_sha = shift_head.stdout.strip()

    primary_head = _git(worker.workdir, "rev-parse", "HEAD")
    if primary_head.returncode != 0 or not (primary_head.stdout or "").strip():
        ledger.append("WARN", reason="shift finalize: cannot read primary HEAD")
        return empty
    primary_sha = primary_head.stdout.strip()
    if primary_sha == shift_sha:
        return empty  # no commits on shift (or already landed)

    # primary must be ancestor of shift for a pure FF.
    anc = _git(
        worker.workdir, "merge-base", "--is-ancestor", primary_sha, shift_sha,
    )
    if anc.returncode != 0:
        ledger.append(
            "WARN",
            reason="shift finalize: not ff-able onto primary; leave branch for rescue",
        )
        return empty

    merge = _git(worker.workdir, "merge", "--ff-only", shift_sha)
    if merge.returncode != 0:
        err = (merge.stderr or merge.stdout or "ff-only failed").strip()
        ledger.append(
            "WARN",
            reason="shift finalize: ff-only merge failed: %.120s" % err,
        )
        return empty
    return {
        "shift_ff": 1,
        "shift_head": shift_sha[:12],
        "primary_was": primary_sha[:12],
    }


def _build_env(worker: Worker, predirty: Optional[str], secret: Optional[str],
               chain_paths: Optional[List[str]] = None,
               shift_workdir: Optional[str] = None) -> Dict[str, str]:
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
    env.pop(SHIFT_WORKDIR_ENV, None)
    env.pop(PRIMARY_WORKDIR_ENV, None)
    env[IDENTITY_ENV] = worker.identity
    if predirty:
        env[worker.predirty_env or PREDIRTY_ENV] = predirty
    if chain_paths:
        env["WORKFORCE_AUTHORITY_CHAIN_PATHS"] = ":".join(chain_paths)
    env.update(worker.env)
    # Engine-owned workdir pointers win over roster env.
    env[PRIMARY_WORKDIR_ENV] = worker.workdir
    if shift_workdir:
        env[SHIFT_WORKDIR_ENV] = shift_workdir
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


def _build_fallback_argv(worker: Worker, prompt_text: str, chain_text: str = "") -> List[str]:
    """Thin wrapper: _build_argv() with fallback_runtime and fallback_model."""
    return _build_argv(worker, prompt_text, chain_text,
                       runtime=worker.fallback_runtime, model=worker.fallback_model)


def _build_argv(worker: Worker, prompt_text: str, chain_text: str = "",
                runtime: str = "", model: Optional[str] = None) -> List[str]:
    use_runtime = runtime or worker.command[0]
    use_model = worker.model if model is None else model
    argv: List[str] = []
    for i, token in enumerate(worker.command):
        if i == 0:
            argv.append(use_runtime)
            continue
        if "{model}" in token:
            if not use_model:
                # unpinned = explicit roster choice; drop the token AND its
                # paired flag — the shell idiom ${MODEL:+--model "$MODEL"}
                # drops both, and an orphaned --model eats the next flag
                # (incident: claude parsed '-p' as the model, 2026-07-14)
                if argv and argv[-1].startswith("-") and token == "{model}":
                    argv.pop()
                continue
            token = token.replace("{model}", use_model)
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


def _host_mutation_check(worker: Worker, ledger: Ledger) -> bool:
    """Return True if command argv is clear of tier-2 host-mutation patterns.

    Inspects the worker's command list as a joined string via
    tier2_mutation_hit (shared with host-audit, wf-160). Logs
    HOST_MUTATION_DENY and returns False on deny.
    """
    if not worker.command:
        return True
    argv_str = " ".join(worker.command)
    pattern = tier2_mutation_hit(argv_str)
    if pattern is None:
        return True
    ledger.append(
        "HOST_MUTATION_DENY",
        pattern=pattern,
        argv_head=worker.command[0],
        gate="stage + file FOUNDER · host; do not execute",
    )
    return False


def dispatch(worker: Worker, local_root: str, dry_run: bool = False) -> int:
    """Run one shift for one worker. Returns the process exit code (0/1)."""
    ledger = Ledger(os.path.join(local_root, "ledger"), worker.name)
    lock = _Lock(os.path.join(local_root, "locks"), worker.name, worker.budget_secs, ledger)
    ready_tasks: List[dict] = []
    try:
        # Host-mutation / scope before preflight: a tier-2 argv must
        # HOST_MUTATION_DENY even when the CLI is missing or the queue is empty
        # — SKIP "CLI not installed" must not mask a live-service restart.
        if not _host_mutation_check(worker, ledger):
            return 1
        if not _scope_check(worker, ledger):
            return 1
        queue_count, ready_tasks = _preflight(worker)
        lock.acquire()
    except _Skip as skip:
        reason = str(skip)
        ledger.append("SKIP", reason=reason)
        if reason == "queue empty":
            _note_empty_run(worker, local_root, ledger)
            # wf-155: empty ready probe still reconciles stranded in_progress
            # (pre-shift ghost_audit never runs on this path — catch-22).
            # Honor dry_run so `dispatch --dry-run` never mutates the desk.
            try:
                heartbeat_reconcile(worker, local_root, dry_run=dry_run)
            except Exception as exc:
                ledger.append(
                    "WARN", reason="heartbeat-reconcile-error",
                    detail=str(exc)[:120],
                )
        return 0
    except InfraError as exc:
        ledger.append("ERROR", reason=str(exc))
        return 1

    try:
        # wf-163: re-check ready after lock (wake-on-route TOCTOU — pc-1111).
        # Preflight saw work; another owner may have claimed before spawn.
        try:
            queue_count, ready_tasks = revalidate_ready_after_lock(
                worker, queue_count, ready_tasks, ledger,
            )
        except _Skip as skip:
            reason = str(skip)
            ledger.append("SKIP", reason=reason)
            if reason == "queue empty":
                _note_empty_run(worker, local_root, ledger)
                try:
                    heartbeat_reconcile(worker, local_root, dry_run=dry_run)
                except Exception as exc:
                    ledger.append(
                        "WARN", reason="heartbeat-reconcile-error",
                        detail=str(exc)[:120],
                    )
            return 0

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

        # wf-153 — prepare shift cwd before predirty so §7 matches spawn cwd
        shift_cwd = _prepare_shift_workdir(worker, local_root, ledger, dry_run)
        predirty = None if dry_run else _predirty_snapshot(
            worker, os.path.join(local_root, "run"), git_cwd=shift_cwd,
        )
        secret = None if dry_run else _fetch_secret(worker)
        env = _build_env(
            worker, predirty, secret,
            chain_paths=worker.authority_chain if chain_entries else None,
            shift_workdir=shift_cwd,
        )
        argv = _build_argv(worker, prompt_text, chain_text=chain_text)

        if not dry_run and worker.ghost_audit:
            _run_ghost_audit(worker, env, ledger)

        chain_kwargs: Dict[str, object] = {"chain_len": len(chain_entries)}
        if chain_shas:
            chain_kwargs["chain_sha"] = ",".join(chain_shas)
        if getattr(worker, "shift_worktree", False):
            chain_kwargs["shift_worktree"] = 1
            chain_kwargs["shift_cwd"] = shift_cwd
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

        # wf-158 — engine-owned claim truth: record ready work orders this
        # shift was handed (wake-on-route and clock fire both use dispatch).
        # Cleared by STOP/ERROR/dry-run DONE via open_claims window close.
        if ready_tasks:
            _record_claims(
                ledger, ready_tasks,
                product=product_from_queue_url(worker.queue_url) or "",
            )

        if dry_run:
            ledger.append("DONE", dry_run=1, argv_head=argv[0], argv_len=len(argv))
            return 0

        def _stop_ok(reason: str) -> int:
            """Terminal STOP after a successful shift; attempt shift FF land."""
            ff_kv = _finalize_shift_workdir(worker, shift_cwd, ledger)
            ledger.append("STOP", reason=reason, **ff_kv)
            return 0

        # §6 multi-pass drain loop: re-spawn while budget, ceiling,
        # queue, and progress allow. Founder rule: after a successful slice,
        # re-check the seat ready feed and take the next ticket until empty /
        # gated (ready count 0) / budget floor / fault / soft or hard ceiling.
        # The deadline is the whole-shift budget, so the lock's stale-reclaim
        # math is untouched by pass count.
        # Worker output goes to a per-shift file (§8 log tail — an agent that
        # dies in 2s must leave its stderr behind); truncated each shift.
        out_dir = os.path.join(local_root, "run")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "%s.out" % worker.name)
        outfh = open(out_path, "w", encoding="utf-8")
        deadline = time.monotonic() + worker.budget_secs
        prev_queue = queue_count
        passes = 0
        pass_ceiling = effective_pass_ceiling(worker)
        while True:
            remain = deadline - time.monotonic()
            outfh.write("--- pass %d ---\n" % (passes + 1))
            outfh.flush()
            pass_offset = outfh.tell()  # telemetry reads this pass's slice only
            pass_t0 = time.monotonic()
            proc = subprocess.Popen(argv, cwd=shift_cwd, env=env,
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
                reason = _classify_exit(out_path)
                if worker.fallback_runtime and reason.startswith("vendor limit:"):
                    ledger.append("WARN", reason="quota-fallback",
                                  primary=os.path.basename(worker.command[0]),
                                  fallback=worker.fallback_runtime)
                    fallback_argv = _build_fallback_argv(worker, prompt_text, chain_text)
                    outfh.write("--- fallback: %s ---\n" % worker.fallback_runtime)
                    outfh.flush()
                    fb_pass_offset = outfh.tell()
                    fb_t0 = time.monotonic()
                    fb_proc = subprocess.Popen(fallback_argv, cwd=shift_cwd, env=env,
                                               stdout=outfh, stderr=subprocess.STDOUT)
                    try:
                        fb_rc = fb_proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
                    except subprocess.TimeoutExpired:
                        fb_proc.kill()
                        fb_proc.wait()
                        ledger.append("ERROR", reason="killed at budget (fallback)",
                                      budget_secs=worker.budget_secs)
                        return 1
                    if fb_rc != 0:
                        ledger.append("ERROR", reason=_classify_exit(out_path),
                                      rc=fb_rc, on_pass=passes + 1,
                                      fallback_runtime=worker.fallback_runtime)
                        return 1
                    passes += 1
                    outfh.flush()
                    usage = _usage_from_output(out_path, fb_pass_offset, worker.usage_fields)
                    ledger.append("DONE", rc=0, on_pass=passes,
                                  secs=int(time.monotonic() - fb_t0),
                                  fallback_runtime=worker.fallback_runtime, **usage)
                    return _stop_ok(
                        "fallback complete (%s)" % worker.fallback_runtime,
                    )
                ledger.append("ERROR", reason=reason, rc=rc, on_pass=passes + 1)
                return 1
            passes += 1
            outfh.flush()
            usage = _usage_from_output(out_path, pass_offset, worker.usage_fields)
            ledger.append("DONE", rc=0, on_pass=passes,
                          secs=int(time.monotonic() - pass_t0), **usage)

            if passes >= pass_ceiling:
                if worker.max_passes == 0:
                    return _stop_ok(
                        "drain hard cap (%d passes)" % pass_ceiling,
                    )
                return _stop_ok(
                    "single-pass complete" if worker.max_passes == 1
                    else "max passes (%d)" % worker.max_passes,
                )
            if deadline - time.monotonic() < worker.min_pass_secs:
                return _stop_ok("budget floor (<%ds left)" % worker.min_pass_secs)
            try:
                now_queue = _probe_queue(worker)
            except InfraError as exc:
                # work already done this shift — a dead probe stops, not errors
                return _stop_ok("queue unprobed between passes: %s" % exc)
            if now_queue is None or now_queue <= 0:
                # Ready feed empty — remaining backlog may be human/timer/
                # deferred gated (desk ready probe excludes them).
                return _stop_ok("queue empty")
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
                return _stop_ok(reason)
            prev_queue = now_queue
    finally:
        try:
            outfh.close()
        except (NameError, OSError):
            pass  # dry-run/preflight paths never opened it
        lock.release()
