# Changelog

## v0.1.7 — 2026-08-07

Post-0.1.6 stable: lane drain multipass, CoS digest upsert, max_fires_per_day, host-mutation guards, shift worktrees, three-type taxonomy, doctor routing repair, WorkLane rename scrub, efficiency passes.

## Unreleased

- **Three-tier worker taxonomy** — every worker now carries a derived
  `type`: `agent` (claims work orders from a ready feed), `staff` (shipped
  workspace seat), or `job` (scheduled duty). `/api/workers` exposes `type` +
  `staff`; `hire` accepts `--type agent|staff|job` (and `type=` on
  `POST /api/hire`) as the citizen-facing alias for kind/staff. Wire values
  are unchanged — `kind` stays `lane|job` and nothing new is persisted.

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
