# Run WorkForce

Build a wheel from the selected source revision and install it into an isolated release environment. Run the installed `workforce` command with `WORKFORCE_DATA_DIR` set to the existing WorkForce home. That directory contains `local/`; do not point the variable at `local/` itself.

The host service should set an explicit executable, data directory, PATH, and optional `SUITE_URL`. `WORKFORCE_API_ONLY=1` keeps the engine separate from the BluePrint interface. `workforce daemon` starts scheduling and the engine API; use `workforce doctor` to inspect configuration before dispatch.

Before an update, inspect `local/daemon.json` and the engine for active flights. Let active work finish. Stage the replacement outside the source checkout, test it with a disposable data directory, then stop and replace the service executable. Verify the new PID, fresh heartbeat, unchanged roster, API response, and installed package version. If activation fails, restore the previous service definition and installed environment. Existing runtime data stays in place.

Changes to an agent's provider, scope, credentials, or schedule are host configuration changes. Starting BluePrint must not silently hire or reseed agents. Empty queues stop; no-op commands must not be described as healthy workers.

Run package tests with `python -m pytest tests -q`. Live dispatch is not a package smoke test.
