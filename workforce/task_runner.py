"""Prepare one assigned task, then exec a configured provider under WorkForce.

This command does not claim, merge, deploy, schedule, or infer completion. The
provider must claim through WorkLane; WorkForce retains process/budget evidence.
Each reservation is permanent until an operator explicitly reconciles it, so a
failed or repeated dispatch cannot overwrite earlier work or silently retry it.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover -- exercised only on non-POSIX platforms
    fcntl = None

from . import task_routing
from . import routing_binding
from .routing_policy import RoutingPolicyError, load_routing_policy
from . import continuity_recovery as continuity_recovery_mod
from ._utils import latest_signed_owner_id


class PreparationError(RuntimeError):
    pass


# Bumped whenever a receipt's meaning under the reservation lock changes.
# Receipts written before this existed (or with an older value) cannot be
# trusted to prove their tracked process ever held -- or would have released
# -- the reservation lock, so an absent/unlocked lock file alone never proves
# they stopped; recovery requires an explicit operator acknowledgement instead.
LOCK_PROTOCOL_VERSION = 2  # provider children inherit exclusion in generated adapters


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=30)
    if p.returncode:
        raise PreparationError("git preparation failed: " + p.stderr.strip())
    return p.stdout.strip()


def _fetch(url):
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
        raise PreparationError("Invalid project, worker, or task identifier")
    return value


def _path(value):
    path = Path(value)
    if not path.is_absolute():
        raise PreparationError("Configuration paths must be absolute")
    return path.resolve()


def _render(value, fields):
    # Single pass: values (including task prose) cannot introduce placeholders.
    return re.sub(r"\{([a-z_]+)\}", lambda m: fields.get(m[1], m[0]), value)


def _recovery_block(task_id, attempt, reason):
    """Prepend-able notice ensuring the seat sees the recovery reason.

    Rendered unconditionally onto every recovered prompt regardless of
    whether the seat's own template has a {recovery_reason} slot, so an
    existing host prompt benefits without editing (wf-257). The reason is
    normalized to a single whitespace-collapsed paragraph and length-capped
    so an operator-supplied reason cannot fracture the prompt with newlines
    or (via %-style content) trip a format operation; the full reason still
    reaches the receipt untouched.
    """
    safe_reason = re.sub(r"\s+", " ", reason).strip()[:2000]
    return ("Recovery: this is recovery attempt " + str(attempt) + " of " + str(task_id) +
            ". Reason: " + safe_reason + ". Act on the reason before anything else.\n\n")


def _eligible_tasks(config, fetch):
    """Return (project, worker, eligible tasks) from the authoritative ready feed."""
    project = _slug(config["project"])
    worker = _slug(config["worker"])
    if os.environ.get("WL_AGENT_ID") != worker:
        raise PreparationError("WorkForce identity does not match configuration")
    origin = config["desk_url"].rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment or parsed.path:
        raise PreparationError("desk_url must be an HTTP(S) origin")
    required_label = config["required_label"]
    if not isinstance(required_label, str) or not required_label:
        raise PreparationError("An explicit execution eligibility label is required")
    query = urlencode({"product": project, "label": "worker:" + worker})
    data = fetch(origin + "/api/admin/tasks/ready?" + query)
    tasks = data.get("tasks")
    if (not isinstance(tasks, list) or type(data.get("count")) is not int
            or data["count"] != len(tasks) or data.get("ok") is False):
        raise PreparationError("Unavailable, malformed, or incomplete ready feed")
    eligible = []
    for task in tasks:
        if not isinstance(task, dict):
            raise PreparationError("Ready feed returned an invalid task")
        labels = task.get("labels", [])
        # HTTP scopes the envelope; MCP also supplies product on each row.
        # When both are present neither may contradict the selected store.
        if (task.get("product", data.get("product")) != project
                or data.get("product", project) != project
                or task.get("status") != "backlog"
                or not isinstance(labels, list)
                or [x for x in labels if isinstance(x, str) and x.startswith("worker:")] != ["worker:" + worker]
                or task.get("gate_type") in ("human", "deferred", "tracking")):
            raise PreparationError("Ready feed returned foreign or ineligible work")
        if required_label in labels:
            eligible.append(dict(task, product=project))
    return project, worker, eligible


def _fetch_task_detail(config, task_id, fetch):
    """Fetch the exact WorkLane task by id -- never the ready feed's projection.

    Used only by explicit same-owner recovery: an owned in_progress/in_review
    task never appears in the ready feed (it is not backlog), so eligibility
    there cannot be decided from :func:`_eligible_tasks`. Fails closed on any
    unavailable or mismatched response rather than assuming identity.
    """
    project = _slug(config["project"])
    origin = config["desk_url"].rstrip("/")
    detail = fetch(origin + "/api/admin/tasks/" + task_id + "?" + urlencode({"product": project}))
    if (not isinstance(detail, dict) or detail.get("ok") is not True
            or detail.get("product", project) != project):
        raise PreparationError("Unavailable or malformed task detail")
    task = detail.get("task")
    if not isinstance(task, dict) or task.get("product") != project or task.get("id") != task_id:
        raise PreparationError("Task detail response does not match the requested product/id")
    return task


def _same_owner_recovery_match(config, worker, task_id, fetch):
    """Validate an explicit operator resume of the recovering worker's own
    active or parked claim, fetched directly rather than via the ready feed.

    This is the manual continuation case: a correctly owned in_progress or
    in_review task is never ready-eligible, so it cannot be found by
    :func:`_eligible_tasks`. Recovery here never infers ownership merely from
    the worker:<name> label -- it also requires the latest signed Owner:
    marker (PROCESS §5) to name this worker, and fails closed on any
    unavailable, ambiguous, gated, or terminal detail.
    """
    required_label = config["required_label"]
    if not isinstance(required_label, str) or not required_label:
        raise PreparationError("An explicit execution eligibility label is required")
    task = _fetch_task_detail(config, task_id, fetch)
    if task.get("status") not in task_routing._LIVE_CLAIM_STATUSES:
        raise PreparationError(
            "Task is not an active or parked same-owner claim; release/reassign it in "
            "WorkLane before recovery")
    if task.get("gate_type") not in (None, ""):
        raise PreparationError("Task carries a blocking gate; refusing explicit same-owner recovery")
    labels = task.get("labels", [])
    if not isinstance(labels, list):
        raise PreparationError("Task detail returned invalid labels")
    worker_labels = [x for x in labels if isinstance(x, str) and x.startswith("worker:")]
    if worker_labels != ["worker:" + worker]:
        raise PreparationError("Task worker label does not match the recovering worker")
    if required_label not in labels:
        raise PreparationError("Task does not carry the configured execution eligibility label")
    if latest_signed_owner_id(task.get("comments")) != worker:
        raise PreparationError(
            "Latest signed WorkLane owner does not name the recovering worker; refusing "
            "to infer ownership from the worker label alone")
    return dict(task, product=config["project"])


def _load_prepared_routing(config):
    context_raw = os.environ.get(routing_binding.CONTEXT_ENV)
    context = json.loads(context_raw) if context_raw else None
    path, host = config.get("routing_policy"), config.get("routing_host")
    if context is not None:
        if not isinstance(context, dict) or context.get("runner_sha256") != routing_binding.runner_digest(config):
            raise PreparationError("supervisor runner configuration changed")
        if path and path != context.get("policy"):
            raise PreparationError("supervisor and runner routing policy differ")
        path, host = context.get("policy"), context.get("host")
    if not path:
        if context is not None:
            raise PreparationError("supervisor routing policy is missing")
        return None
    if not isinstance(host, str) or not host.strip():
        raise PreparationError("routing_host must be a non-empty string when routing_policy is set")
    path = str(_path(path))
    policy, seat = routing_binding.bound_seat(path, config)
    return {"path": path, "seats": (seat,), "results": policy.results_by_candidate,
            "host": host, "context": context}


def _routed_task(config, eligible, routing):
    if routing is None:
        return eligible[0] if eligible else None, None
    context = routing["context"]
    if context:
        eligible = [t for t in eligible if t.get("id") == context.get("task_id")]
        if len(eligible) != 1 or routing_binding.task_digest(eligible[0]) != context.get("task_sha256"):
            raise PreparationError("selected work order changed after supervisor qualification")
    for task in eligible:
        try:
            receipt = routing_binding.qualify(routing["path"], routing["host"], config, task)
            return task, receipt
        except ValueError:
            continue
    return None, None


def _record_launch_binding(config, result, routing, task, qualification):
    if routing is None:
        return result
    data = json.loads(Path(result["receipt"]).read_text())
    data["routing"] = {"policy": routing["path"], "host": routing["host"],
        "runner_sha256": routing_binding.runner_digest(config),
        "task_sha256": routing_binding.task_digest(task), "qualification": qualification,
        "head": _git(result["checkout"], "rev-parse", "HEAD"),
        "authority_sha256": hashlib.sha256(_authority_text(config).encode()).hexdigest()}
    context = routing.get("context") or {}
    if context.get("resume"):
        data["routing"]["resume"] = context["resume"]
    Path(result["receipt"]).write_text(json.dumps(data, indent=2) + "\n")
    return result


def _revalidate_launch(config, result, fetch=_fetch):
    data = json.loads(Path(result["receipt"]).read_text())
    if data.get("recovery_kind") == "same_owner":
        if os.environ.get(routing_binding.CONTEXT_ENV) or config.get("routing_policy"):
            raise PreparationError("qualified routing requires its guarded handoff path")
        binding = data.get("manual_recovery") or {}
        task = _same_owner_recovery_match(config, result["worker"], result["task_id"], fetch)
        current = {
            "runner_sha256": routing_binding.runner_digest(config),
            "task_sha256": routing_binding.task_digest(task),
            "head": _git(result["checkout"], "rev-parse", "HEAD"),
            "authority_sha256": hashlib.sha256(_authority_text(config).encode()).hexdigest(),
        }
        if binding != current:
            raise PreparationError("manual recovery context changed before provider launch")
        return
    bound = data.get("routing")
    if bound is None:
        if os.environ.get(routing_binding.CONTEXT_ENV) or config.get("routing_policy"):
            raise PreparationError("qualified launch receipt missing")
        return
    if bound["runner_sha256"] != routing_binding.runner_digest(config):
        raise PreparationError("runner changed before provider launch")
    if bound["head"] != _git(result["checkout"], "rev-parse", "HEAD"):
        raise PreparationError("source revision changed before provider launch")
    if bound["authority_sha256"] != hashlib.sha256(_authority_text(config).encode()).hexdigest():
        raise PreparationError("instructions changed before provider launch")
    _, _, tasks = _eligible_tasks(config, fetch)
    task = next((t for t in tasks if t.get("id") == result["task_id"]), None)
    if task is None or routing_binding.task_digest(task) != bound["task_sha256"]:
        raise PreparationError("work order changed before provider launch")
    routing_binding.qualify(bound["policy"], bound["host"], config, task)
    resume = bound.get("resume")
    if resume:
        canonical = _canonical_reservation(Path(result["receipt"])) / "preparation.json"
        original = json.loads(canonical.read_text())
        detail = fetch(config["desk_url"].rstrip("/") + "/api/admin/tasks/" + result["task_id"]
                       + "?" + urlencode({"product": config["project"]}))
        current = detail.get("task", {})
        if current.get("product") != config["project"] or current.get("id") != result["task_id"]:
            raise PreparationError("checkpoint work order identity changed before launch")
        signed = continuity_recovery_mod.parse_latest_signed_checkpoint(current, owner=resume["checkpoint_owner"])
        if (signed is None or signed["checkpoint_id"] != resume["checkpoint_id"]
                or signed["checkpoint"] != resume["checkpoint"]):
            raise PreparationError("signed checkpoint changed before provider launch")
        continuity_recovery_mod._observed_resume(signed["checkpoint"], config, config, original, current)



def _check_remote(repo, config):
    remote = config.get("remote", "origin")
    if _git(repo, "remote", "get-url", remote) != config["expected_remote"]:
        raise PreparationError("Repository remote differs from authorized destination")
    if _git(repo, "remote", "get-url", "--push", remote) != config["expected_remote"]:
        raise PreparationError("Repository push destination differs from authorization")


def _run_auth_check(config):
    auth = config.get("auth_check")
    if auth:
        if not isinstance(auth, list) or not all(isinstance(x, str) for x in auth):
            raise PreparationError("auth_check must be an argv list")
        check = subprocess.run(auth, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if check.returncode:
            raise PreparationError("Provider authentication check failed")


def _authority_text(config):
    authority = [_path(p) for p in config["authority_chain"]]
    if not authority:
        raise PreparationError("An explicit authority chain is required")
    return "\n\n".join(str(p) + "\n" + p.read_text() for p in authority)


def prepare(config, fetch=_fetch):
    """Return launch metadata, or None for an empty eligible feed; never write WL."""
    command = config["command"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise PreparationError("Provider command must be an argv list")
    repo = _path(config["repository"])
    state = _path(config["state_dir"])
    prompt_template = _path(config["prompt_template"]).read_text()
    authority_text = _authority_text(config)
    project, worker, eligible = _eligible_tasks(config, fetch)
    if not eligible:
        return None
    routing = _load_prepared_routing(config)
    task, qualification = _routed_task(config, eligible, routing)
    if task is None:
        return None
    task_id = _slug(task["id"])
    _check_remote(repo, config)
    base = _git(repo, "rev-parse", "--verify", config.get("base_ref", "origin/main") + "^{commit}")
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    _run_auth_check(config)
    reservation = state / worker / task_id
    reservation.parent.mkdir(parents=True, exist_ok=True)
    try:
        reservation.mkdir()  # Atomic, never reuse or reset earlier task work.
    except FileExistsError:
        raise PreparationError("Task already prepared; inspect preserved receipt/work before recovery")
    checkout = reservation / "checkout"
    branch = "workforce/task/" + worker + "/" + task_id
    receipt = {"project": project, "worker": worker, "task_id": task_id,
               "branch": branch, "base": base, "checkout": str(checkout),
               "state": "reserved", "claimed": False,
               "lock_protocol": LOCK_PROTOCOL_VERSION}
    receipt_path = reservation / "preparation.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        _git(repo, "worktree", "add", "-b", branch, str(checkout), base)
        fields = {"project": project, "worker": worker, "task_id": task_id,
                  "branch": branch, "checkout": str(checkout),
                  "git_common_dir": str(common), "result": str(reservation / "result.md"),
                  "authority": authority_text,
                  "recovery_reason": "", "recovery_attempt": "", "recovery_of": ""}
        prompt = _render(prompt_template, fields)
        prompt_path = reservation / "prompt.md"
        prompt_path.write_text(prompt)
        fields.update(prompt=prompt, prompt_file=str(prompt_path))
        argv = [_render(arg, fields) for arg in command]
        receipt.update(state="prepared", prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        result = _record_launch_binding(config, {"argv": argv, "checkout": str(checkout), "receipt": str(receipt_path),
                "project": project, "worker": worker, "task_id": task_id,
                "lock": str(reservation / "lock")}, routing, task, qualification)
        return result
    except Exception:
        receipt["state"] = "preparation_failed"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise


def exclude_from_git(checkout, names):
    """Append names to the checkout worktree's git info/exclude, idempotently.

    Planted vendor identity files (``.cursor/``, ``.grok/``) must never be
    picked up by the seat's own ``git add``. A dispatched checkout is a git
    worktree, so ``<checkout>/.git`` is a file pointing at the real gitdir
    under ``<repo>/.git/worktrees/<name>``; resolve that before writing.
    """
    checkout = Path(checkout)
    dotgit = checkout / ".git"
    if dotgit.is_dir():
        git_dir = dotgit
    elif dotgit.is_file():
        line = dotgit.read_text().strip()
        _, _, target = line.partition(":")
        target = Path(target.strip())
        git_dir = target if target.is_absolute() else (checkout / target).resolve()
    else:
        return
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set(exclude_path.read_text().splitlines()) if exclude_path.exists() else set()
    missing = [name for name in names if name not in existing]
    if missing:
        with exclude_path.open("a") as handle:
            for name in missing:
                handle.write(name + "\n")


def _find_worktree(repo, checkout, branch):
    """Return True if checkout is a registered worktree of repo on branch."""
    listing = _git(repo, "worktree", "list", "--porcelain")
    checkout = checkout.resolve()
    for entry in listing.split("\n\n"):
        fields = {}
        for line in entry.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if fields.get("worktree") and Path(fields["worktree"]).resolve() == checkout:
            return fields.get("branch") == "refs/heads/" + branch
    return False


def _canonical_reservation(receipt_path):
    """Resolve any receipt -- original or nested attempts/N -- to one reservation root.

    A caller could point --recover-receipt at an earlier attempt's own
    preparation.json instead of the original. If that were allowed to define
    its own lock/attempts scope, two recoveries anchored on different
    receipts of the same reservation could take different locks and overlap.
    Every receipt under a reservation always resolves to the same root here.
    """
    path = receipt_path.parent
    while path.name.isdigit() and path.parent.name == "attempts":
        path = path.parent.parent
    return path


def recover(config, receipt_path, reason, fetch=_fetch, legacy_stop_evidence=None):
    """Explicitly resume a preserved reservation; never create or reset a checkout.

    Requires an operator-supplied receipt path and recovery reason. Whichever
    receipt is supplied (the original or a nested attempt), recovery always
    anchors on the one canonical original receipt at the reservation root, so
    every attempt and every worker shares the same reservation lock. The
    reservation's evidence (prompt/result/receipt) is preserved untouched; a
    new, uniquely numbered attempt directory holds this recovery's own prompt,
    result path and receipt. The task must currently be ready-eligible for the
    configured worker in WorkLane, OR -- the manual continuation case -- must be
    the recovering worker's own currently active or parked (in_progress/in_review)
    claim on the exact same original reservation, validated directly against
    WorkLane's task detail rather than the ready feed (see
    :func:`_same_owner_recovery_match`). Different-worker recovery is a legitimate
    handoff as long as the ready feed already reflects that reassignment; an old
    owner can never resume after a handoff, since the same-owner path additionally
    requires the canonical reservation's own worker to still match the one
    recovering.

    A canonical receipt written before LOCK_PROTOCOL_VERSION existed never
    held the reservation lock in the first place, so an absent or unlocked
    lock file proves nothing about whether its process is still running.
    Recovering such a receipt requires `legacy_stop_evidence`: an explicit,
    retained operator statement of how they confirmed the prior process
    stopped. That statement is recorded on the new attempt's receipt.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise PreparationError("An explicit operator recovery reason is required")
    state = _path(config["state_dir"])
    receipt_path = _path(str(receipt_path))
    try:
        receipt_path.relative_to(state)
    except ValueError:
        raise PreparationError("Recovery receipt must be inside the configured state directory")
    if not receipt_path.is_file():
        raise PreparationError("Recovery receipt does not exist")
    reservation = _canonical_reservation(receipt_path)
    canonical_receipt_path = reservation / "preparation.json"
    if not canonical_receipt_path.is_file():
        raise PreparationError("Canonical original receipt is missing; cannot resolve a single reservation anchor")
    old = json.loads(canonical_receipt_path.read_text())
    for key in ("project", "worker", "task_id", "branch", "checkout", "base"):
        if not isinstance(old.get(key), str) or not old[key]:
            raise PreparationError("Canonical original receipt is missing required fields")
    if (old.get("lock_protocol") != LOCK_PROTOCOL_VERSION
            or old.get("execution_lock_protocol") != LOCK_PROTOCOL_VERSION):
        if not isinstance(legacy_stop_evidence, str) or not legacy_stop_evidence.strip():
            raise PreparationError(
                "Canonical receipt predates the lock protocol; an absent or unlocked lock file cannot "
                "prove that process stopped. Recovery requires explicit legacy-stop evidence recording "
                "how the operator confirmed it stopped")
    project, worker, eligible = _eligible_tasks(config, fetch)
    if old["project"] != project:
        raise PreparationError("Recovery receipt belongs to a different project")
    task_id = _slug(old["task_id"])
    match = next((t for t in eligible if t.get("id") == task_id), None)
    routing = _load_prepared_routing(config)
    same_owner_recovery = match is None
    if not same_owner_recovery:
        matched, qualification = _routed_task(config, [match], routing)
        if matched is None:
            raise PreparationError("recovery seat is not qualified for the preserved work")
    else:
        # Manual continuation: a correctly owned in_progress/in_review
        # task is never in the backlog-only ready feed, so an empty match there does
        # not by itself mean the task is unavailable. Explicit same-owner recovery is
        # validated directly against WorkLane's task detail instead, and only for the
        # worker that already holds the canonical reservation -- never a handoff, which
        # remains the ready-feed/routed path above.
        if old["worker"] != worker:
            raise PreparationError(
                "Task is not currently ready for this worker, and the canonical reservation "
                "belongs to a different worker; release/reassign it in WorkLane before recovery")
        if routing is not None:
            raise PreparationError(
                "qualified routing requires its guarded handoff path")
        match = _same_owner_recovery_match(config, worker, task_id, fetch)
        qualification = None
    repo = _path(config["repository"])
    _check_remote(repo, config)
    checkout = _path(old["checkout"])
    try:
        checkout.relative_to(state)
    except ValueError:
        raise PreparationError("Preserved checkout is outside the configured state directory")
    if not checkout.is_dir():
        raise PreparationError("Preserved checkout is missing; recovery cannot fabricate a new one")
    if not _find_worktree(repo, checkout, old["branch"]):
        raise PreparationError("Preserved checkout is not a registered worktree of this repository on its branch")
    _run_auth_check(config)
    prompt_template = _path(config["prompt_template"]).read_text()
    authority_text = _authority_text(config)
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    attempts = reservation / "attempts"
    attempts.mkdir(exist_ok=True)
    n = 1
    while True:
        attempt = attempts / str(n)
        try:
            attempt.mkdir()
            break
        except FileExistsError:
            n += 1
    fields = {"project": project, "worker": worker, "task_id": task_id,
              "branch": old["branch"], "checkout": str(checkout),
              "git_common_dir": str(common), "result": str(attempt / "result.md"),
              "authority": authority_text,
              "recovery_reason": reason, "recovery_attempt": str(n),
              "recovery_of": str(canonical_receipt_path)}
    prompt = _render(prompt_template, fields)
    prompt = _recovery_block(task_id, n, reason) + prompt
    prompt_path = attempt / "prompt.md"
    prompt_path.write_text(prompt)
    fields.update(prompt=prompt, prompt_file=str(prompt_path))
    argv = [_render(arg, fields) for arg in config["command"]]
    new_receipt = {"project": project, "worker": worker, "task_id": task_id,
                   "branch": old["branch"], "base": old["base"], "checkout": str(checkout),
                   "state": "recovered", "claimed": False,
                   "lock_protocol": LOCK_PROTOCOL_VERSION,
                   "recovery_of": str(canonical_receipt_path),
                   "recovery_source_receipt": str(receipt_path),
                   "recovery_reason": reason,
                   "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    if legacy_stop_evidence:
        new_receipt["legacy_stop_acknowledged"] = True
        new_receipt["legacy_stop_evidence"] = legacy_stop_evidence
    if same_owner_recovery:
        new_receipt["recovery_kind"] = "same_owner"
        new_receipt["manual_recovery"] = {
            "runner_sha256": routing_binding.runner_digest(config),
            "task_sha256": routing_binding.task_digest(match),
            "head": _git(checkout, "rev-parse", "HEAD"),
            "authority_sha256": hashlib.sha256(authority_text.encode()).hexdigest(),
        }
    new_receipt_path = attempt / "preparation.json"
    new_receipt_path.write_text(json.dumps(new_receipt, indent=2) + "\n")
    return _record_launch_binding(config, {"argv": argv, "checkout": str(checkout), "receipt": str(new_receipt_path),
            "project": project, "worker": worker, "task_id": task_id,
            "lock": str(reservation / "lock")}, routing, match, qualification)


def _acquire_lock(lock_path):
    """Hold a per-reservation exclusive lock across exec; refuse if already held.

    The lock is tied to the open file descriptor's lifetime, not a PID: if a
    prior process dies (even uncleanly) the kernel releases it when the
    descriptor closes, so liveness never rests on a PID-age guess. This
    requires POSIX advisory locking (fcntl.flock); on a platform without it
    this fails closed rather than launching without exclusion, and rather
    than crashing the whole module at import time.
    """
    if fcntl is None:
        raise PreparationError(
            "POSIX file locking is unavailable on this platform; refuse to launch without reservation exclusion")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise PreparationError(
            "A prior process for this reservation may still be active; refuse ambiguous live state")
    os.set_inheritable(fd, True)
    return fd


def main(argv=None, *, before_exec=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--recover-receipt",
                         help="Preserved preparation.json to resume; requires --recovery-reason")
    parser.add_argument("--recovery-reason",
                         help="Operator rationale for explicit recovery; requires --recover-receipt")
    parser.add_argument("--legacy-stop-evidence",
                         help="Operator's retained proof a pre-lock-protocol receipt's process stopped; "
                              "required only when the canonical receipt predates the lock protocol")
    args = parser.parse_args(argv)
    if bool(args.recover_receipt) != bool(args.recovery_reason):
        print("Task preparation stopped: --recover-receipt and --recovery-reason must be used together",
              file=sys.stderr)
        return 1
    lock_fd = None
    try:
        config = json.loads(Path(args.config).read_text())
        if args.recover_receipt:
            result = recover(config, args.recover_receipt, args.recovery_reason,
                              legacy_stop_evidence=args.legacy_stop_evidence)
        else:
            result = prepare(config)
        if result is None:
            print("No eligible assigned work; stopped without launching a provider.")
            return 0
        print("Prepared %s; not yet claimed. Receipt: %s" % (result["task_id"], result["receipt"]), flush=True)
        lock_fd = _acquire_lock(result["lock"])  # noqa: F841 -- kept open across exec
        _revalidate_launch(config, result)
        if before_exec is not None:
            before_exec(config, result)
            _revalidate_launch(config, result)
        # Preparation alone cannot prove an older/custom wrapper inherited
        # the lock. Only this guarded exec path attests actual launch exclusion.
        receipt_path = Path(result["receipt"])
        receipt = json.loads(receipt_path.read_text())
        receipt["execution_lock_protocol"] = LOCK_PROTOCOL_VERSION
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        os.chdir(result["checkout"])
        env = dict(os.environ)
        env.update(WL_AGENT_ID=result["worker"], TP_AGENT_ID=result["worker"],
                   WL_PROJECT=result["project"], TP_PROJECT=result["project"])
        # Replace this process, preserving WorkForce's budget and exit tracking.
        os.execvpe(result["argv"][0], result["argv"], env)
    except (PreparationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("Task preparation stopped: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
