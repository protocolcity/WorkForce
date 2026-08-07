"""CLI — manual dispatch is a lawful loop (RUNNING.md); the daemon is the scheduler."""

import argparse
import datetime
import os
import sys

from . import engine, roster as roster_mod
from .ledger import Ledger


def _board_url(port=None) -> str:
    """The board's own door — localhost by construction (the daemon binds
    127.0.0.1). Not a host seam: this is WorkForce's own surface, not a
    worker's desk or workplace."""
    from . import board
    return "http://127.0.0.1:%d" % (port or board.DEFAULT_PORT)


def _command_present(cmd0: str) -> bool:
    """True if roster argv[0] is runnable.

    Absolute paths are checked on the path itself (isfile + X_OK) — venv-pinned
    seats often use an absolute interpreter whose basename is not on PATH.
    Bare names fall back to shutil.which against PATH.
    """
    import shutil

    if not cmd0:
        return True
    if os.path.isabs(cmd0):
        return os.path.isfile(cmd0) and os.access(cmd0, os.X_OK)
    return shutil.which(cmd0) is not None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="workforce",
                                     description="Employment infrastructure for agents.")
    parser.add_argument("--file", help="roster path (default: local/roster.json, roster.json, or $WORKFORCE_ROSTER)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("roster", help="list employed workers")

    p_dispatch = sub.add_parser("dispatch", help="run one shift for one worker")
    p_dispatch.add_argument("worker")
    p_dispatch.add_argument("--dry-run", action="store_true",
                            help="every step except spawning the vendor CLI")

    p_hire = sub.add_parser("hire", help="employ a worker (papers + roster row)")
    p_hire.add_argument("name", help="persona name (becomes the identity slug)")
    p_hire.add_argument("--workdir", required=True,
                        help="cabinet / neighborhood absolute path")
    p_hire.add_argument("--role", default="", help="role title (e.g. Market Analyst)")
    p_hire.add_argument("--display", default="", help="Persona · Role (optional)")
    p_hire.add_argument("--kind", choices=("lane", "job"), default="lane")
    p_hire.add_argument(
        "--type", choices=("agent", "staff", "job"), default="",
        help="citizen tier: agent=claims a ready feed · "
             "staff=shipped workspace seat · job=scheduled duty; "
             "maps onto kind/staff, conflicts rejected",
    )
    p_hire.add_argument("--identity", default="", help="signing id (default: slug of name)")
    p_hire.add_argument("--schedule", default="*/30 * * * *")
    p_hire.add_argument("--model", default="")
    p_hire.add_argument("--project", default="",
                        help="Desk project slug for the ready-queue probe")
    p_hire.add_argument("--queue-url", default="")
    p_hire.add_argument("--no-plant", action="store_true",
                        help="require existing CONTRACT.md + prompt.md")
    p_hire.add_argument("--force-papers", action="store_true",
                        help="overwrite existing papers from templates")
    p_hire.add_argument("--dry-run", action="store_true",
                        help="plant/check papers but do not write the roster")
    p_hire.add_argument(
        "--staff", action="store_true",
        help="force staff=true (Map Office staff bay); auto when workdir is "
             ".protocolcity/ops",
    )
    p_hire.add_argument(
        "--no-staff", action="store_true",
        help="force staff=false even for city-ops workdirs",
    )
    p_hire.add_argument(
        "--shift-worktree", action="store_true",
        help="force shift_worktree=true (per-shift git worktree; wf-153)",
    )
    p_hire.add_argument(
        "--no-shift-worktree", action="store_true",
        help="force shift_worktree=false (default for jobs; lanes default on)",
    )

    p_ledger = sub.add_parser("ledger", help="tail a worker's shift ledger")
    p_ledger.add_argument("worker")
    p_ledger.add_argument("-n", type=int, default=20)

    p_board = sub.add_parser("board", help="serve the workforce office (own port, own theme)")
    p_board.add_argument("--port", type=int, default=None)

    p_open = sub.add_parser("open", help="[DEPRECATED] engine port is API-only; the visual Roster is the BluePrint suite Roster")
    p_open.add_argument("--port", type=int, default=None)
    p_open.add_argument("--print", dest="print_only", action="store_true",
                        help="(deprecated, ignored)")

    p_daemon = sub.add_parser("daemon", help="the scheduler — one service, schedules from roster data; serves the board")
    p_daemon.add_argument("--once", action="store_true",
                          help="run a single tick and exit (smoke/verify)")
    p_daemon.add_argument("--no-board", action="store_true",
                          help="schedule only; don't serve the board from this process")

    p_plist = sub.add_parser("daemon-plist",
                             help="print the single launchd agent XML (bootstrap: write to "
                                  "~/Library/LaunchAgents/com.workforce.daemon.plist, then launchctl bootstrap)")
    p_plist.add_argument("--path", default=None,
                         help="service PATH (default: conventional user-CLI dirs that exist)")

    sub.add_parser("runtimes",
                   help="list installed agent runtimes and their employment status")

    p_capacity = sub.add_parser(
        "capacity",
        help="detect provider-pool capacity blocks (vendor_limit thrash) — wf-120",
    )
    p_capacity.add_argument(
        "--consecutive", type=int, default=None,
        help="N consecutive capacity fails on one seat (default: 3)",
    )
    p_capacity.add_argument(
        "--seats-hour", type=int, default=None,
        help="K seats capacity-failing in the same UTC hour (default: 2)",
    )
    p_capacity.add_argument(
        "--write-report", action="store_true",
        help="write local/reports/capacity/YYYY-MM-DD.md",
    )
    p_capacity.add_argument(
        "--drop", action="store_true",
        help="create/refresh human-gated For You inbox card(s) via desk HTTP",
    )
    p_capacity.add_argument(
        "--live", action="store_true",
        help="with --drop: actually POST (default is dry-run receipt only)",
    )
    p_capacity.add_argument(
        "--desk", default="",
        help="desk base URL (default: $WL_DESK_URL / $TP_DESK_URL)",
    )
    p_capacity.add_argument(
        "--workspace", default="",
        help="city root for city-rel report paths in drop body",
    )

    p_host_audit = sub.add_parser(
        "host-audit",
        help="ghost-audit open claims for mid-shift tier-2 host-mutation "
             "patterns — dry-run default",
    )
    p_host_audit.add_argument(
        "--product", default="",
        help="store slug to scan (default: workforce, or $WORKFORCE_PRODUCT)",
    )
    p_host_audit.add_argument(
        "--all-products", action="store_true",
        help="scan every product listed on --products / default set",
    )
    p_host_audit.add_argument(
        "--products", default="",
        help="comma-separated product list when using --all-products "
             "(default: workforce)",
    )
    p_host_audit.add_argument(
        "--worker", default="",
        help="narrow to label worker:<name> claims only",
    )
    p_host_audit.add_argument(
        "--live", action="store_true",
        help="post Blocked: on ungated hits (default is dry-run receipt)",
    )
    p_host_audit.add_argument(
        "--desk", default="",
        help="desk base URL (default: $WL_DESK_URL / $TP_DESK_URL)",
    )
    p_host_audit.add_argument(
        "--author", default="workforce",
        help="comment author for live Blocked: posts (default: workforce)",
    )
    p_host_audit.add_argument(
        "--include-run-logs", action="store_true",
        help="reserved (slice 3) — ignored in v1; run tails not scanned yet",
    )

    p_digest = sub.add_parser(
        "digest-upsert",
        help="CoS daily digest: one desk ticket per local day, update-in-place "
             " — dry-run default",
    )
    p_digest.add_argument(
        "--day", default="",
        help="YYYY-MM-DD (default: host-local today)",
    )
    p_digest.add_argument(
        "--project", default="workforce",
        help="desk product store (default: workforce)",
    )
    p_digest.add_argument(
        "--body", default="",
        help="digest description body (markdown)",
    )
    p_digest.add_argument(
        "--body-file", default="",
        help="read description body from file (- = stdin)",
    )
    p_digest.add_argument(
        "--desk", default="",
        help="desk base URL (default: $WL_DESK_URL / $TP_DESK_URL)",
    )
    p_digest.add_argument(
        "--author", default="chief-of-staff",
        help="create author id (default: chief-of-staff)",
    )
    p_digest.add_argument(
        "--live", action="store_true",
        help="POST/PATCH the desk (default is dry-run receipt)",
    )

    p_doctor = sub.add_parser(
        "doctor",
        help="check roster health — dual-home drift, queue URLs, stale "
             "needs:routing, unlanded shift commits",
    )
    p_doctor.add_argument(
        "--suite-roster", default="",
        help="suite roster path to compare (default: $WORKFORCE_SUITE_ROSTER)",
    )
    p_doctor.add_argument(
        "--desk", default="",
        help="desk base URL for stale-routing scan "
             "(default: $WL_DESK_URL / $TP_DESK_URL)",
    )
    p_doctor.add_argument(
        "--product", action="append", default=[],
        help="limit stale-routing scan to this product (repeatable); "
             "default discovers stores via GET /api/admin/products",
    )
    p_doctor.add_argument(
        "--skip-stale-routing", action="store_true",
        help="skip desk scan for needs:routing on done/canceled",
    )
    p_doctor.add_argument(
        "--repair", action="store_true",
        help="remove needs:routing from done/canceled only (default is "
             "report/dry-run; never touches open tickets or other labels)",
    )

    p_report = sub.add_parser("report", help="cost-rollup reports")
    report_sub = p_report.add_subparsers(dest="report_cmd", required=True)
    p_report_cost = report_sub.add_parser(
        "cost", help="write daily cost-rollup snapshot — local/reports/cost/YYYY-MM-DD.md",
    )
    p_report_cost.add_argument(
        "--date", default="", help="YYYY-MM-DD (default: today UTC)",
    )
    p_report_cost.add_argument(
        "--threshold", type=float, default=5.0,
        help="anomaly flag threshold USD (default: 5.0)",
    )
    p_report_cost.add_argument(
        "--overwrite", action="store_true",
        help="remove existing file for the date and rewrite",
    )

    p_repin = sub.add_parser(
        "repin",
        help="Mode B re-pin: stage policy-checked roster diff + citizen --apply",
    )
    p_repin.add_argument(
        "worker",
        nargs="?",
        default="",
        help="worker seat to re-pin (omit with --apply)",
    )
    p_repin.add_argument(
        "--to",
        dest="to_model",
        default="",
        help="target model pin (must be an allowed policy transition)",
    )
    p_repin.add_argument(
        "--apply",
        dest="apply_path",
        default="",
        help="citizen apply: path to staged roster-diff-*.json (writes live roster + .bak)",
    )
    p_repin.add_argument(
        "--reason",
        default="",
        help="why this stage exists (capacity restore, …)",
    )
    p_repin.add_argument(
        "--source",
        default="",
        help="audit source tag (e.g. capacity-claude:2026-08-03)",
    )
    p_repin.add_argument(
        "--created-by",
        default="chief-of-staff",
        help="stager identity (default: chief-of-staff)",
    )
    p_repin.add_argument(
        "--policy",
        default="",
        help="capacity policy path (default: local/capacity_policy.json or $WORKFORCE_CAPACITY_POLICY)",
    )
    p_repin.add_argument(
        "--no-drop",
        action="store_true",
        help="stage only — do not mint a For You card receipt",
    )
    p_repin.add_argument(
        "--live",
        action="store_true",
        help="with drop: actually POST the For You card (default dry-run receipt)",
    )
    p_repin.add_argument(
        "--desk",
        default="",
        help="desk base URL for For You drop (default: $WL_DESK_URL / $TP_DESK_URL)",
    )
    p_repin.add_argument(
        "--no-cap",
        action="store_true",
        help="skip seats_per_day / cooldown enforcement (tests / citizen override)",
    )

    args = parser.parse_args(argv)
    _data_env = os.environ.get("WORKFORCE_DATA_DIR", "").strip()
    # _base: the WorkForce home directory — source of local/ state and roster.
    # When WORKFORCE_DATA_DIR is set (installed-package installs, multi-dir
    # setups), use it directly so the daemon and board never depend on CWD.
    _base = _data_env if _data_env else os.getcwd()
    local_root = os.path.join(_base, "local")

    if args.cmd == "board":
        from . import board
        board.serve(args.port or board.DEFAULT_PORT, local_root)
        return 0

    if args.cmd == "open":
        print(
            "DEPRECATED: 'workforce open' pointed at the engine port (:8797), "
            "which is API-only. The visual Roster is in the BluePrint suite — "
            "run 'blueprint serve' to start it.",
            file=sys.stderr,
        )
        return 1

    if args.cmd == "daemon":
        from .daemon import Daemon
        d = Daemon(base=_base, local_root=local_root)
        if args.once:
            fired = d.tick(wait=True)
            print("tick complete: fired %d worker(s)" % fired)
            return 0
        return d.run(with_board=not args.no_board)

    if args.cmd == "daemon-plist":
        from .daemon import plist_xml
        print(plist_xml(_base, path=args.path, data_dir=_data_env or None), end="")
        # The door, on stderr so stdout stays clean XML for redirection into
        # the plist.
        print("# once bootstrapped, the engine API is at %s  (API only; "
              "visual Roster is in the BluePrint suite)"
              % _board_url(), file=sys.stderr)
        return 0

    if args.cmd == "hire":
        from . import hire as hire_mod
        from .roster import RosterError
        if getattr(args, "staff", False) and getattr(args, "no_staff", False):
            print("hire error: --staff and --no-staff are mutually exclusive",
                  file=sys.stderr)
            return 1
        if (getattr(args, "shift_worktree", False)
                and getattr(args, "no_shift_worktree", False)):
            print(
                "hire error: --shift-worktree and --no-shift-worktree "
                "are mutually exclusive",
                file=sys.stderr,
            )
            return 1
        staff_arg = None
        if getattr(args, "staff", False):
            staff_arg = True
        elif getattr(args, "no_staff", False):
            staff_arg = False
        shift_wt_arg = None
        if getattr(args, "shift_worktree", False):
            shift_wt_arg = True
        elif getattr(args, "no_shift_worktree", False):
            shift_wt_arg = False
        try:
            result = hire_mod.hire(
                name=args.name,
                workdir=args.workdir,
                display=args.display,
                role=args.role,
                kind=args.kind,
                identity=args.identity,
                schedule=args.schedule,
                model=args.model,
                queue_url=args.queue_url,
                project=args.project,
                plant=not args.no_plant,
                force_papers=args.force_papers,
                roster_path=args.file,
                base=_base,
                dry_run=args.dry_run,
                staff=staff_arg,
                worker_type=getattr(args, "type", "") or "",
                shift_worktree=shift_wt_arg,
            )
        except RosterError as exc:
            print("hire error: %s" % exc, file=sys.stderr)
            return 1
        print(result.get("msg") or ("hired %s" % args.name))
        for step in result.get("next_steps") or []:
            print("  next: %s" % step)
        return 0

    if args.cmd == "doctor":
        faults = []
        engine_workers = set()
        er_path = ""
        er = None
        try:
            er = roster_mod.load(args.file, base=_base)
            engine_workers = set(er.workers)
            er_path = er.path or ""
            print("Engine roster: %s (%d workers)" % (er_path, len(engine_workers)))
            for w_name, w in er.workers.items():
                if w.kind == "lane" and not w.queue_url:
                    faults.append(
                        "QUEUE: lane %r has no queue_url — worker will never claim work" % w_name
                    )
                if (w.queue_url
                        and ("?worker=" in w.queue_url or "&worker=" in w.queue_url)
                        and "label=worker:" not in w.queue_url):
                    faults.append(
                        "QUEUE: %r queue_url uses ?worker= probe — "
                        "replace with label=worker: form" % w_name
                    )
        except roster_mod.RosterError as exc:
            faults.append("ENGINE: %s" % exc)
            print("Engine roster: LOAD ERROR — %s" % exc)

        if er is not None:
            from . import hire as hire_mod

            for w_name, w in er.workers.items():
                cmd = getattr(w, "command", None) or []
                if cmd:
                    cmd0 = cmd[0] or ""
                    if cmd0 and not _command_present(cmd0):
                        if os.path.isabs(cmd0):
                            faults.append(
                                "CLI: %r command %r missing or not executable"
                                % (w_name, cmd0)
                            )
                        else:
                            faults.append(
                                "CLI: %r command %r not found on PATH"
                                % (w_name, cmd0)
                            )
                # City-ops seats must be staff=true so Map groups them in the
                # Office staff bay. Drift = fragmented "ops" sector.
                if hire_mod.is_city_ops_workdir(getattr(w, "workdir", "") or ""):
                    staff_val = getattr(w, "staff", False)
                    if staff_val not in (True, 1, "1", "true", "True"):
                        faults.append(
                            "STAFF: %r workdir is city-ops (.protocolcity/ops) "
                            "but staff is not true — Map will bay outside "
                            "Office staff; set staff=true or re-hire"
                            % w_name
                        )
            # wf-153 — shift worktree enablement: note (not fault) when a code
            # lane still shares the primary checkout with founder sessions.
            from .engine import _is_git_workdir

            shift_notes = []
            for w_name, w in er.workers.items():
                if getattr(w, "kind", "") != "lane":
                    continue
                if getattr(w, "shift_worktree", False):
                    continue
                wd = getattr(w, "workdir", "") or ""
                if wd and _is_git_workdir(wd):
                    shift_notes.append(w_name)
            if shift_notes:
                print(
                    "Shift worktree: %d lane(s) on git workdir with "
                    "shift_worktree off — shared checkout risk: %s"
                    % (len(shift_notes), ", ".join(sorted(shift_notes)))
                )
            else:
                print("Shift worktree: no code-lane enablement notes")
            # wf-174 — drain-loop stunted: lane with queue still on max_passes=1
            # (single-pass) crawls deep backlogs at 1 slice/shift. Note only —
            # citizen repin to 0 (budget drain) or N; hands never edit roster.
            drain_stunted = []
            for w_name, w in er.workers.items():
                if getattr(w, "kind", "") != "lane":
                    continue
                if not (getattr(w, "queue_url", "") or "").strip():
                    continue
                if int(getattr(w, "max_passes", 1) or 0) == 1:
                    drain_stunted.append(w_name)
            if drain_stunted:
                print(
                    "Drain loop: %d lane(s) with max_passes=1 (single-pass) — "
                    "deep ready feeds crawl at ~1 slice/shift; "
                    "citizen set max_passes=0 (budget drain) or N: %s"
                    % (len(drain_stunted), ", ".join(sorted(drain_stunted)))
                )
            else:
                print("Drain loop: no single-pass lane notes")
            # wf-171 — unlanded shift commits (note only; never auto-push).
            # Detects board-green vs code-on-main drift when hands close
            # without PROCESS §5.1.3 land-on-origin/main.
            from . import shift_landing as landing_mod

            unlanded = landing_mod.scan_unlanded(er, local_root)
            print(landing_mod.format_report(unlanded))
            # wf-166 — daily fire ceiling pins (note only; 0/absent = unlimited)
            day_caps = []
            for w_name, w in er.workers.items():
                cap = int(getattr(w, "max_fires_per_day", 0) or 0)
                if cap > 0:
                    day_caps.append("%s=%d" % (w_name, cap))
            if day_caps:
                print(
                    "Daily fire ceiling: %d seat(s) with max_fires_per_day "
                    ": %s"
                    % (len(day_caps), ", ".join(sorted(day_caps)))
                )
            else:
                print("Daily fire ceiling: no seats pinned (max_fires_per_day=0)")
            # §5.2 registry coverage: roster identity must appear in
            # PROCESS.md so board writes stay attributable.
            from .identity_registry import load_section_52_ids

            reg_ids, process_path = load_section_52_ids()
            if process_path is None:
                print(
                    "§5.2 registry: not found "
                    "(set WORKLANE_PROCESS or keep worklane/PROCESS.md as sibling)"
                )
            elif not reg_ids:
                print("§5.2 registry: %s — no agent-id rows parsed" % process_path)
            else:
                missing = []
                for w_name, w in er.workers.items():
                    ident = (w.identity or w_name or "").strip()
                    if ident and ident not in reg_ids:
                        missing.append(ident)
                if missing:
                    print(
                        "§5.2 registry: %s (%d ids) — %d missing"
                        % (process_path, len(reg_ids), len(missing))
                    )
                    for ident in sorted(set(missing)):
                        faults.append(
                            "IDENTITY: roster id %r missing from PROCESS §5.2"
                            % ident
                        )
                else:
                    print(
                        "§5.2 registry: %s — all %d roster identities registered"
                        % (process_path, len(engine_workers))
                    )
            from . import capacity as capacity_mod
            cap_alerts = capacity_mod.detect_capacity_alerts(er, local_root)
            if cap_alerts:
                print("Capacity:      %d pool block(s)" % len(cap_alerts))
                for a in cap_alerts:
                    faults.append(
                        "CAPACITY: pool %r blocked — %s" % (a["pool"], a["reason"])
                    )
            else:
                print("Capacity:      no pool blocks")

        suite_path = (getattr(args, "suite_roster", None) or "").strip() or \
            os.environ.get("WORKFORCE_SUITE_ROSTER", "").strip()
        if suite_path:
            # One-file law (2026-07-27): same inode / realpath → no dual-home drift.
            same_home = False
            if er_path:
                try:
                    same_home = os.path.samefile(er_path, suite_path)
                except OSError:
                    same_home = (
                        os.path.realpath(er_path) == os.path.realpath(suite_path)
                    )
            if same_home:
                print("Suite roster:  same file as engine (single-home / symlink) — OK")
                print("Dual-home:     N/A — one physical roster")
            else:
                suite_workers = set()
                try:
                    sr = roster_mod.load(path=suite_path)
                    suite_workers = set(sr.workers)
                    print("Suite roster:  %s (%d workers)" % (sr.path, len(suite_workers)))
                except roster_mod.RosterError as exc:
                    faults.append("SUITE: %s" % exc)
                    print("Suite roster:  LOAD ERROR — %s" % exc)
                if engine_workers and suite_workers:
                    only_engine = engine_workers - suite_workers
                    only_suite = suite_workers - engine_workers
                    if only_engine:
                        faults.append(
                            "DRIFT: in engine only: %s" % ", ".join(sorted(only_engine))
                        )
                    if only_suite:
                        faults.append(
                            "DRIFT: in suite only: %s" % ", ".join(sorted(only_suite))
                        )
                    if not only_engine and not only_suite:
                        print(
                            "Dual-home:     OK — keys match "
                            "(prefer one file + symlink; two copies still drift risk)"
                        )
        else:
            print("Suite roster:  not configured (set WORKFORCE_SUITE_ROSTER or --suite-roster)")

        # wf-168 — stale needs:routing on terminal tickets (report default).
        # Hermetic suite (WORKFORCE_NO_DESK) skips live desk GETs so doctor
        # unit tests stay offline; routing_hygiene is covered with mocks.
        skip_stale = bool(getattr(args, "skip_stale_routing", False))
        no_desk = (os.environ.get("WORKFORCE_NO_DESK") or "").strip().lower() in (
            "1", "true", "yes",
        )
        allow_desk = (os.environ.get("WORKFORCE_ALLOW_DESK") or "").strip().lower() in (
            "1", "true", "yes",
        )
        if skip_stale:
            print("Stale needs:routing: skipped (--skip-stale-routing)")
        elif no_desk and not allow_desk:
            print("Stale needs:routing: skipped (WORKFORCE_NO_DESK)")
        else:
            from . import routing_hygiene as routing_mod

            desk = (getattr(args, "desk", None) or "").strip()
            products = list(getattr(args, "product", None) or [])
            products = [p.strip() for p in products if p and str(p).strip()]
            try:
                stale = routing_mod.scan_stale_routing(
                    desk=desk,
                    products=products or None,
                    repair=bool(getattr(args, "repair", False)),
                )
                print(routing_mod.format_report(stale))
                # Live repair failures are faults; dry-run residue is a note.
                if getattr(args, "repair", False) and not stale.get("ok", True):
                    for e in stale.get("errors") or []:
                        faults.append("STALE_ROUTING: %s" % e)
            except Exception as exc:
                # Desk down must not hide roster faults — note and continue.
                print(
                    "Stale needs:routing: desk scan failed — %s"
                    % (str(exc)[:200],)
                )

        if faults:
            for f in faults:
                print("FAULT: %s" % f, file=sys.stderr)
            return 1
        print("doctor: OK")
        return 0

    if args.cmd == "runtimes":
        from .runtimes import detect, staffing_pool
        detected = detect()
        r = None
        try:
            r = roster_mod.load(args.file, base=_base)
        except roster_mod.RosterError:
            print("(no roster found — employment status unavailable)", file=sys.stderr)
        pool = staffing_pool(detected, r, local_root=local_root)
        n_installed = sum(1 for e in pool if e["path"] is not None)
        print("RUNTIME STAFFING POOL (%d of %d installed)" % (n_installed, len(pool)))
        for e in pool:
            if e["path"] is None:
                print("  %-10s (not installed)" % e["cli"])
            elif not e["employed"]:
                print("  %-10s %-40s available" % (e["cli"], e["path"]))
            else:
                hits = e["limit_hits"]
                worker_word = "worker" if e["employed"] == 1 else "workers"
                hits_str = "%d quota hit%s (7d)" % (hits, "" if hits == 1 else "s")
                print("  %-10s %-40s %d %s · %s" % (
                    e["cli"], e["path"], e["employed"], worker_word, hits_str))
        return 0

    if args.cmd == "report":
        from . import reports as reports_mod
        date_arg = (getattr(args, "date", None) or "").strip()
        if date_arg:
            try:
                target_date = datetime.datetime.strptime(date_arg, "%Y-%m-%d").date()
            except ValueError:
                print("report: invalid --date %r (want YYYY-MM-DD)" % date_arg, file=sys.stderr)
                return 1
        else:
            target_date = datetime.datetime.now(datetime.timezone.utc).date()
        try:
            r = roster_mod.load(args.file, base=_base)
        except roster_mod.RosterError as exc:
            print("roster error: %s" % exc, file=sys.stderr)
            return 1
        if getattr(args, "overwrite", False):
            existing = os.path.join(
                local_root, "reports", "cost", "%s.md" % target_date.strftime("%Y-%m-%d"),
            )
            if os.path.exists(existing):
                os.remove(existing)
        path = reports_mod.write_daily_cost_report(
            local_root, target_date, r.workers,
            cost_threshold=getattr(args, "threshold", 5.0),
        )
        print("report: %s" % path)
        return 0

    if args.cmd == "capacity":
        from . import capacity as capacity_mod
        try:
            r = roster_mod.load(args.file, base=_base)
        except roster_mod.RosterError as exc:
            print("roster error: %s" % exc, file=sys.stderr)
            return 1
        kw = {}
        if args.consecutive is not None:
            kw["consecutive"] = args.consecutive
        if args.seats_hour is not None:
            kw["seats_same_hour"] = args.seats_hour
        alerts = capacity_mod.detect_capacity_alerts(r, local_root, **kw)
        if not alerts:
            print("capacity: no pool blocks detected")
            if args.write_report:
                path = capacity_mod.write_capacity_report(local_root, alerts)
                print("report: %s" % path)
            return 0
        print("capacity: %d pool block(s)" % len(alerts))
        for a in alerts:
            print("  pool %-10s streak=%d seats_hour=%d · %s" % (
                a["pool"], a["streak"], a["seats_hour"], a["reason"]))
        report_path = ""
        if args.write_report or args.drop:
            report_path = capacity_mod.write_capacity_report(local_root, alerts)
            print("report: %s" % report_path)
        if args.drop:
            dry = not args.live
            workspace = (args.workspace or "").strip() or os.path.dirname(_base.rstrip(os.sep))
            # When base is the city product folder (…/workforce), city root is parent.
            if os.path.basename(_base.rstrip(os.sep)) == "workforce" and not args.workspace:
                workspace = os.path.dirname(_base.rstrip(os.sep))
            rel = capacity_mod.city_rel_report_path(report_path, workspace) if report_path else ""
            for a in alerts:
                receipt = capacity_mod.drop_capacity_for_you(
                    a,
                    report_path=report_path,
                    desk=args.desk,
                    dry_run=dry,
                    city_rel_path=rel,
                )
                print("  drop pool=%s action=%s label=%s%s" % (
                    a["pool"],
                    receipt.get("action"),
                    receipt.get("label"),
                    "" if receipt.get("ok") else " ERR=%s" % receipt.get("error"),
                ))
            if dry:
                print("capacity: dry-run only (pass --live to POST inbox cards)")
        return 1  # non-zero when blocks exist — useful for cron / cadence

    if args.cmd == "host-audit":
        from . import host_audit as host_audit_mod
        products: list = []
        if getattr(args, "all_products", False):
            raw = (getattr(args, "products", None) or "").strip()
            products = [p.strip() for p in raw.split(",") if p.strip()] or [
                "workforce"
            ]
        else:
            p = (getattr(args, "product", None) or "").strip()
            if not p:
                p = (os.environ.get("WORKFORCE_PRODUCT") or "workforce").strip()
            products = [p]
        dry = not bool(getattr(args, "live", False))
        desk = (getattr(args, "desk", None) or "").strip()
        worker = (getattr(args, "worker", None) or "").strip()
        author = (getattr(args, "author", None) or "workforce").strip() or "workforce"
        if getattr(args, "include_run_logs", False):
            print(
                "host-audit: --include-run-logs reserved for slice 3 "
                "(ignored this release)",
                file=sys.stderr,
            )
        any_ungated = False
        any_error = False
        for product in products:
            summary = host_audit_mod.audit_product(
                product,
                desk=desk,
                worker=worker,
                author=author,
                dry_run=dry,
            )
            print(host_audit_mod.format_receipt(summary))
            if (summary.get("ungated_hits") or 0) > 0:
                any_ungated = True
            if summary.get("error") or (summary.get("errors") or 0) > 0:
                any_error = True
        # Exit 1 on ungated hit so patrol / ghost_audit argv can WARN.
        if any_ungated or any_error:
            return 1
        return 0

    if args.cmd == "digest-upsert":
        from . import digest_upsert as digest_mod
        day = (getattr(args, "day", None) or "").strip()
        if day:
            try:
                datetime.datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                print(
                    "digest-upsert: invalid --day %r (want YYYY-MM-DD)" % day,
                    file=sys.stderr,
                )
                return 1
        else:
            day = digest_mod.local_day_str()
        body = (getattr(args, "body", None) or "").strip()
        body_file = (getattr(args, "body_file", None) or "").strip()
        if body_file:
            if body_file == "-":
                body = sys.stdin.read()
            else:
                try:
                    with open(body_file, "r", encoding="utf-8") as fh:
                        body = fh.read()
                except OSError as exc:
                    print("digest-upsert: body-file: %s" % exc, file=sys.stderr)
                    return 1
        if not body:
            body = (
                "## Glance\n\n"
                "Chief-of-staff daily digest · %s (placeholder body).\n\n"
                "## Where\n\n"
                "WorkForce · CoS seat · STAFFING §6\n\n"
                "## Done when\n\n"
                "- You skimmed the digest and acted on gold items\n"
                % day
            )
        dry = not bool(getattr(args, "live", False))
        receipt = digest_mod.upsert_cos_digest(
            body,
            day=day,
            project=(getattr(args, "project", None) or "workforce").strip()
            or "workforce",
            desk=(getattr(args, "desk", None) or "").strip(),
            author=(getattr(args, "author", None) or "chief-of-staff").strip()
            or "chief-of-staff",
            dry_run=dry,
        )
        print(digest_mod.format_receipt(receipt))
        if dry:
            print("digest-upsert: dry-run only (pass --live to POST/PATCH)")
        if receipt.get("ok") is False:
            return 1
        return 0

    if args.cmd == "repin":
        from . import repin as repin_mod
        apply_path = (getattr(args, "apply_path", None) or "").strip()
        policy_arg = (getattr(args, "policy", None) or "").strip() or None
        if apply_path:
            try:
                result = repin_mod.apply_repin(
                    apply_path,
                    roster_path=args.file,
                    local_root=local_root,
                    policy_path=policy_arg,
                    base=_base,
                )
            except repin_mod.RepinError as exc:
                print("repin apply error: %s" % exc, file=sys.stderr)
                return 1
            print("repin: applied %s" % ", ".join(result.get("applied") or []))
            print("  roster: %s" % result.get("roster_path"))
            print("  bak:    %s" % result.get("bak_path"))
            if result.get("applied_diff_path"):
                print("  diff:   %s" % result.get("applied_diff_path"))
            print("  next:   python3 -m workforce roster && python3 -m workforce capacity && python3 -m workforce doctor")
            return 0

        worker = (getattr(args, "worker", None) or "").strip()
        to_model = (getattr(args, "to_model", None) or "").strip()
        if not worker or not to_model:
            print(
                "repin: need <worker> --to <model>, or --apply <staged-diff>",
                file=sys.stderr,
            )
            return 2
        try:
            r = roster_mod.load(args.file, base=_base)
        except roster_mod.RosterError as exc:
            print("roster error: %s" % exc, file=sys.stderr)
            return 1
        try:
            stage = repin_mod.stage_repin(
                r,
                local_root,
                [worker],
                to_model,
                policy_path=policy_arg,
                created_by=getattr(args, "created_by", None) or "chief-of-staff",
                reason=getattr(args, "reason", None) or "",
                source=getattr(args, "source", None) or "",
                enforce_caps=not getattr(args, "no_cap", False),
            )
        except repin_mod.RepinError as exc:
            print("repin stage error: %s" % exc, file=sys.stderr)
            return 1
        print("repin: staged %s" % stage.get("path"))
        print("  stage_id: %s" % stage.get("stage_id"))
        print("  seats_today: %s" % stage.get("seats_today"))
        for ch in (stage.get("diff") or {}).get("changes") or []:
            fields = ch.get("fields") or {}
            model = fields.get("model") or {}
            print(
                "  %s: model %s → %s"
                % (ch.get("worker"), model.get("from"), model.get("to"))
            )
            if "command" in fields:
                cmd_to = (fields["command"] or {}).get("to") or []
                rt = os.path.basename(cmd_to[0]) if cmd_to else "?"
                print("       command runtime → %s" % rt)
        if not getattr(args, "no_drop", False):
            dry = not getattr(args, "live", False)
            receipt = repin_mod.drop_repin_for_you(
                stage,
                desk=getattr(args, "desk", None) or "",
                dry_run=dry,
            )
            print(
                "  drop: action=%s label=%s%s"
                % (
                    receipt.get("action"),
                    receipt.get("label"),
                    " hermetic=1" if receipt.get("hermetic") else "",
                )
            )
            if dry:
                print("  drop: dry-run only (pass --live to POST inbox card)")
        print("  apply: python3 -m workforce repin --apply %s" % stage.get("path"))
        return 0

    try:
        r = roster_mod.load(args.file, base=_base)
    except roster_mod.RosterError as exc:
        print("roster error: %s" % exc, file=sys.stderr)
        return 1

    if args.cmd == "roster":
        for name in sorted(r.workers):
            w = r.workers[name]
            print("%-28s %-4s id=%s schedule=%s model=%s" % (
                name, w.kind, w.identity, w.schedule or "-", w.model or "default"))
        return 0

    if args.cmd == "dispatch":
        try:
            w = r.worker(args.worker)
        except roster_mod.RosterError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return engine.dispatch(w, local_root, dry_run=args.dry_run)

    if args.cmd == "ledger":
        print(Ledger(os.path.join(local_root, "ledger"), args.worker).tail(args.n), end="")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
