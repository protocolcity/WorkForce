"""Mode B roster re-pin — stage + citizen apply.

Never merges live ``local/roster.json`` on the stage path. Apply is the
citizen gate: auto ``.bak`` then pin-field merge only, with stale-from
refusal. Policy envelope is the sole authority for allowed model pairs
and fields (``capacity_policy``).

Host-neutral: staged path under the data dir; desk URL from env when
dropping the For You card. Hermetic under pytest.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import capacity
from .capacity_policy import (
    CapacityPolicy,
    CapacityPolicyError,
    load_capacity_policy,
)
from .roster import Roster, RosterError, Worker, load as load_roster

SCHEMA_ID = "workforce.roster_diff/v1"
MODE_B = "B"
STAGED_SUBDIR = "staged"
APPLIED_SUBDIR = os.path.join(STAGED_SUBDIR, "applied")

# Conventional argv shapes when runtime (CLI basename) changes with model.
# Host-neutral templates — no desk/workplace paths.
_RUNTIME_COMMANDS = {
    "claude": [
        "claude",
        "--model",
        "{model}",
        "-p",
        "{prompt_text}",
        "--dangerously-skip-permissions",
    ],
    "grok": [
        "grok",
        "--prompt-file",
        "{prompt_path}",
        "--always-approve",
        "--output-format",
        "plain",
    ],
    "codex": [
        "codex",
        "exec",
        "--full-auto",
        "{prompt_text}",
    ],
}


class RepinError(ValueError):
    """Staging or apply refused (policy, stale from, missing worker, …)."""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_compact(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y%m%dT%H%M%SZ")


def _iso_z(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_str(when: Optional[datetime.datetime] = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%d")


def staged_dir(local_root: str) -> str:
    return os.path.join(local_root, STAGED_SUBDIR)


def applied_dir(local_root: str) -> str:
    return os.path.join(local_root, APPLIED_SUBDIR)


def _iter_staged_diffs(local_root: str) -> Iterator[Tuple[str, dict]]:
    """Yield (filename, data) for each readable roster-diff-*.json in staged_dir."""
    root = staged_dir(local_root)
    if not os.path.isdir(root):
        return
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith("roster-diff-") or not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        yield name, data


def default_command_for_runtime(runtime: str) -> Optional[List[str]]:
    """Return a conventional command argv for a runtime basename, or None."""
    key = (runtime or "").strip().lower()
    tpl = _RUNTIME_COMMANDS.get(key)
    return list(tpl) if tpl else None


def command_runtime(command: Sequence[str]) -> str:
    if not command:
        return ""
    return os.path.basename(command[0] or "")


def inbox_key_for_stage(stage_id: str) -> str:
    safe = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in (stage_id or "unknown")
    )
    return "repin-%s" % (safe or "unknown")


def inbox_label(project: str, stage_id: str, day: Optional[str] = None) -> str:
    day = day or _day_str()
    return "inbox-report:%s:%s:%s" % (project, inbox_key_for_stage(stage_id), day)


def _atomic_write_json(path: str, raw: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".roster-diff-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_diff(path: str) -> Dict[str, Any]:
    """Load and shallow-validate a staged roster-diff file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RepinError("cannot read staged diff %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise RepinError("staged diff root must be a JSON object")
    if data.get("schema") != SCHEMA_ID:
        raise RepinError(
            "unsupported schema %r (want %r)" % (data.get("schema"), SCHEMA_ID)
        )
    if data.get("mode") != MODE_B:
        raise RepinError(
            "unsupported mode %r — Mode A is not ratified; only mode %r"
            % (data.get("mode"), MODE_B)
        )
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes:
        raise RepinError("staged diff has no changes")
    return data


def _field_from_to(value: Any, label: str) -> Tuple[Any, Any]:
    if not isinstance(value, dict):
        raise RepinError("%s must be an object with from/to" % label)
    if "from" not in value or "to" not in value:
        raise RepinError("%s must include from and to" % label)
    return value["from"], value["to"]


def validate_diff_against_policy(
    diff: Dict[str, Any],
    policy: CapacityPolicy,
) -> None:
    """Refuse diffs outside the Mode B envelope (fields + model transitions)."""
    for i, ch in enumerate(diff.get("changes") or []):
        label = "changes[%d]" % i
        if not isinstance(ch, dict):
            raise RepinError("%s must be an object" % label)
        worker = ch.get("worker")
        if not isinstance(worker, str) or not worker.strip():
            raise RepinError("%s.worker must be a non-empty string" % label)
        fields = ch.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise RepinError("%s.fields must be a non-empty object" % label)
        for fname, fval in fields.items():
            if not policy.allows_field(fname):
                raise RepinError(
                    "%s field %r not in policy allowed_fields %s"
                    % (label, fname, list(policy.allowed_fields))
                )
            _field_from_to(fval, "%s.fields.%s" % (label, fname))
        if "model" in fields:
            fro, to = _field_from_to(fields["model"], "%s.fields.model" % label)
            if not isinstance(fro, str) or not isinstance(to, str):
                raise RepinError("%s.fields.model from/to must be strings" % label)
            if not policy.is_allowed_transition(fro, to):
                raise RepinError(
                    "model transition %r → %r not in capacity policy pin_pairs"
                    % (fro, to)
                )


def _endpoint_runtime_for_model(policy: CapacityPolicy, model: str) -> str:
    for pair in policy.pin_pairs:
        if pair.a.model == model and pair.a.runtime:
            return pair.a.runtime
        if pair.b.model == model and pair.b.runtime:
            return pair.b.runtime
    return ""


def plan_worker_fields(
    worker: Worker,
    to_model: str,
    policy: CapacityPolicy,
) -> Dict[str, Dict[str, Any]]:
    """Build from/to field map for one seat, or raise RepinError."""
    from_model = (worker.model or "").strip()
    to_model = (to_model or "").strip()
    if not to_model:
        raise RepinError("target model is empty")
    if not from_model:
        raise RepinError(
            "worker %r has empty model pin — set a current model before re-pin"
            % worker.name
        )
    if from_model == to_model:
        raise RepinError(
            "worker %r already pinned to %r" % (worker.name, to_model)
        )
    if not policy.is_allowed_transition(from_model, to_model):
        raise RepinError(
            "model transition %r → %r for %r not allowed by capacity policy"
            % (from_model, to_model, worker.name)
        )

    fields: Dict[str, Dict[str, Any]] = {
        "model": {"from": from_model, "to": to_model},
    }

    if policy.allows_field("command"):
        from_rt = command_runtime(worker.command)
        to_rt = _endpoint_runtime_for_model(policy, to_model) or from_rt
        if to_rt and to_rt != from_rt:
            new_cmd = default_command_for_runtime(to_rt)
            if new_cmd is None:
                raise RepinError(
                    "no command template for runtime %r (worker %r)"
                    % (to_rt, worker.name)
                )
            if list(worker.command) != new_cmd:
                fields["command"] = {
                    "from": list(worker.command),
                    "to": new_cmd,
                }
    return fields


def _count_seats_staged_today(
    local_root: str,
    day: str,
    exclude_workers: Optional[Sequence[str]] = None,
) -> int:
    """Count unique workers appearing in Mode B staged files for ``day``."""
    exclude = set(exclude_workers or ())
    seen: set = set()
    day_compact = day.replace("-", "")
    for name, data in _iter_staged_diffs(local_root):
        created = str(data.get("created_at") or "")
        on_day = created.startswith(day)
        if not on_day and len(name) >= 20:
            # filename: roster-diff-YYYYMMDDTHHMMSSZ.json
            on_day = name[12:20] == day_compact
        if not on_day:
            continue
        for ch in data.get("changes") or []:
            if not isinstance(ch, dict):
                continue
            w = ch.get("worker")
            if isinstance(w, str) and w.strip() and w not in exclude:
                seen.add(w.strip())
    return len(seen)


def _last_stage_ts_for_worker(local_root: str, worker: str) -> Optional[datetime.datetime]:
    latest: Optional[datetime.datetime] = None
    for _name, data in _iter_staged_diffs(local_root):
        hit = any(
            isinstance(ch, dict) and ch.get("worker") == worker
            for ch in data.get("changes") or []
        )
        if not hit:
            continue
        created = str(data.get("created_at") or "")
        try:
            ts = datetime.datetime.strptime(
                created, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def stage_repin(
    roster: Roster,
    local_root: str,
    worker_names: Sequence[str],
    to_model: str,
    *,
    policy: Optional[CapacityPolicy] = None,
    policy_path: Optional[str] = None,
    created_by: str = "chief-of-staff",
    reason: str = "",
    source: str = "",
    when: Optional[datetime.datetime] = None,
    enforce_caps: bool = True,
) -> Dict[str, Any]:
    """Validate + write one staged roster-diff. Does not touch live roster.

    Returns dict with path, stage_id, diff, seats_today.
    """
    when = when or _utcnow()
    day = _day_str(when)
    if policy is None:
        try:
            policy = load_capacity_policy(
                path=policy_path, local_root=local_root, required=True,
            )
        except CapacityPolicyError as exc:
            raise RepinError(str(exc)) from exc
    assert policy is not None

    names = [n.strip() for n in worker_names if n and n.strip()]
    if not names:
        raise RepinError("stage needs at least one worker")
    # de-dupe preserve order
    seen_n: set = set()
    ordered: List[str] = []
    for n in names:
        if n not in seen_n:
            seen_n.add(n)
            ordered.append(n)

    changes: List[Dict[str, Any]] = []
    for name in ordered:
        try:
            w = roster.worker(name)
        except RosterError as exc:
            raise RepinError(str(exc)) from exc
        fields = plan_worker_fields(w, to_model, policy)
        changes.append({"worker": name, "fields": fields})

    if enforce_caps:
        already = _count_seats_staged_today(local_root, day)
        if already + len(changes) > policy.seats_per_day:
            raise RepinError(
                "seats_per_day cap %d would be exceeded (already staged today: %d, "
                "this stage: %d)"
                % (policy.seats_per_day, already, len(changes))
            )
        if policy.cooldown_hours > 0:
            for name in ordered:
                last = _last_stage_ts_for_worker(local_root, name)
                if last is None:
                    continue
                delta = when - last
                need = datetime.timedelta(hours=policy.cooldown_hours)
                if delta < need:
                    raise RepinError(
                        "worker %r still in cooldown (last staged %s, need %dh)"
                        % (name, last.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           policy.cooldown_hours)
                    )

    stage_id = _utc_compact(when)
    policy_ref = os.path.basename(policy.source_path) if policy.source_path else (
        os.path.basename(policy_path) if policy_path else "capacity_policy.json"
    )
    diff: Dict[str, Any] = {
        "schema": SCHEMA_ID,
        "mode": MODE_B,
        "created_at": _iso_z(when),
        "created_by": (created_by or "chief-of-staff").strip(),
        "reason": (reason or "").strip(),
        "source": (source or "").strip(),
        "policy_ref": policy_ref,
        "stage_id": stage_id,
        "changes": changes,
    }
    validate_diff_against_policy(diff, policy)

    out_dir = staged_dir(local_root)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "roster-diff-%s.json" % stage_id)
    if os.path.exists(path):
        # Same-second collision — suffix once.
        path = os.path.join(
            out_dir, "roster-diff-%s-%s.json" % (stage_id, ordered[0])
        )
    _atomic_write_json(path, diff)
    seats_today = _count_seats_staged_today(local_root, day)
    return {
        "ok": True,
        "path": path,
        "stage_id": stage_id,
        "diff": diff,
        "seats_today": seats_today,
        "day": day,
        "policy_path": policy.source_path,
    }


def drop_repin_for_you(
    stage_result: Dict[str, Any],
    *,
    desk: str = "",
    author: str = "you",
    dry_run: bool = True,
    project: str = "workforce",
    city_rel_path: str = "",
) -> dict:
    """Create/refresh one human-gated For You card for a staged re-pin.

    Idempotent on ``inbox_label`` for stage_id/day.
    Hermetic under pytest / WORKFORCE_NO_DESK.
    """
    path = stage_result.get("path") or ""
    stage_id = stage_result.get("stage_id") or "unknown"
    day = stage_result.get("day") or _day_str()
    diff = stage_result.get("diff") or {}
    changes = diff.get("changes") or []
    lines = []
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        w = ch.get("worker")
        fields = ch.get("fields") or {}
        model = fields.get("model") or {}
        lines.append(
            "- **%s**: model `%s` → `%s`"
            % (w, model.get("from"), model.get("to"))
        )
        if "command" in fields:
            cmd_to = (fields["command"] or {}).get("to") or []
            runtime = command_runtime(cmd_to) if isinstance(cmd_to, list) else ""
            if runtime:
                lines.append("  - command runtime → `%s`" % runtime)
    summary = "\n".join(lines) if lines else "_no changes_"
    rel = city_rel_path or path
    key = inbox_key_for_stage(stage_id)
    label = inbox_label(project, stage_id, day)
    reason = (diff.get("reason") or "").strip()
    glance = (
        "Mode B re-pin staged (%d seat%s). Review diff then apply."
        % (len(changes), "" if len(changes) == 1 else "s")
    )
    if reason:
        glance = glance + " Reason: %s" % reason
    title = "Inbox · Re-pin · Mode B stage %s · %s" % (stage_id, day)
    description = (
        "**Staged diff:** `%s`\n"
        "**Date:** %s · **Key:** `%s`\n"
        "**Created by:** %s\n\n"
        "## Glance\n\n"
        "%s\n\n"
        "## Changes\n\n"
        "%s\n\n"
        "## Where\n\n"
        "`%s`\n\n"
        "## Apply (citizen)\n\n"
        "```\n"
        "python3 -m workforce repin --apply %s\n"
        "```\n\n"
        "Apply writes `local/roster.json.bak-repin-<UTC>` then merges pin "
        "fields only. Hands never write the live roster (STAFFING §5).\n\n"
        "## Done when\n\n"
        "- Citizen applies or discards the staged file\n"
        "- `workforce roster` / `capacity` / `doctor` clean after apply\n"
        % (
            rel,
            day,
            key,
            diff.get("created_by") or "—",
            glance,
            summary,
            rel,
            path,
        )
    )
    labels = [
        "worker:you",
        "you:todo",
        "inbox-report",
        label,
        "product:%s" % project,
        "capacity",
        "repin",
        "for-you",
    ]
    receipt = {
        "ok": True,
        "project": project,
        "key": key,
        "day": day,
        "label": label,
        "stage_id": stage_id,
        "action": "none",
        "path": rel,
    }
    hermetic_block = (not dry_run) and (not capacity.desk_writes_allowed())
    if hermetic_block:
        dry_run = True
        receipt["hermetic"] = True
    if dry_run:
        receipt["action"] = "would_create"
        receipt["title"] = title
        return receipt

    desk = (desk or capacity.DEFAULT_DESK).rstrip("/")
    existing = capacity.find_open_by_label(desk, project, label)
    if existing:
        tid = str(existing.get("id") or "")
        body = {
            "title": title[:200],
            "description": description,
            "gate_type": "human",
            "gate_note": "Mode B re-pin staged — review + apply",
            "priority": 1,
        }
        out = capacity._req(
            "PATCH",
            "%s/api/admin/tasks/%s?product=%s"
            % (desk, urllib.parse.quote(tid), project),
            body,
        )
        receipt["action"] = "updated"
        receipt["task_id"] = (out.get("task") or existing).get("id")
        receipt["api"] = out
        return receipt

    create_body = {
        "title": title[:200],
        "description": description,
        "author": author,
        "labels": labels,
        "priority": 1,
        "intake": "workforce-repin",
        "project": project,
    }
    out = capacity._req(
        "POST",
        "%s/api/admin/tasks?product=%s" % (desk, project),
        create_body,
    )
    task = out.get("task") or {}
    tid = str(task.get("id") or "")
    if not tid:
        receipt["ok"] = False
        receipt["error"] = out.get("error") or out
        receipt["action"] = "create_failed"
        return receipt
    gout = capacity._req(
        "PATCH",
        "%s/api/admin/tasks/%s?product=%s"
        % (desk, urllib.parse.quote(tid), project),
        {
            "gate_type": "human",
            "gate_note": "Mode B re-pin staged — review + apply",
            "priority": 1,
        },
    )
    receipt["action"] = "created"
    receipt["task_id"] = tid
    receipt["api"] = {"create": out, "gate": gout}
    return receipt


def _values_equal(a: Any, b: Any) -> bool:
    """Structural equality for from-checks (lists compare element-wise)."""
    if isinstance(a, list) and isinstance(b, list):
        return list(a) == list(b)
    return a == b


def apply_repin(
    diff_path: str,
    *,
    roster_path: Optional[str] = None,
    local_root: Optional[str] = None,
    policy: Optional[CapacityPolicy] = None,
    policy_path: Optional[str] = None,
    base: Optional[str] = None,
    bak_tag: str = "repin",
    when: Optional[datetime.datetime] = None,
    move_applied: bool = True,
) -> Dict[str, Any]:
    """Citizen apply: .bak live roster, merge pin fields, refuse stale from.

    Returns dict with bak_path, roster_path, applied workers.
    """
    when = when or _utcnow()
    diff = load_diff(diff_path)
    base = base or os.getcwd()
    if local_root is None:
        # Prefer roster parent/local when path known later; default base/local.
        local_root = os.path.join(base, "local")

    if policy is None:
        # Prefer policy next to live data; fall back to example only if missing
        # would fail — required=True for apply (policy_ref must still match envelope).
        try:
            policy = load_capacity_policy(
                path=policy_path, local_root=local_root, required=True,
            )
        except CapacityPolicyError as exc:
            raise RepinError(str(exc)) from exc
    assert policy is not None
    validate_diff_against_policy(diff, policy)

    r = load_roster(path=roster_path, base=base)
    roster_file = r.path
    # Re-read raw JSON so we only patch listed fields (preserve unknown keys
    # and ordering-friendly dump).
    try:
        with open(roster_file, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RepinError("cannot read roster %s: %s" % (roster_file, exc)) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("workers"), dict):
        raise RepinError("roster %s has no workers object" % roster_file)

    applied: List[str] = []
    for i, ch in enumerate(diff.get("changes") or []):
        name = ch["worker"]
        if name not in raw["workers"]:
            raise RepinError("apply: worker %r not in live roster" % name)
        if name not in r.workers:
            raise RepinError("apply: worker %r failed roster load validation" % name)
        live_w = r.workers[name]
        spec = raw["workers"][name]
        if not isinstance(spec, dict):
            raise RepinError("apply: worker %r spec is not an object" % name)
        fields = ch["fields"]
        for fname, fval in fields.items():
            fro, to = _field_from_to(fval, "changes[%d].fields.%s" % (i, fname))
            if fname == "model":
                live_val = (live_w.model or "").strip()
            elif fname == "command":
                live_val = list(live_w.command)
            else:
                live_val = spec.get(fname)
            if not _values_equal(live_val, fro):
                raise RepinError(
                    "stale stage for %r field %r: live %r != staged from %r"
                    % (name, fname, live_val, fro)
                )
            spec[fname] = to
        applied.append(name)

    # Backup first, then atomic replace.
    ts = _utc_compact(when)
    bak_path = "%s.bak-%s-%s" % (roster_file, bak_tag, ts)
    try:
        shutil.copy2(roster_file, bak_path)
    except OSError as exc:
        raise RepinError("cannot write backup %s: %s" % (bak_path, exc)) from exc

    _atomic_write_json(roster_file, raw)

    # Reload to ensure the merged roster still validates.
    try:
        load_roster(path=roster_file, base=base)
    except RosterError as exc:
        # Best-effort restore from bak
        try:
            shutil.copy2(bak_path, roster_file)
        except OSError:
            pass
        raise RepinError(
            "apply produced unloadable roster; restored from bak if possible: %s"
            % exc
        ) from exc

    applied_path = ""
    if move_applied:
        dest_dir = applied_dir(local_root)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(diff_path))
        try:
            shutil.move(diff_path, dest)
            applied_path = dest
        except OSError:
            applied_path = ""  # leave staged file in place as receipt

    return {
        "ok": True,
        "roster_path": roster_file,
        "bak_path": bak_path,
        "applied": applied,
        "applied_diff_path": applied_path or diff_path,
        "stage_id": diff.get("stage_id") or "",
    }
