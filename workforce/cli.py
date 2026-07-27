"""CLI — manual dispatch is a lawful loop (RUNNING.md); the daemon is the scheduler."""

import argparse
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

    p_ledger = sub.add_parser("ledger", help="tail a worker's shift ledger")
    p_ledger.add_argument("worker")
    p_ledger.add_argument("-n", type=int, default=20)

    p_board = sub.add_parser("board", help="serve the workforce office (own port, own theme)")
    p_board.add_argument("--port", type=int, default=None)

    p_open = sub.add_parser("open", help="open the WorkForce board in the default browser (the door to the dispatch office)")
    p_open.add_argument("--port", type=int, default=None)
    p_open.add_argument("--print", dest="print_only", action="store_true",
                        help="print the board URL instead of launching a browser")

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

    p_doctor = sub.add_parser(
        "doctor",
        help="check roster health — dual-home drift between engine and suite homes",
    )
    p_doctor.add_argument(
        "--suite-roster", default="",
        help="suite roster path to compare (default: $WORKFORCE_SUITE_ROSTER)",
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
        import subprocess
        url = _board_url(args.port)
        print(url)
        if args.print_only:
            return 0
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            print("could not launch %s: %s (board URL above)" % (opener, exc),
                  file=sys.stderr)
            return 1
        return 0

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
        print("# once bootstrapped, the board is at %s  (`workforce open`)"
              % _board_url(), file=sys.stderr)
        return 0

    if args.cmd == "hire":
        from . import hire as hire_mod
        from .roster import RosterError
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
        try:
            er = roster_mod.load(args.file, base=_base)
            engine_workers = set(er.workers)
            print("Engine roster: %s (%d workers)" % (er.path, len(engine_workers)))
        except roster_mod.RosterError as exc:
            faults.append("ENGINE: %s" % exc)
            print("Engine roster: LOAD ERROR — %s" % exc)

        suite_path = (getattr(args, "suite_roster", None) or "").strip() or \
            os.environ.get("WORKFORCE_SUITE_ROSTER", "").strip()
        if suite_path:
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
                    faults.append("DRIFT: in engine only: %s" % ", ".join(sorted(only_engine)))
                if only_suite:
                    faults.append("DRIFT: in suite only: %s" % ", ".join(sorted(only_suite)))
                if not only_engine and not only_suite:
                    print("Dual-home:     OK — worker keys match")
        else:
            print("Suite roster:  not configured (set WORKFORCE_SUITE_ROSTER or --suite-roster)")

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
        pool = staffing_pool(detected, r)
        n_installed = sum(1 for e in pool if e["path"] is not None)
        print("RUNTIME STAFFING POOL (%d of %d installed)" % (n_installed, len(pool)))
        for e in pool:
            if e["path"] is None:
                print("  %-10s (not installed)" % e["cli"])
            else:
                workers = e["workers"]
                status = ("employed by: " + ", ".join(sorted(workers))) if workers else "available"
                print("  %-10s %-40s [%s]" % (e["cli"], e["path"], status))
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
