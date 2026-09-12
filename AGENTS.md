# WorkForce product instructions

WorkForce owns agent identities, contracts, schedules, dispatch, budgets, and execution history. WorkLane is the optional work-order authority; BluePrint is the optional interface. Business products remain independent.

Canonical repository: protocolcity/WorkForce. Maintain public-safe source directly here; host roster, credentials, logs, and customer operations remain outside product source. Host-specific authorization and publishing rules belong to the selected workspace.

Use Python3.9-compatible syntax. `local/` is runtime state and must survive package updates. Installed runtimes select their home with `WORKFORCE_DATA_DIR`, independent of their working directory. Never infer agent liveness from repository activity or stale heartbeat files.

Tests: `python -m pytest tests -q`. Tests use disposable runtime directories and disable live desk writes. Inspect active flights before a daemon update; do not interrupt an agent merely to simplify installation. Dispatch comes from registered roster configuration and the existing engine controls; do not invent employment records or bypass authentication/host protections.

Architecture references are the existing README, RUNNING and package modules. Root architecture and deployment references should describe the final source/runtime split and preserve compatibility with existing consumers.
