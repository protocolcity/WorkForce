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


class PreparationError(RuntimeError):
    pass


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


def prepare(config, fetch=_fetch):
    """Return launch metadata, or None for an empty eligible feed; never write WL."""
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
    command = config["command"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise PreparationError("Provider command must be an argv list")
    repo = _path(config["repository"])
    state = _path(config["state_dir"])
    prompt_template = _path(config["prompt_template"]).read_text()
    authority = [_path(p) for p in config["authority_chain"]]
    if not authority:
        raise PreparationError("An explicit authority chain is required")
    authority_text = "\n\n".join(str(p) + "\n" + p.read_text() for p in authority)
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
            eligible.append(task)
    if not eligible:
        return None
    task = eligible[0]  # Preserve WorkLane's priority ordering.
    task_id = _slug(task["id"])
    remote = config.get("remote", "origin")
    if _git(repo, "remote", "get-url", remote) != config["expected_remote"]:
        raise PreparationError("Repository remote differs from authorized destination")
    if _git(repo, "remote", "get-url", "--push", remote) != config["expected_remote"]:
        raise PreparationError("Repository push destination differs from authorization")
    base = _git(repo, "rev-parse", "--verify", config.get("base_ref", "origin/main") + "^{commit}")
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    auth = config.get("auth_check")
    if auth:
        if not isinstance(auth, list) or not all(isinstance(x, str) for x in auth):
            raise PreparationError("auth_check must be an argv list")
        check = subprocess.run(auth, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if check.returncode:
            raise PreparationError("Provider authentication check failed")
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
               "state": "reserved", "claimed": False}
    receipt_path = reservation / "preparation.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        _git(repo, "worktree", "add", "-b", branch, str(checkout), base)
        fields = {"project": project, "worker": worker, "task_id": task_id,
                  "branch": branch, "checkout": str(checkout),
                  "git_common_dir": str(common), "result": str(reservation / "result.md"),
                  "authority": authority_text}
        prompt = _render(prompt_template, fields)
        prompt_path = reservation / "prompt.md"
        prompt_path.write_text(prompt)
        fields.update(prompt=prompt, prompt_file=str(prompt_path))
        argv = [_render(arg, fields) for arg in command]
        receipt.update(state="prepared", prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        return {"argv": argv, "checkout": str(checkout), "receipt": str(receipt_path),
                "project": project, "worker": worker, "task_id": task_id}
    except Exception:
        receipt["state"] = "preparation_failed"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(json.loads(Path(args.config).read_text()))
        if result is None:
            print("No eligible assigned work; stopped without launching a provider.")
            return 0
        print("Prepared %s; not yet claimed. Receipt: %s" % (result["task_id"], result["receipt"]), flush=True)
        os.chdir(result["checkout"])
        env = dict(os.environ)
        env.update(WL_AGENT_ID=result["worker"], TP_AGENT_ID=result["worker"],
                   WL_PROJECT=result["project"], TP_PROJECT=result["project"])
        # Replace this process, preserving WorkForce's budget and exit tracking.
        os.execvpe(result["argv"][0], result["argv"], env)
    except (PreparationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("Task preparation stopped: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
