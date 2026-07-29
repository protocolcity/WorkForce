# WorkForce

> **Pre-release (0.1.x).** Part of the **ProtocolCity** suite with
> [WorkLane](https://github.com/protocolcity/ProtocolCity-WorkLane) and
> [BluePrint](https://github.com/protocolcity/ProtocolCity-BluePrint).
> Expect sharp edges; file issues.

**Employment infrastructure for agents.**

WorkForce turns AI agents into a staffed workforce. Each worker has identity
papers (a contract and a prompt), a schedule, a budget, and a tamper-evident
shift ledger. One daemon dispatches every shift; one board — the **Roster** —
shows you who is employed, who is on the floor, and what every shift cost.

## Install the whole suite (recommended)

```bash
brew install protocolcity/tap/blueprint
blueprint setup ~/my-city
blueprint serve --root ~/my-city --with-engines
# → http://127.0.0.1:8801/  (Map · Desk · Roster)
```

## WorkForce alone

```bash
pip install protocolcity-workforce

workforce roster                     # who is employed
workforce dispatch <worker> --dry-run   # rehearse one shift, spend nothing
workforce ledger <worker>            # the shift record
workforce daemon                     # the scheduler — one service, serves the board
workforce open                       # open the Roster board in your browser
workforce-mcp                        # stdio MCP for chat agents
```

### MCP (chat agents)

```bash
# Point at a city roster (BluePrint path):
export WORKFORCE_ROSTER=~/my-city/.protocolcity/workforce/local/roster.json
# or: export WORKFORCE_DATA_DIR=~/my-city/.protocolcity/workforce
python -m workforce.mcp --author you
# tools: wf_status · wf_roster · wf_show · wf_hire · wf_dispatch
```

Wire next to WorkLane MCP (`python -m worklane.mcp` / `worklane-mcp`)
so agents can hire and dispatch as well as file work orders. Prefer
`dry_run=true` on hire/dispatch until you trust the seat.

Part of the [ProtocolCity](https://github.com/protocolcity) suite: pairs
naturally with **WorkLane** (the ticket desk your workers pull work from),
and runs standalone against any queue that can answer a ready-count URL.
Everything is local-first — the roster is a JSON file on your machine, the
ledger is an append-only record, and there is no cloud dependency.

## Quickstart (same as alone)

```
pip install protocolcity-workforce

workforce roster
workforce dispatch <worker> --dry-run
workforce ledger <worker>
workforce daemon
workforce open
```

The board serves at `http://127.0.0.1:8797` by default.

## How it works

- **The roster is data.** Start from [`roster.example.json`](roster.example.json)
  and keep your real roster at `local/roster.json` (gitignored) or point
  `$WORKFORCE_ROSTER` at it. One row per worker: identity, contract path,
  model, cron schedule, budget, queue URL.
- **Workers claim tickets, then work them.** A worker is bound to one
  working directory and pulls from its queue — a WorkLane ready-endpoint
  works out of the box, but any URL that returns a count will do. Empty
  queue means the shift is skipped, not billed.
- **Every shift lands in the ledger.** Outcome, passes, tokens in/out, and
  cost are appended per shift — the record your budgets and reports read.
- **Schedules are data, not services.** The daemon is the only OS service
  you run (`workforce daemon-plist` prints a launchd agent for macOS);
  hiring, pausing, or rescheduling a worker is a roster edit, not a deploy.

## Configuration

All configuration is optional — WorkForce works out of the box from the repo
root with no env vars set. For installed-package deployments or
multi-directory setups, use these variables:

| Variable | Default | Description |
|---|---|---|
| `WORKFORCE_DATA_DIR` | `./local` (CWD) | Home directory for WorkForce runtime state (roster, ledger, daemon heartbeat). Set this when running `workforce` commands from outside the repo root so the daemon and board always find their data. |
| `WORKFORCE_PORT` | `8797` | HTTP port for `workforce board` and `workforce daemon`. |
| `WORKFORCE_ROSTER` | `$WORKFORCE_DATA_DIR/local/roster.json` | Explicit roster path — overrides the default search under `WORKFORCE_DATA_DIR`. |
| `WORKFORCE_DESK` | `http://127.0.0.1:8799` | WorkLane / Desk API URL used by the board's activity join. |
| `WORKFORCE_CITYHALL` | `http://127.0.0.1:8796` | City-hall API URL used by the board's city join. |
| `WORKFORCE_BRAND` | `city` | Board brand mode: `city` (ProtocolCity suite) or `standalone`. |
| `PROTOCOLCITY_TEMPLATES` | _(sibling checkout)_ | Path to ProtocolCity templates for `workforce hire --no-plant`. |

**Installed-package quickstart:**

```
export WORKFORCE_DATA_DIR=~/.workforce
mkdir -p "$WORKFORCE_DATA_DIR/local"
# copy roster.example.json → $WORKFORCE_DATA_DIR/local/roster.json and edit
workforce roster
workforce daemon
```

**launchd (macOS):** run `workforce daemon-plist` while `WORKFORCE_DATA_DIR`
is set and the plist will bake it into `EnvironmentVariables` automatically.

## Hiring a worker

```
workforce hire --help
```

Hiring writes the worker's papers (contract + prompt) and adds the roster
row. Contracts are the law a worker reads before every shift — a worker
whose contract says it is not armed will refuse to work, by design.

## Requirements

Python 3.9+. macOS and Linux. Agent CLIs (such as `claude`) are invoked per
shift using the command template in the roster row — bring whichever agent
vendor you employ.

## License

Apache-2.0. Copyright 2026 ProtocolCity.
