# Run WorkForce

Build a wheel from the selected source revision and install it into an isolated release environment. Run the installed `workforce` command with `WORKFORCE_DATA_DIR` set to the existing WorkForce home. That directory contains `local/`; do not point the variable at `local/` itself.

The host service should set an explicit executable, data directory, PATH, and optional `SUITE_URL`. `WORKFORCE_API_ONLY=1` keeps the engine separate from the BluePrint interface. `workforce daemon` starts scheduling and the engine API; use `workforce doctor` to inspect configuration before dispatch.

Before an update, inspect `local/daemon.json` and the engine for active flights. Let active work finish. Stage the replacement outside the source checkout, test it with a disposable data directory, then stop and replace the service executable. Verify the new PID, fresh heartbeat, unchanged roster, API response, and installed package version. If activation fails, restore the previous service definition and installed environment. Existing runtime data stays in place.

Changes to an agent's provider, scope, credentials, or schedule are host configuration changes. Starting BluePrint must not silently hire or reseed agents. Empty queues stop; no-op commands must not be described as healthy workers.

Run package tests with `python -m pytest tests -q`. Live dispatch is not a package smoke test.

## Manual implementation workers

A `manual` (or other non-cron) schedule requires explicit `dispatch` or `fire_now`. Ticket events and route wakes do not start these workers. This is suitable for proving one authenticated implementation loop before enabling recurring work. A cron schedule opts into automatic clock and queue-event dispatch.

Record a dedicated identity and project queue, an isolated checkout, provider authentication, allowed writes, wall-clock budget, empty/fault behavior, and result evidence. CLI discovery is not authentication; a START or CANDIDATE ledger row is not proof of a WorkLane claim. Verify the signed WorkLane Owner record and delivery before calling a run successful. A local CLI calling a hosted model is local execution, not a remote host.

## Reusable task preparation

Use `python -m workforce.task_runner --config /absolute/path/runner.json` as a
registered worker command when each task needs its own branch. Keep the config
and prompt outside distributed source. WorkForce still owns dispatch and budgets;
the helper prepares one task and replaces itself with the configured provider.

Required configuration: `project`, `worker`, `desk_url` (HTTP origin),
`required_label` (explicit eligibility such as `execution:bounded`), `repository`,
`expected_remote` (exact fetch and push URL), `state_dir`, `prompt_template`,
`authority_chain` (nonempty absolute file paths), and `command` (argv list).
Optional `remote` defaults to `origin`; `base_ref` defaults to `origin/main`.
Optional `auth_check` is an argv list whose output is suppressed. Authentication
failure stops preparation. The operator refreshes the reviewed base ref before
dispatch; the helper never fetches, switches the primary checkout, or merges.

The prompt and argv accept `{project}`, `{worker}`, `{task_id}`, `{branch}`,
`{checkout}`, `{git_common_dir}`, `{result}`, and `{authority}`. The argv also
accepts `{prompt}` and `{prompt_file}`. Substitution is a single pass without a
shell. Configure the provider's real sandbox, dedicated WorkLane MCP identity,
tool permissions and result option in argv; this helper is not a sandbox.

Only the selected worker's ready feed is considered. A foreign assignment,
malformed/truncated feed or changed repository destination fails closed. An
empty eligible feed stops without launching a provider. Each selected task
reserves `state_dir/worker/task-id`, creates a separate checkout on
`workforce/task/worker/task-id`, and retains a preparation receipt and prompt.
The receipt explicitly says `claimed: false`: the provider must reread the order
and successfully claim it through WorkLane before implementation. A candidate
can lose eligibility between preparation and claim; that must stop the provider.

A repeat dispatch refuses the existing reservation, even if the tree is clean.
An operator must inspect WorkLane ownership, result, branch and dirty/ignored
files before recovery. Never remove the reservation to hide a failure. No
automatic retry, cross-provider takeover, review acceptance or deployment is
established by a prepared checkout or a zero provider exit. Preserve the PR-stage
handoff and record completion on the same order after actual review/acceptance.
