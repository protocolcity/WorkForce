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

### Explicit recovery

`--recover-receipt /absolute/path/preparation.json --recovery-reason "..."`
resumes a preserved reservation instead of preparing a new task. Both flags are
required together; there is no automatic or unattended recovery path. The
operator, not the helper, first releases or reassigns the task in WorkLane —
recovery only proceeds once the current authoritative ready feed shows the
task backlog, ungated, and labeled for the configured worker. A gated, done,
foreign, or wrong-owner task is refused with no WorkLane writes.

Recovery reuses the exact preserved checkout, worktree, and branch verified
against the repository's registered worktrees and the authorized remote; it
never fabricates or resets a checkout, and dirty files, commits, and the
original preparation receipt, prompt, and result are left untouched. Each
recovery attempt gets its own numbered `attempts/N/` directory under the
existing reservation with a fresh prompt, result path, and receipt (marked
`state: "recovered"` with `recovery_of` and `recovery_reason`); it never
overwrites earlier evidence.

A per-reservation OS lock (`state_dir/worker/task-id/lock`, held open across
the provider's exec) excludes a second start against the same reservation.
Because the lock is tied to the holding process's open file descriptor, the
kernel releases it automatically if that process dies for any reason —
recovery never infers liveness from a PID or its age, and refuses to proceed
while the lock is held. This requires POSIX advisory locking (`fcntl.flock`);
on a platform without it, launching fails closed with an explicit error
instead of the module crashing at import time or launching unlocked.

`--recover-receipt` may point at the original `preparation.json` or at any
later `attempts/N/preparation.json`; either way, recovery resolves the single
canonical reservation root first and reads the original receipt there, so
every attempt and every worker always shares the exact same lock. A receipt
can never define its own separate lock scope by nesting.

Every receipt records a `lock_protocol` marker. A canonical receipt written
before that marker existed never held the reservation lock in the first
place, so its absence or an unlocked lock file proves nothing about whether
its process is still running. Recovering such a legacy receipt additionally
requires `--legacy-stop-evidence "..."`: an explicit, retained operator
statement of how they confirmed the prior process stopped. That statement is
recorded on the new attempt's receipt; recovery still makes no WorkLane
writes and never overwrites the canonical original.

Recovery to a different worker (handoff) is allowed only when the operator has
already reassigned the `worker:` label in WorkLane to that worker and that
worker's own config/identity requests the recovery; the new worker signs its
own WorkLane claim before any writes. The target checkout is always the one
in the canonical original receipt — never an arbitrary path or foreign
repository.
