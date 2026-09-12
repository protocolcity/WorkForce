# WorkForce architecture

WorkForce owns roster identities, contracts, schedules, dispatch controls, budgets, process locks, and shift history. WorkLane supplies optional work queues. BluePrint reads execution evidence and presents controls through the engine. Repository activity is delivery evidence, not proof that an agent is running.

The daemon schedules registered workers and hosts the engine API. The dispatch engine validates the worker, checks its queue and budget, acquires a single-flight lock, runs the configured command, and records its outcome. A job may run a deterministic script; a lane requires an actual provider command and a correctly scoped work queue. A placeholder command is not operational coverage.

The distributable package contains code only. `WORKFORCE_DATA_DIR` selects the host directory containing `local/roster.json`, locks, ledgers, reports, and heartbeat state. The package working directory must not determine which workspace runs. Updates replace an installed environment while retaining this data directory.

Canonical source is protocolcity/WorkForce. Former private histories are references for reconciliation; do not merge or publish them wholesale. Host addresses, credentials, worktrees, and customer papers do not belong in distributable source.

See [RUNNING.md](RUNNING.md) for installation and lifecycle checks.
