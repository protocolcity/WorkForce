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

The recovered attempt's prompt always carries the reason so the seat sees why
it was recovered instead of silently re-running the fresh-preparation prompt
(wf-257 — a recovery whose reason went unread ran a no-op shift). Rendering
passes `recovery_reason`, `recovery_attempt`, and `recovery_of` into the
template fields (empty strings on a fresh preparation, so a template that
references them never renders a literal placeholder), and a "Recovery: this
is recovery attempt N of \<task\>. Reason: \<text\>. Act on the reason before
anything else." block is always prepended to the rendered prompt when the
reason is non-empty — even for a host template with no `{recovery_reason}`
slot, so existing worker prompts benefit without editing.

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

### Recovery through the engine

Running `python -m workforce.task_runner --recover-receipt ... --recovery-reason
...` directly, as above, resumes the reservation but bypasses `engine.dispatch`:
the attempt gets no ledger START/STOP rows, no engine wall-clock budget, no
engine per-worker lock, and no `run/<worker>.out`, so the engine API, the
bounded supervisor, and BluePrint's open-shift view see the worker idle while a
provider is actually running. `workforce dispatch <worker> --recover-receipt
/absolute/path/preparation.json --recovery-reason "..."` (optionally with
`--legacy-stop-evidence "..."`) routes the identical recovery through
`engine.dispatch` instead: the two flags are appended to the worker's own
`command` argv (a task_runner-based worker command is exactly the invocation
above), and the shift's ledger START/CANDIDATE/STOP (or ERROR) rows are tagged
`recovery=1`; the CANDIDATE row also carries `reason` (first 80 chars of the
recovery reason) and `reason_sha` (its sha256, truncated) so the ledger names
why the recovery ran, not just that it did. The engine still holds its own
per-worker lock and enforces the worker's budget for the recovered attempt,
and still makes no WorkLane writes.
task_runner's own reservation lock and ready-eligibility re-check, run inside
the spawned subprocess, are unchanged — a second concurrent start (through the
engine or run directly) is still refused, and an ungated task still fails
closed. Direct `task_runner --recover-receipt` invocation, outside `workforce
dispatch`, remains available for an operator who is not ready to route through
the engine; it is simply not engine-visible.

A recovered shift through the engine is always a forced single pass: the
worker's own `max_passes` (drain or multi-pass) never re-spawns the recovery
argv a second time, and the forced ceiling is recorded on the START row
(`recovery_single_pass=1`). If the primary recovery attempt exits with a
vendor-limit signature and the worker has `fallback_runtime` set, the engine
does not fall back — a recovery targets one specific reservation, and a
silent runtime switch mid-recovery is not a lawful takeover; it logs
`ERROR reason="fallback skipped during recovery"` instead. The shift's
CANDIDATE evidence names only the task being resumed (read from the
canonical original receipt), not the live ready snapshot, which may still
list other backlog this attempt is not touching; dispatch refuses before
START (ERROR, no ledger writes) if the receipt is unreadable or resolves
outside the worker's configured `state_dir`.

## Generating a seat from a provider adapter

`workforce hire <name> --provider {claude,cursor,grok,codex} --project <slug>
--repository /absolute/path [--remote <url>] [--model <pin>] [--held]
[--dry-run]` writes the whole seat folder under the worker-config root —
`runner.json`, `launch.py`, `mcp.json`, `CONTRACT.md`, `prompt.md` — from the
named adapter, plus the roster row. Each adapter's command is checked against
its own `bypass_flags` list and refuses to generate one (`AdapterError`); no
generated command carries `--dangerously-skip-permissions`, `--force`/`--yolo`,
or an equivalent tool-permission bypass. The cursor adapter does emit `--trust`
— that flag only dismisses cursor-agent's interactive "trust this workspace?"
prompt, which is required for headless dispatch; it is never emitted without
`--sandbox enabled` immediately alongside it, which is the actual safety
boundary, so `--trust` is not in `cursor`'s `bypass_flags` list. `--dry-run`
prints the five file paths, the command, and the allow list without writing
anything or touching the roster.
`--held` clears the row's schedule so the desk shows the seat OFF (a bare
hire without `--held` gets a normal cron schedule); `fire_now`/manual dispatch
still work on a held seat. `--regenerate` rewrites an existing seat's folder,
moving the previous one aside to `<seat>.backup-<timestamp>` first, and keeps
the row's prior held state unless `--held`/`--no-held` is passed explicitly.
The model pin is validated against the current `CANONICAL_MODEL_IDS`
registry exactly as `workforce hire --workdir` does; shorthand ids are
rejected.

## Bounded AI supervisory pass

`python -m workforce.supervisor --config /absolute/path/supervisor.json` is a
single manual invocation, not a service: an explicit `local_root` (runtime
home), `roster_path`, `projects`/`workers` allowlists, `provider_argv`, a
`time_budget_secs`/`output_budget_bytes` pair, and `max_dispatch` are all
required. Three checks run before any provider call is even considered, each
able to end the pass with no model call and evidence `pass_outcome` set
accordingly: an optional absolute `stop_file` path — if that file exists at
pass start, the pass stops immediately (`pass_outcome: "stopped_by_operator"`,
exit 0) before state is even collected; a fresh snapshot of exactly the
configured manual (non-cron) lane workers — if no eligible worker row in it
is currently dispatchable (not busy, no monitoring flag, and a non-empty
ready-task-id list — the same criteria dispatch validation applies), the pass
stops without ever launching the provider (`pass_outcome:
"no_eligible_ready_work"`, exit 0; excluded workers, and any busy or
monitoring-flagged worker's own ready-task-id list, still appear in that
snapshot so an operator can see stale/busy state even though it did not
count toward eligibility); and an optional
`max_consecutive_provider_failures` (default 3) — before calling the
provider, the newest that-many evidence reports that actually reached the
provider (`provider_skipped` not set) are read, walking past any
`stopped_by_operator`/`no_eligible_ready_work`/`escalated_provider_failures`
skip report in between without breaking or counting it, ordered by filename
(timestamp-prefixed at write time), and if every one of those provider-
invoking reports recorded an explicit `provider_ok: false` (an
unreadable/malformed report among them counts as a failure too — fail
closed), the pass refuses to call the provider (`pass_outcome:
"escalated_provider_failures"`, exit 1, a reason on stderr) — an escalated
skip's own report is itself walked past by the next pass rather than treated
as ending the streak, so escalation latches until a real provider success.
`--acknowledge-provider-failures "reason"` lifts that refusal for one pass
only and records the reason as `provider_failure_acknowledgement` in that
pass's own evidence — there is no automatic reset of the streak. Fewer than
`max_consecutive_provider_failures` provider-invoking reports on disk, or any
one of the recent provider-invoking reports recording a success, means no
escalation. Every evidence report
carries `pass_outcome`, one of `no_eligible_ready_work` |
`stopped_by_operator` | `escalated_provider_failures` | `provider_failed` |
`proposed` | `dispatched` — the last two mean the provider was actually
called and returned or failed cleanly in inspect vs. execute mode
respectively.

Once past those checks, default mode is inspect/propose — it hands that same
snapshot to the configured provider as untrusted JSON over a byte-and-time-bounded pipe (the
bound is enforced during the read itself, and a timeout/over-budget cutoff
kills the provider's whole process group by its own pid — the provider is
launched with `start_new_session=True`, which makes that pid the process
group id by definition, so the kill never depends on looking up a leader
that may already have exited early while a descendant it forked lives on,
possibly still holding the inherited stdout pipe open; this module does not
and cannot claim that a generic provider argv has no tool access — that is
the operator's own configuration to trust or not), then
**re-fetches state again** after the provider returns and validates every
proposed `{worker, project}` action against that later snapshot. `engine.
dispatch` takes no task id — a worker always re-probes and works its own
authoritative ready feed in its own order — so an action is WORKER+PROJECT
scoped only; a fresh ready-task-id list is carried as context (proof real
work exists), never a binding promise about which task runs. Checks are:
worker/project allowlist membership, no duplicate WORKER across proposals in
the same pass, matching project, busy/lock state, recent-shift monitoring
flags, and at least one fresh eligible ready task. `--execute` re-checks
each validated worker **again, immediately before** its own dispatch call
(closing the gap since the last snapshot), then dispatches the *exact*
`Worker` object that recheck returned — never a second, independent roster
load that could observe a config changed in between. Because dispatch's
return code alone cannot distinguish a completed shift from a clean SKIP or
an explicit SCOPE_DENY/HOST_MUTATION_DENY refusal, the outcome is classified
from both the ledger rows that call itself wrote and its return code
(`attempted`/`started`/`completed`/`failed`, plus an `outcome` string) — a
denied refusal or any non-zero return code is always `failed` and can never
be `completed`, never from `len(results)`, and never assumed from mere
`CANDIDATE` row presence. Every pass writes exactly one evidence file under
`local/reports/supervisor/` with a unique, exclusively-created filename (a
timestamp alone can collide) with its own path embedded inside it; it never
closes WorkLane work and never invents a recovery — a stale/failed
monitoring flag must be resolved through the explicit preserved-reservation
recovery protocol above, not a fresh dispatch. The CLI exits non-zero on a
provider failure or any failed dispatch; it never reports success just
because the process reached exit.

`GET /api/supervisor` on the board port exposes read-only rows from those
evidence files (`passes`, newest-first, optional `?limit=`). It is a record
of past coordination passes only — not a running supervisor, not liveness,
and not a roster worker. `/api/report` includes a matching `supervisor`
summary for the same report window.
