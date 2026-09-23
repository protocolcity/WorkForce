"""Qualified checkpoint recovery after quota-limited or interrupted runs (wf-283).

Opt-in through roster ``qualified_recovery`` and an explicit ``recovery_fallback_workers``
order. Replaces the legacy ``fallback_runtime`` swap (unqualified CLI takeover) with
routing-policy-aware handoff onto another registered seat on the same work order.
WorkLane checkpoint/handoff writes are explicit HTTP operations; tests inject ``post_fn``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import contextlib
import tempfile
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import engine, routing_binding
from .roster import Roster, Worker

SCHEMA_ID = "workforce.continuity_recovery/v1"
DEFAULT_COOLDOWN_SECS = 60
DEFAULT_MAX_ATTEMPTS = 3
CHECKPOINT_PREFIX = "Work checkpoint v1:\n"
HANDOFF_ACTOR = "you"

PostFn = Callable[[str, dict], dict]

INTERRUPTION_QUOTA = "quota_exhausted"
INTERRUPTION_TRANSPORT = "transient_transport"
INTERRUPTION_AUTH = "authentication"
INTERRUPTION_PERMISSION = "permission"
INTERRUPTION_TOOL = "tool_incompatibility"
INTERRUPTION_USER = "user_decision"
INTERRUPTION_UNKNOWN = "unknown"

_FALLBACK_ALLOWED = frozenset({INTERRUPTION_QUOTA, INTERRUPTION_TRANSPORT})

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recovery_root(local_root: str) -> str:
    return os.path.join(local_root, "continuity", "recovery")


def plan_path(local_root: str, task_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", task_id)
    return os.path.join(recovery_root(local_root), "%s.json" % safe)


def classify_interruption(reason: str, *, stop_value: str = "") -> Dict[str, Any]:
    """Classify a shift failure; only unambiguous quota/transport may fallback."""
    text = (reason or "").lower()
    stop = (stop_value or "").lower()
    if "auth" in text or "auth" in stop or "401" in text:
        kind = INTERRUPTION_AUTH
    elif any(x in text + " " + stop for x in ("permission", "denied", "403", "forbidden", "unauthorized")):
        kind = INTERRUPTION_PERMISSION
    elif stop in ("cancelled", "canceled") or "user decision" in text:
        kind = INTERRUPTION_USER
    elif "tool" in text and any(x in text for x in ("incompat", "missing", "unknown")):
        kind = INTERRUPTION_TOOL
    elif text.startswith("vendor limit:") or "rate limit" in text or "quota" in text:
        kind = INTERRUPTION_QUOTA
    elif any(x in text for x in ("timeout", "timed out", "connection reset", "transport", "502", "503")):
        kind = INTERRUPTION_TRANSPORT
    else:
        kind = INTERRUPTION_UNKNOWN
    return {
        "kind": kind,
        "fallback_allowed": kind in _FALLBACK_ALLOWED,
        "reason": reason or stop_value or kind,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_artifact_ref(checkout: Path, ref: str) -> Optional[Path]:
    if not ref or ref.startswith("/") or ref.startswith("\\"):
        return None
    ref_path = Path(ref)
    if ref_path.is_absolute() or ".." in ref_path.parts:
        return None
    try:
        checkout_resolved = checkout.resolve()
        target = (checkout / ref).resolve()
        target.relative_to(checkout_resolved)
    except (OSError, ValueError):
        return None
    if any((checkout / Path(*ref_path.parts[:i])).is_symlink()
           for i in range(1, len(ref_path.parts) + 1)):
        return None
    return target


def artifact_hashes(checkout: Path, artifacts: Sequence[dict]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for art in artifacts:
        ref = str(art.get("ref") or "").strip()
        if not ref:
            continue
        target = _resolve_artifact_ref(checkout, ref)
        if target is not None and target.is_file():
            out[ref] = _sha256_file(target)
    return out


def validate_partial_artifacts(checkpoint: dict, checkout: Path) -> List[str]:
    errors: List[str] = []
    for art in checkpoint.get("artifacts") or []:
        if not isinstance(art, dict):
            errors.append("invalid artifact entry")
            continue
        ref = str(art.get("ref") or "")
        expected = str(art.get("sha256") or "")
        if not ref or not re.fullmatch(r"[0-9a-f]{64}", expected):
            errors.append("artifact metadata invalid for %r" % ref)
            continue
        path = _resolve_artifact_ref(checkout, ref)
        if path is None:
            errors.append("unsafe artifact ref %r" % ref)
            continue
        if not path.is_file():
            errors.append("missing artifact %r" % ref)
            continue
        if _sha256_file(path) != expected:
            errors.append("changed artifact %r" % ref)
    return errors


def validate_resume_facts(
    checkpoint: dict,
    *,
    project: str,
    workspace_id: str,
    source_revision: str,
    instruction_revision: str,
    checkout: Path,
) -> List[str]:
    errors: List[str] = []
    for key, expected in (
        ("project", project),
        ("workspace_id", workspace_id),
        ("source_revision", source_revision),
        ("instruction_revision", instruction_revision),
    ):
        value = checkpoint.get(key)
        if not isinstance(value, str) or value != expected:
            errors.append("resume mismatch: %s" % key)
    hashes = artifact_hashes(checkout, checkpoint.get("artifacts") or [])
    for art in checkpoint.get("artifacts") or []:
        ref = str(art.get("ref") or "")
        expected = str(art.get("sha256") or "")
        if hashes.get(ref) != expected:
            errors.append("missing or changed resume artifact: %s" % ref)
    return errors


def parse_latest_signed_checkpoint(task: dict, *, owner: str) -> Optional[Dict[str, Any]]:
    comments = task.get("comments")
    if not isinstance(comments, list):
        return None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not body.startswith(CHECKPOINT_PREFIX):
            continue
        if comment.get("author") != owner or not comment.get("id"):
            return None
        try:
            data = json.loads(body[len(CHECKPOINT_PREFIX):])
        except ValueError:
            return None
        if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
                or any(not isinstance(data.get(k), str) or not data[k].strip() for k in
                    ('project','workspace_id','objective','acceptance','scope','instruction_revision','source_revision','branch','next_action'))
                or any(not isinstance(data.get(k), list) for k in ('artifacts','decisions','checks','remaining'))):
            return None
        return {"checkpoint_id": str(comment['id']), "checkpoint": data}
    return None


def pause_view(plan: dict) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_ID,
        "task_id": plan.get("task_id"),
        "project": plan.get("project"),
        "primary_worker": plan.get("primary_worker"),
        "target_worker": plan.get("target_worker"),
        "transfer_state": plan.get("transfer_state"),
        "status": plan.get("status"),
        "interruption": plan.get("interruption"),
        "pause_reason": plan.get("pause_reason"),
        "preserved": plan.get("preserved"),
        "next_actions": plan.get("next_actions"),
        "attempts_used": plan.get("attempts_used"),
        "cooldown_until": plan.get("cooldown_until"),
    }


def list_pause_states(local_root: str) -> List[Dict[str, Any]]:
    root = recovery_root(local_root)
    if not os.path.isdir(root):
        return []
    out: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, name), "r", encoding="utf-8") as fh:
                plan = json.load(fh)
            if plan.get("status") != "resumed":
                out.append(pause_view(plan))
        except (OSError, ValueError):
            continue
    return out


def _load_plan(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_plan(path: str, plan: dict) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".recovery-", dir=str(parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(plan, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _operation_lock(path):
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path + ".operation-lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def instruction_revision(config):
    """Hash explicitly shared workspace/project rules, excluding seat contracts."""
    from . import task_runner as tr
    paths = config.get("continuity_instructions")
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
        raise ValueError("explicit continuity_instructions are required")
    return hashlib.sha256(tr._authority_text({"authority_chain": paths}).encode()).hexdigest()


def _observed_resume(checkpoint, primary_config, target_config, receipt, task):
    from . import task_runner as tr
    if (primary_config.get("project") != task["product"] or target_config.get("project") != task["product"]):
        raise ValueError("receiving runner project differs from work")
    workspace_id = primary_config.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id or target_config.get("workspace_id") != workspace_id:
        raise ValueError("explicit matching workspace identities are required")
    checkout = tr._path(receipt["checkout"])
    for config in (primary_config, target_config):
        checkout.relative_to(tr._path(config["state_dir"]))
        if not tr._find_worktree(tr._path(config["repository"]), checkout, receipt["branch"]):
            raise ValueError("preserved checkout/repository/branch mismatch")
        tr._check_remote(tr._path(config["repository"]), config)
    revision = instruction_revision(primary_config)
    if instruction_revision(target_config) != revision:
        raise ValueError("receiving shared instructions differ")
    if checkpoint.get("branch") != receipt["branch"]:
        raise ValueError("checkpoint branch differs from preserved checkout")
    head = tr._git(checkout, "rev-parse", "HEAD")
    errors = validate_resume_facts(checkpoint, project=task["product"], workspace_id=workspace_id,
        source_revision=head, instruction_revision=revision, checkout=checkout)
    errors += validate_partial_artifacts(checkpoint, checkout)
    # Every uncommitted file must be preserved by the owner's checkpoint. Deleted
    # or renamed paths require an explicit recovery review instead of inference.
    changes = subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain', '-z'])
    artifacts = {a['ref'] for a in checkpoint.get('artifacts', []) if isinstance(a, dict) and 'ref' in a}
    for entry in changes.decode().split('\0'):
        if not entry:
            continue
        status, name = entry[:2], entry[3:]
        if any(c in status for c in 'DRC') or name not in artifacts:
            errors.append("uncheckpointed working-copy change")
    if errors:
        raise ValueError(errors[0])
    return {"source_revision": head, "instruction_revision": revision,
            "workspace_id": workspace_id, "checkpoint": checkpoint}


def _fallback_order(worker: Worker, roster: Roster) -> List[str]:
    return [w for w in (worker.recovery_fallback_workers or []) if w in roster.workers]


def _pool_id_for_worker(worker_name: str, policy_path: str, runner_config: dict) -> Optional[str]:
    try:
        _, seat = routing_binding.bound_seat(policy_path, runner_config)
        quota = seat.candidate.quota
        if quota is None or not quota.pool_id:
            return None
        return "%s:%s" % (quota.pool, quota.pool_id)
    except ValueError:
        return None


def _backlog_projection(task: dict, worker_name: str) -> dict:
    labels = [label for label in (task.get("labels") or []) if not str(label).startswith("worker:")]
    labels.append("worker:" + worker_name)
    projected = dict(task)
    projected["status"] = "backlog"
    projected["labels"] = labels
    return projected


def _seat_qualifies_recovery(
    worker_name: str,
    roster: Roster,
    task: dict,
    policy_path: str,
    host: str,
    exhausted_pools: Sequence[str],
) -> Tuple[bool, str]:
    worker = roster.workers.get(worker_name)
    if worker is None:
        return False, "%s is not registered" % worker_name
    if worker.kind != "lane":
        return False, "%s is not a claiming lane" % worker_name
    try:
        runner = routing_binding.configured_runner(worker)
    except ValueError as exc:
        return False, str(exc)
    pool = _pool_id_for_worker(worker_name, policy_path, runner)
    if pool and pool in exhausted_pools:
        return False, "shared quota pool %s exhausted" % pool
    projection = _backlog_projection(task, worker_name)
    try:
        routing_binding.qualify(policy_path, host, runner, projection, worker)
    except ValueError as exc:
        return False, str(exc)
    return True, "qualified"


def _existing_attempts(local_root: str, task_id: str) -> int:
    from .task_runner import _slug
    path = plan_path(local_root, _slug(task_id))
    if not os.path.isfile(path):
        return 0
    try:
        return int(_load_plan(path).get("attempts_used") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def plan_qualified_recovery(**kwargs):
    """Create once under process exclusion; repeated planning cannot reset budgets."""
    from .task_runner import _slug
    task_id = _slug(kwargs["task"].get("id"))
    with _operation_lock(plan_path(kwargs["local_root"], task_id)):
        return _plan_qualified_recovery(**kwargs)


def _plan_qualified_recovery(
    *,
    local_root: str,
    primary: Worker,
    roster: Roster,
    task: dict,
    receipt_path: str,
    interruption: dict,
    policy_path: str,
    host: str,
    workspace_id: str,
    instruction_revision: str,
    exhausted_pools: Optional[List[str]] = None,
) -> dict:
    exhausted_pools = list(exhausted_pools or [])
    task_id = str(task.get("id") or "")
    from .task_runner import _slug
    _slug(task_id)
    existing_path = plan_path(local_root, task_id)
    if os.path.exists(existing_path):
        return _load_plan(existing_path)

    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    checkout = Path(str(receipt.get("checkout") or ""))
    signed = parse_latest_signed_checkpoint(task, owner=primary.name)
    checkpoint = signed["checkpoint"] if signed else None
    artifact_errors = validate_partial_artifacts(checkpoint, checkout) if checkpoint and checkout.is_dir() else []
    if signed is None:
        artifact_errors = artifact_errors or ["missing signed WorkLane checkpoint"]
    preserved = {
        "receipt": receipt_path,
        "checkout": str(checkout),
        "branch": receipt.get("branch"),
        "signed_checkpoint_id": (signed or {}).get("checkpoint_id", ""),
        "checkpoint": checkpoint,
    }
    next_actions: List[str] = []
    pause_reason = interruption.get("reason") or interruption.get("kind")
    target_worker = ""
    refusal = ""
    if not interruption.get("fallback_allowed"):
        pause_reason = "%s — fallback not permitted" % interruption.get("kind")
        next_actions = ["Resolve %s before resuming" % interruption.get("kind")]
    elif signed is None or artifact_errors:
        pause_reason = "artifact recovery required before checkpoint handoff" if artifact_errors else pause_reason
        if signed is None:
            pause_reason = "missing signed WorkLane checkpoint"
        next_actions = (artifact_errors or ["Post a signed checkpoint before recovery handoff"])[:5]
    else:
        for candidate in _fallback_order(primary, roster):
            if candidate == primary.name:
                continue
            ok, detail = _seat_qualifies_recovery(
                candidate, roster, task, policy_path, host, exhausted_pools,
            )
            if ok:
                target_worker = candidate
                break
            refusal = detail
        if not target_worker:
            pause_reason = refusal or "no eligible qualified fallback seat"
            next_actions = ["Refresh routing policy quota evidence", "Reassign manually in WorkLane"]
    ready = (
        target_worker
        and signed
        and not artifact_errors
        and interruption.get("fallback_allowed")
    )
    plan = {
        "schema": SCHEMA_ID,
        "status": "pending" if ready else "paused",
        "task_id": task_id,
        "project": str(task.get("product") or receipt.get("project") or ""),
        "primary_worker": primary.name,
        "target_worker": target_worker,
        "receipt_path": receipt_path,
        "policy_path": policy_path,
        "host": host,
        "workspace_id": workspace_id,
        "instruction_revision": instruction_revision,
        "interruption": interruption,
        "pause_reason": pause_reason,
        "preserved": preserved,
        "next_actions": next_actions,
        "attempts_used": _existing_attempts(local_root, task_id),
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "cooldown_until": "",
        "exhausted_pools": exhausted_pools,
        "created_at": _utc_iso(_utcnow()),
        "transfer_state": "",
    }
    if plan["status"] == "pending":
        plan["next_actions"] = [
            "Confirm prior writer stopped",
            "Hand off to worker:%s using signed checkpoint" % target_worker,
            "Engine recovery dispatch on preserved receipt",
        ]
    _save_plan(plan_path(local_root, task_id), plan)
    return plan


def defer_after_quota_interruption(
    *,
    local_root: str,
    worker: Worker,
    roster: Roster,
    task: dict,
    receipt_path: str,
    reason: str,
    policy_path: str,
    host: str,
    workspace_id: str,
    instruction_revision: str,
) -> dict:
    interruption = classify_interruption(reason)
    pool = None
    try:
        runner = routing_binding.configured_runner(worker)
        pool = _pool_id_for_worker(worker.name, policy_path, runner)
    except ValueError:
        pool = None
    exhausted = [pool] if pool and interruption["kind"] == INTERRUPTION_QUOTA else []
    return plan_qualified_recovery(
        local_root=local_root,
        primary=worker,
        roster=roster,
        task=task,
        receipt_path=receipt_path,
        interruption=interruption,
        policy_path=policy_path,
        host=host,
        workspace_id=workspace_id,
        instruction_revision=instruction_revision,
        exhausted_pools=exhausted,
    )


def _desk_origin(worker: Worker) -> str:
    origin = engine.desk_origin_from_queue_url(worker.queue_url or "")
    if not origin:
        raise ValueError("worker %s has no HTTP desk origin" % worker.name)
    return origin


def _acquire_reservation_lock(lock_path, canonical_receipt, *, timeout_secs=0,
                              poll_secs=0.05, lock_probe=None):
    from . import task_runner as tr
    if canonical_receipt.get("lock_protocol") != tr.LOCK_PROTOCOL_VERSION:
        return None, "legacy lock protocol requires explicit operator stopped evidence and manual recovery"
    if not Path(lock_path).is_file():
        return None, "missing reservation lock; process state is unproven"
    if lock_probe is not None and lock_probe(lock_path) is not False:
        return None, "prior reservation lock is held or unknown"
    try:
        return tr._acquire_lock(lock_path), "prior reservation lock released; recovery coordinator holds exclusion"
    except (OSError, tr.PreparationError):
        return None, "prior reservation lock is held or unknown"


def _release_lock_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def execute_pending_recovery(*, local_root, roster, plan, fetch_task, post_fn,
                             dispatch_fn, dry_run=False, lock_probe=None):
    from . import task_runner as tr
    task_id = tr._slug(plan.get("task_id"))
    path = plan_path(local_root, task_id)
    def refused(reason):
        return {"ok": False, "reason": reason, "plan": pause_view(plan)}
    try:
        with _operation_lock(path):
            if os.path.isfile(path):
                plan = _load_plan(path)
            if plan.get('schema') != SCHEMA_ID or plan.get('status') != 'pending':
                return refused('plan is not pending')
            if plan.get('transfer_state') not in ('', 'transferred'):
                return refused('uncertain prior mutation requires reconciliation')
            kind = classify_interruption(plan.get('interruption', {}).get('reason', ''))
            if not kind['fallback_allowed']:
                return refused('interruption does not permit fallback')
            attempts, maximum = plan.get('attempts_used'), plan.get('max_attempts')
            if (type(attempts) is not int or type(maximum) is not int or attempts < 0
                    or maximum < 1 or maximum > DEFAULT_MAX_ATTEMPTS or attempts >= maximum):
                return refused('recovery attempts exhausted or invalid')
            if plan.get('cooldown_until'):
                try:
                    until = datetime.datetime.fromisoformat(plan['cooldown_until'].replace('Z','+00:00'))
                    if until > _utcnow():
                        return refused('cooldown active')
                except (ValueError, TypeError):
                    return refused('invalid cooldown; reconcile before retry')
            primary = roster.workers.get(plan.get('primary_worker'))
            target = roster.workers.get(plan.get('target_worker'))
            if (primary is None or target is None or not primary.qualified_recovery
                    or target.name not in primary.recovery_fallback_workers or primary.name == target.name):
                return refused('recovery is not explicitly authorized for these registered seats')
            if _desk_origin(primary) != _desk_origin(target):
                return refused('cross-store handoff is not supported')
            primary_config = routing_binding.configured_runner(primary)
            target_config = routing_binding.configured_runner(target)
            receipt_path = tr._path(plan['receipt_path'])
            receipt_path.relative_to(tr._path(primary_config['state_dir']))
            reservation = tr._canonical_reservation(receipt_path)
            receipt = json.loads((reservation/'preparation.json').read_text())
            if receipt.get('task_id') != task_id or receipt.get('project') != plan.get('project'):
                return refused('canonical receipt scope mismatch')
            lock_fd, stop_evidence = _acquire_reservation_lock(str(reservation/'lock'), receipt,
                                                            lock_probe=lock_probe)
            if lock_fd is None:
                return refused(stop_evidence)
            try:
                # Fetch and inspect mutable facts only after taking exclusion.
                task = fetch_task(primary, task_id)
                if task.get('id') != task_id or task.get('product') != plan['project']:
                    return refused('task identity/project mismatch')
                if task.get('status') not in ('in_progress','in_review','backlog'):
                    return refused('work is terminal or unavailable')
                transferred = plan.get('transfer_state') == 'transferred'
                expected_seat = target.name if transferred else primary.name
                labels = [x for x in task.get('labels',[]) if isinstance(x,str) and x.startswith('worker:')]
                if labels != ['worker:'+expected_seat] or (transferred and task.get('status') != 'backlog'):
                    return refused('ownership changed; reconcile before retry')
                signed = parse_latest_signed_checkpoint(task, owner=primary.name)
                if signed is None or signed['checkpoint_id'] != plan.get('preserved',{}).get('signed_checkpoint_id'):
                    return refused('latest signed checkpoint is missing or changed')
                facts = _observed_resume(signed['checkpoint'], primary_config, target_config, receipt, task)
                facts.update(checkpoint_id=signed['checkpoint_id'], checkpoint_owner=primary.name)
                ok, detail = _seat_qualifies_recovery(target.name, roster, task, plan['policy_path'],
                                                    plan['host'], plan.get('exhausted_pools',[]))
                if not ok:
                    return refused('receiving seat no longer qualifies: '+detail)
                if dry_run:
                    return {"ok":True,"dry_run":True,"would_dispatch":target.name,"plan":pause_view(plan)}
                plan['attempts_used'] = attempts + 1
                plan['cooldown_until'] = _utc_iso(_utcnow()+datetime.timedelta(seconds=DEFAULT_COOLDOWN_SECS))
                if not transferred:
                    plan['transfer_state'] = 'handoff_pending'
                    _save_plan(path,plan)
                    payload = dict(author=HANDOFF_ACTOR, project=plan['project'], previous_owner=primary.name,
                        next_owner=target.name, expected_version=task.get('updated_at'),
                        checkpoint_id=signed['checkpoint_id'], stopped_evidence=stop_evidence)
                    try:
                        response = post_fn(_desk_origin(primary)+'/api/admin/tasks/'+task_id+'/handoff',payload)
                        if not isinstance(response,dict) or response.get('ok') is not True:
                            raise ValueError('handoff response did not confirm success')
                        handed = response.get('task')
                        task = fetch_task(primary,task_id)
                        if (not isinstance(handed,dict) or not handed.get('updated_at')
                                or task.get('updated_at') != handed['updated_at']
                                or task.get('status') != 'backlog'
                                or [x for x in task.get('labels',[]) if x.startswith('worker:')] != ['worker:'+target.name]):
                            raise ValueError('handoff response/ownership could not be verified')
                        plan['transfer_state']='transferred'
                        _save_plan(path,plan)
                    except Exception as exc:
                        plan.update(status='needs_reconciliation',pause_reason='handoff outcome uncertain: '+type(exc).__name__)
                        _save_plan(path,plan)
                        return refused(plan['pause_reason'])
                # Pin the freshly transferred task and checkpoint into the actual runner.
                context = dict(policy=plan['policy_path'],host=plan['host'],task_id=task_id,
                    task_sha256=routing_binding.task_digest(task),runner_sha256=routing_binding.runner_digest(target_config),
                    resume=facts)
                plan['transfer_state']='dispatch_pending'
                _save_plan(path,plan)
            finally:
                _release_lock_fd(lock_fd)
            try:
                rc=dispatch_fn(target.name,local_root,str(receipt_path),
                    'qualified checkpoint recovery after '+kind['kind'],routing_context=context)
                current=fetch_task(target,task_id)
                claims = [c for c in current.get('comments', []) if isinstance(c,dict)
                          and str(c.get('body','')).startswith('Owner:')]
                claimed_by_target = bool(claims and claims[-1].get('author') == target.name
                    and claims[-1]['body'].splitlines()[0].strip() == 'Owner: '+target.name)
                if rc==0 and claimed_by_target and current.get('status') in ('in_progress','in_review','done'):
                    plan.update(status='resumed',transfer_state='resumed',pause_reason='receiving executor progressed the same work order')
                elif current.get('status')=='backlog' and 'worker:'+target.name in current.get('labels',[]):
                    plan.update(status='pending' if plan['attempts_used'] < maximum else 'exhausted',
                        transfer_state='transferred',pause_reason='dispatch did not claim; bounded retry remains')
                else:
                    plan.update(status='needs_reconciliation',transfer_state='dispatch_failed',
                        pause_reason='receiving executor stopped with owned or uncertain work')
                _save_plan(path,plan)
                return {"ok":plan['status']=='resumed',"exit_code":rc,"plan":pause_view(plan)}
            except Exception as exc:
                plan.update(status='needs_reconciliation',pause_reason='dispatch outcome uncertain: '+type(exc).__name__)
                _save_plan(path,plan)
                return refused(plan['pause_reason'])
    except (OSError,ValueError,KeyError,tr.PreparationError,subprocess.SubprocessError) as exc:
        return refused(str(exc))


def execute_recovery_for_task(
    *,
    local_root: str,
    roster: Roster,
    task_id: str,
    fetch_task: Callable[[Worker, str], dict],
    post_fn: PostFn,
    dispatch_fn: Callable[..., int],
    dry_run: bool = False,
    lock_probe: Optional[Callable[[str], Optional[bool]]] = None,
) -> dict:
    from .task_runner import _slug
    path = plan_path(local_root, _slug(task_id))
    if not os.path.isfile(path):
        return {"ok": False, "reason": "no recovery plan for %s" % task_id}
    plan = _load_plan(path)
    return execute_pending_recovery(
        local_root=local_root,
        roster=roster,
        plan=plan,
        fetch_task=fetch_task,
        post_fn=post_fn,
        dispatch_fn=dispatch_fn,
        dry_run=dry_run,
        lock_probe=lock_probe,
    )
