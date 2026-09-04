# Changelog

## v0.1.8 — 2026-08-12

- **Script-tier preflight fix** — `shutil.which()` cannot resolve
  path-like commands such as `./scripts/run.sh` or `.venv/bin/python`, causing
  valid script-tier jobs to skip silently with "CLI not installed". Resolution
  now mirrors subprocess argv[0] rules: absolute paths checked with
  `os.path.isfile`; `os.sep`-containing paths resolved against workdir; bare
  names still use roster env PATH. Jobs that used relative or absolute
  entrypoints now run correctly. (**Headline fix** — no published engine
  carried this before 0.1.8.)
- **Three-tier worker taxonomy** — every worker now carries a derived
  `type`: `agent` (claims work orders from a ready feed), `staff` (shipped
  workspace seat), or `job` (scheduled duty). `/api/workers` exposes `type` +
  `staff`; `hire` accepts `--type agent|staff|job` (and `type=` on
  `POST /api/hire`) as the citizen-facing alias for kind/staff. Wire values
  are unchanged — `kind` stays `lane|job` and nothing new is persisted.
- **`doctor` improvements** — `--repair` is now bounded (clears terminal
  `needs:routing` residue); unlanded-shift checks emit unique patches and
  visit the primary worktree exactly once; land-it advice matches actual
  remote status rather than assuming a clean origin.
- **`host-audit --include-run-logs`** — bounded scan of run tails added to
  host audit.
- **PyPI package metadata** — `dependencies`, `license`, `urls`, and
  classifiers now populated in `pyproject.toml`.
- **CI** — GitHub Actions pytest workflow added; export seam ships.

## v0.1.7 — 2026-08-07

Post-0.1.6 stable: lane drain multipass, CoS digest upsert, max_fires_per_day, host-mutation guards, shift worktrees, three-type taxonomy, doctor routing repair, WorkLane rename scrub, efficiency passes.

## v0.1.6 — 2026-08-03

- **Event-driven dispatch** — `POST /api/wake {worker}`: WorkLane nudges
  a hand's lane on route events so a freshly seated work order fires within
  seconds instead of waiting for the next clock. Probe-first, single-flight safe,
  debounced; the clock fire remains the guaranteed fallback.
- **Adaptive idle backoff** — after the empty-run threshold, an idle
  lane's cadence stretches automatically (1h → 4h → daily heartbeat) and resets
  to base on any wake or non-empty probe. Opt out per seat with
  `empty_run_adaptive: false`; explicit `empty_run_backoff` pins still win.
- **Roster honesty** — `/api/workers` now reports `backoff_secs` + `resting` so
  the suite Map can show a resting lane truthfully.

## v0.1.5 — 2026-07-27
- **Persona rename** melanie → salem (Salem · Systems Engineer).

- **WorkForce MCP** — `wf_status` / `wf_roster` / `wf_show` / `wf_hire` / `wf_dispatch`.
- **Hire defaults** use `worker:` feed + exclusive `queue_url`.
- **Orphan lock reclaim** by pid (kill-9 no longer blocks redispatch).
- **Persona rename** otto → melanie (Systems Engineer).
- **Daemon plist harden** — ProcessType Background, AbandonProcessGroup, ThrottleInterval.
- **`workforce doctor`** + dual-home roster law (engine home authoritative).
- **Light scene** sentinel contract + generation_token tests.

## v0.1.4 — 2026-07-26

WorkForce MCP package surface and suite pairing.

All notable public changes. Feedback: open a GitHub issue and include the version.
