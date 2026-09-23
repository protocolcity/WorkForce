"""Bind automatic routing evidence to the configuration that actually executes.

Host-authored policy hashes attest the runner command, model/tool settings and
registered worker configuration observed during qualification. A changed setting
requires refreshed evidence; this module never guesses vendor command syntax.
"""
import dataclasses
import hashlib
import json
import math
from pathlib import Path

from . import routing_policy, task_routing

CONTEXT_ENV = 'WORKFORCE_ROUTING_CONTEXT'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def runner_digest(config):
    return digest({k: v for k, v in config.items()
                   if k not in ('routing_policy', 'routing_host')})


def worker_digest(worker):
    return digest(dataclasses.asdict(worker))


def task_digest(task):
    # Comments and observation timestamps are not executable requirements.
    return digest(dict(product=task.get('product') or task.get('project'), **{k: task.get(k) for k in ('id', 'status',
                   'labels', 'gate_type', 'gate_until', 'host', 'required_tools',
                   'title', 'description', 'acceptance')}))


def configured_runner(worker):
    argv = worker.command
    if 'workforce.task_runner' not in argv or argv.count('--config') != 1:
        raise ValueError('automatic routing requires the verified task_runner adapter')
    index = argv.index('--config') + 1
    if index >= len(argv) or not Path(argv[index]).is_absolute():
        raise ValueError('runner configuration must be an absolute path')
    config = json.loads(Path(argv[index]).read_text())
    if config.get('worker') != worker.name or worker.identity != worker.name:
        raise ValueError('registered and configured runner identities differ')
    return config


def bound_seat(policy_path, config, worker=None):
    raw = json.loads(Path(policy_path).read_text())
    policy = routing_policy.parse_routing_policy(raw)
    rows = [r for r in raw['seats'] if isinstance(r, dict)
            and r.get('worker') == config['worker']]
    if len(rows) != 1:
        raise ValueError('one unambiguous capability record per registered worker is required')
    row = rows[0]
    if row.get('runner_sha256') != runner_digest(config):
        raise ValueError('runner configuration differs from qualified command/model/tools')
    if worker is not None and row.get('worker_sha256') != worker_digest(worker):
        raise ValueError('registered worker changed since qualification')
    budget = row.get('max_run_units')
    if (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0):
        raise ValueError('a finite positive run budget is required')
    seats = [s for s in policy.seats if s.worker == config['worker']]
    if len(seats) != 1 or seats[0].candidate.project != config['project']:
        raise ValueError('qualified project differs from configured runner')
    quota = seats[0].candidate.quota
    if (quota is None or quota.remaining is None or budget > quota.remaining
            or row.get('budget_units') != quota.units):
        raise ValueError('insufficient or incompatible quota for bounded run')
    return policy, seats[0]


def qualify(policy_path, host, config, task, worker=None):
    policy, seat = bound_seat(policy_path, config, worker)
    receipt = task_routing.route_task(task, [seat], policy.results_by_candidate, host=host)
    if not receipt.accepted or receipt.recommendation is None:
        raise ValueError('automatic routing refused: ' + receipt.reason)
    if receipt.recommendation.get('candidate_id') != seat.candidate.candidate_id():
        raise ValueError('selected candidate differs from executing seat')
    return receipt.to_dict()
