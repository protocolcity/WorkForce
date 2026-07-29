"""The daemon — the single scheduler service.

THE DAEMON IS THE SCHEDULER (ratified on oc-2, 2026-07-13): exactly one OS
service on the machine, firing every daemon-owned worker internally from
roster data. Nothing is ever installed per worker — a cadence change, a new
worker, a paused lane are all roster edits, picked up on the next tick.

Ownership rule: a worker is daemon-owned iff its ``schedule`` parses as a
five-field cron expression (schedule.maybe_cron). Informational strings
("manual", "launchd ... (legacy)") are never fired — that's how migration
stays one-lane-at-a-time: flipping a lane IS the roster edit.

Mechanics:
  - Tick on the minute boundary; a worker fires when its cron matches the
    tick's UTC minute. Missed minutes (host asleep) are not replayed —
    conservative skips are correct, the next matching minute picks up.
  - Each fire runs engine.dispatch in its own thread; the engine's §3
    per-worker lock already guarantees single-flight, so an overrunning
    shift makes the next fire a clean SKIP, never an overlap.
  - Single instance: a pidfile-checked heartbeat (local/daemon.json),
    rewritten every tick — this is the machine-readable seam RUNNER_SPEC
    §8 says the board reads (last tick, next fire per worker, pid).
"""

import datetime
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

from . import engine, roster as roster_mod
from .schedule import maybe_cron

HEARTBEAT = "daemon.json"
EVENT_CURSOR = "event_cursors.json"
STALE_TICK_SECS = 180  # heartbeat older than this = daemon presumed dead
# Desk base for event-trigger. Same default as board.
DESK_URL = os.environ.get(
    "WORKFORCE_DESK", os.environ.get("WORKFORCE_DESK", "http://127.0.0.1:8799")
)
# Debounce: do not re-fire the same worker more often than this (seconds).
EVENT_FIRE_COOLDOWN_SECS = 90


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: Optional[datetime.datetime]) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def read_heartbeat(local_root: str) -> Optional[dict]:
    try:
        with open(os.path.join(local_root, HEARTBEAT), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def heartbeat_status(local_root: str) -> str:
    """'running' | 'stale' | 'stopped' — the board's one-word daemon health."""
    hb = read_heartbeat(local_root)
    if not hb or not pid_alive(hb.get("pid", 0)):
        return "stopped"
    try:
        last = datetime.datetime.strptime(
            hb["last_tick"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (KeyError, ValueError):
        return "stale"
    return "running" if (_utcnow() - last).total_seconds() < STALE_TICK_SECS else "stale"


class Daemon:
    def __init__(self, base: str, local_root: str) -> None:
        self.base = base
        self.local_root = local_root
        self.started_at = _utcnow()
        # worker -> last fired minute key, restored from the prior heartbeat so
        # a restart inside an already-fired minute doesn't re-fire it (oc-9:
        # observed 04:20:38 re-fire after bootstrap). Keys are absolute minute
        # timestamps, so a stale entry only guards the exact interrupted minute
        # — a later minute never matches, so no staleness window is needed.
        self._fired: Dict[str, str] = self._reload_fired()
        self._threads: Dict[str, threading.Thread] = {}
        self._draining = False
        self._wake = threading.Event()  # set by begin_drain to cut the sleep short
        # wf-74 event-trigger: poll cursor per WorkLane project + last fire time
        self._event_cursors: Dict[str, int] = self._load_event_cursors()
        self._event_last_fire: Dict[str, float] = {}  # worker -> monotonic ts

    def _reload_fired(self) -> Dict[str, str]:
        """Last-fired minute keys from the prior heartbeat, or {} on a fresh
        install. The double-start guard in run() still refuses to co-run with a
        live daemon, so inheriting a dead predecessor's cursor is safe."""
        hb = read_heartbeat(self.local_root)
        fired = hb.get("fired") if hb else None
        return dict(fired) if isinstance(fired, dict) else {}

    def _log(self, msg: str) -> None:
        print("%s daemon %s" % (_iso(_utcnow()), msg), flush=True)

    def _roster(self) -> Optional[roster_mod.Roster]:
        try:
            return roster_mod.load(base=self.base)   # reload every tick: roster is data
        except roster_mod.RosterError as exc:
            self._log("ERROR roster unreadable: %s" % exc)
            return None

    def begin_drain(self, signum: Optional[int] = None) -> None:
        """Stop firing, let in-flight shifts finish, then exit — SIGTERM's
        meaning.
        launchctl bootout sends SIGTERM first; the plist's ExitTimeOut gives
        this drain room to finish inside the largest shift budget."""
        if not self._draining:
            self._draining = True
            self._wake.set()
            self._log("drain begins (signal %s) — no new fires; waiting on in-flight shifts"
                      % (signum if signum is not None else "-"))

    def in_flight(self) -> List[str]:
        return sorted(n for n, t in self._threads.items() if t.is_alive())

    def _write_heartbeat(self, now: datetime.datetime, roster: Optional[roster_mod.Roster]) -> None:
        workers = {}
        if roster:
            for name in sorted(roster.workers):
                cron = maybe_cron(roster.workers[name].schedule)
                workers[name] = {
                    "schedule": roster.workers[name].schedule,
                    "owned": bool(cron),
                    "next_fire": _iso(cron.next_fire(now)) if cron else "",
                }
        payload = {
            "pid": os.getpid(),
            "started_at": _iso(self.started_at),
            "last_tick": _iso(now),
            "state": "draining" if self._draining else "scheduling",
            "in_flight": self.in_flight(),
            "fired": dict(self._fired),   # last-fired minute per worker; reloaded on restart
            "workers": workers,
        }
        path = os.path.join(self.local_root, HEARTBEAT)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)

    def _fire(self, worker: roster_mod.Worker) -> None:
        rc = engine.dispatch(worker, self.local_root)
        self._log("fired %s rc=%d" % (worker.name, rc))

    def tick(self, now: Optional[datetime.datetime] = None, wait: bool = False) -> int:
        """One scheduler pass; returns the number of workers fired."""
        now = now or _utcnow()
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        roster = self._roster()
        fired = 0
        if roster and not self._draining:
            for name in sorted(roster.workers):
                w = roster.workers[name]
                cron = maybe_cron(w.schedule)
                if not cron or not cron.matches(now):
                    continue
                if self._fired.get(name) == minute_key:
                    continue  # already fired this minute (late/duplicate tick)
                prev = self._threads.get(name)
                if prev and prev.is_alive():
                    # engine lock would SKIP anyway; don't stack threads on it
                    self._log("skip %s: previous shift thread still running" % name)
                    self._fired[name] = minute_key
                    continue
                self._fired[name] = minute_key
                t = threading.Thread(target=self._fire, args=(w,), daemon=False,
                                     name="shift-%s" % name)
                self._threads[name] = t
                t.start()
                fired += 1
                if wait:
                    t.join()
        self._write_heartbeat(now, roster)
        return fired

    def fire_now(self, name: str) -> Tuple[bool, str]:
        """Manual trigger — the third trigger source (time/event/manual).

        Same engine path as a scheduled fire; the §3 lock still guarantees
        single-flight, so a manual fire during a shift is a clean SKIP.
        """
        roster = self._roster()
        if not roster:
            return False, "roster unreadable"
        if name not in roster.workers:
            return False, "no such worker"
        prev = self._threads.get(name)
        if prev and prev.is_alive():
            return False, "shift already in flight"
        t = threading.Thread(target=self._fire, args=(roster.workers[name],),
                             daemon=False, name="manual-%s" % name)
        self._threads[name] = t
        t.start()
        self._log("manual dispatch %s" % name)
        return True, "dispatched"

    def skip_now(self, name: str) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """Skip the *next* scheduled fire only.

        Marks the upcoming fire minute as already fired so the tick path
        advances to the following cron match. Does not kill an in-flight shift
        (returns 409-shaped false). Does not pause the hire forever.
        """
        roster = self._roster()
        if not roster:
            return False, "roster unreadable", None, None
        if name not in roster.workers:
            return False, "no such worker", None, None
        prev = self._threads.get(name)
        if prev and prev.is_alive():
            return False, "shift already in flight — cannot skip while LIVE", None, None
        w = roster.workers[name]
        cron = maybe_cron(w.schedule)
        if not cron:
            return False, "worker has no schedule", None, None
        now = _utcnow()
        # Next match at or after now; if already matched this minute, next after
        nxt = cron.next_fire(now)
        if nxt is None:
            return False, "no upcoming fire", None, None
        minute_key = nxt.strftime("%Y-%m-%dT%H:%M")
        self._fired[name] = minute_key
        self._write_heartbeat(now, roster)
        # Following fire after the skipped one (for response)
        following = cron.next_fire(nxt + datetime.timedelta(minutes=1))
        self._log("skip_now %s skipped_fire=%s next=%s" % (
            name, minute_key, _iso(following) if following else ""))
        return (
            True,
            "skipped upcoming fire",
            minute_key,
            _iso(following) if following else None,
        )

    # ── wf-74 event trigger (Desk feed → fire_now) ────────────────────

    def _load_event_cursors(self) -> Dict[str, int]:
        path = os.path.join(self.local_root, EVENT_CURSOR)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _save_event_cursors(self) -> None:
        path = os.path.join(self.local_root, EVENT_CURSOR)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._event_cursors, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            self._log("WARN event cursor save failed: %s" % exc)

    def _projects_from_roster(self, roster: roster_mod.Roster) -> Set[str]:
        """Infer WorkLane project slugs from worker queue_url query params."""
        out: Set[str] = set()
        for w in roster.workers.values():
            url = (w.queue_url or "").strip()
            if not url:
                continue
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            except Exception:
                continue
            for key in ("project", "product"):
                for val in qs.get(key) or []:
                    if val and val.lower() not in ("", "all"):
                        out.add(val.lower())
        return out

    def _workers_for_event(
        self, roster: roster_mod.Roster, event: dict, project: str
    ) -> List[str]:
        """Map a ticket event to roster worker names (labels + queue_url)."""
        labels = event.get("labels") or []
        if not isinstance(labels, list):
            labels = []
        label_set = {str(x).lower() for x in labels}
        names: List[str] = []
        for name, w in roster.workers.items():
            # Explicit assignment label worker:<name>
            if ("worker:%s" % name.lower()) in label_set:
                names.append(name)
                continue
            # Queue URL scoped to this project and worker/label
            url = (w.queue_url or "").lower()
            if not url:
                continue
            if project and ("project=%s" % project) not in url and (
                "product=%s" % project
            ) not in url:
                continue
            if ("worker=%s" % name.lower()) in url or (
                "label=worker:%s" % name.lower()
            ) in url:
                names.append(name)
        return names

    def _poll_desk_events(self, project: str, since: int) -> Tuple[List[dict], int]:
        """Fetch /api/events?project=&since= — returns (events, new_cursor)."""
        q = urllib.parse.urlencode({
            "project": project,
            "since": str(max(0, int(since))),
            "limit": "100",
        })
        url = "%s/api/events?%s" % (DESK_URL.rstrip("/"), q)
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return [], since
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            events = []
        cursor = data.get("cursor", since) if isinstance(data, dict) else since
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = since
        return events, cursor

    def event_tick(self) -> int:
        """Poll WorkLane ticket events and fire matching workers.

        First run primes cursors to the latest event id (no historical replay).
        Clock schedules remain the heartbeat; this is the event trigger source.
        Returns number of workers dispatched.
        """
        if self._draining:
            return 0
        roster = self._roster()
        if not roster:
            return 0
        projects = self._projects_from_roster(roster)
        if not projects:
            return 0
        dispatched = 0
        now_m = time.monotonic()
        for project in sorted(projects):
            since = int(self._event_cursors.get(project, -1))
            if since < 0:
                # Prime: jump to current end without replay
                _, cursor = self._poll_desk_events(project, 10 ** 12)
                # empty feed: cursor stays high; re-poll with since=0 get none
                # Better: poll with since=0 limit=1 from high end — use cursor from
                # empty since=max. Actually API returns cursor=since when empty.
                # Fetch latest by since=0 limit=1 sorted asc then take last id...
                # list_events is ASC from since. So since=0 limit=1 is oldest.
                # Use large since to get empty and then we need max id differently.
                # Practical prime: GET events?since=0&limit=1 is oldest; instead
                # poll once with since=0 limit=500 and take max id, discard events.
                bootstrap, _ = self._poll_desk_events(project, 0)
                # fetch a second page? for prime only take last page by looping
                max_id = 0
                cur = 0
                for _ in range(20):
                    batch, cur = self._poll_desk_events(project, cur)
                    if not batch:
                        break
                    max_id = max(max_id, int(batch[-1].get("id") or 0))
                    if len(batch) < 100:
                        break
                    cur = int(batch[-1].get("id") or cur)
                self._event_cursors[project] = max_id
                self._log("event cursor primed %s=%d (no historical replay)"
                          % (project, max_id))
                continue
            events, cursor = self._poll_desk_events(project, since)
            if cursor > since:
                self._event_cursors[project] = cursor
            targets: Set[str] = set()
            for ev in events:
                et = (ev.get("event_type") or "").strip()
                # status/label changes that imply work for someone
                if et not in ("status_change", "labels_changed", "created",
                              "comment"):
                    continue
                for name in self._workers_for_event(roster, ev, project):
                    targets.add(name)
            for name in sorted(targets):
                last = self._event_last_fire.get(name, 0.0)
                if now_m - last < EVENT_FIRE_COOLDOWN_SECS:
                    continue
                ok, msg = self.fire_now(name)
                if ok:
                    self._event_last_fire[name] = now_m
                    dispatched += 1
                    self._log("event dispatch %s (%s)" % (name, project))
                else:
                    self._log("event skip %s: %s" % (name, msg))
        self._save_event_cursors()
        return dispatched

    def start_board(self) -> Optional[threading.Thread]:
        """Serve the board from the daemon process — the ONE service carries
        its own UI across reboots. Port held elsewhere (e.g. a standalone
        `workforce board`) is not fatal: scheduling never depends on it."""
        from . import board
        try:
            httpd = board.make_server(local_root=self.local_root, daemon=self)
        except OSError as exc:
            self._log("WARN board not served (port busy?): %s" % exc)
            return None
        t = threading.Thread(target=httpd.serve_forever, daemon=True, name="board")
        t.start()
        self._log("board serving on http://127.0.0.1:%d" % httpd.server_address[1])
        return t

    def run(self, with_board: bool = True) -> int:
        hb = read_heartbeat(self.local_root)
        if hb and hb.get("pid") != os.getpid() and pid_alive(hb.get("pid", 0)):
            print("daemon already running (pid %s) — refusing to double-start"
                  % hb["pid"], file=sys.stderr)
            return 1
        self._log("start pid=%d base=%s" % (os.getpid(), self.base))
        signal.signal(signal.SIGTERM, lambda s, f: self.begin_drain(s))
        signal.signal(signal.SIGINT, lambda s, f: self.begin_drain(s))
        if with_board:
            self.start_board()
        while True:
            self.tick()
            # wf-74: event source between clock ticks (and on the tick)
            try:
                self.event_tick()
            except Exception as exc:
                self._log("WARN event_tick: %s" % exc)
            if self._draining:
                waiting = self.in_flight()
                if waiting:
                    self._log("draining: waiting on %s" % ", ".join(waiting))
                    for t in list(self._threads.values()):
                        t.join()  # each shift is bounded by its own budget
                self._write_heartbeat(_utcnow(), self._roster())
                self._log("drained cleanly — exiting")
                return 0
            now = time.time()
            # Event poll cadence ~15s; clock still fires on minute boundary
            # via tick() when the wait ends near :00.
            wait = min(15.0, max(1.0, 60.0 - (now % 60.0)))
            self._wake.wait(timeout=wait)


def default_service_path() -> str:
    """PATH for the service: launchd gives a bare system PATH that no user-
    installed vendor CLI lives on. Conventional user-CLI dirs, existing only —
    deliberately NOT the generating shell's PATH (session pollution). A worker
    needing more sets PATH in its roster ``env`` (the engine honors it for
    preflight and spawn alike)."""
    candidates = [
        os.path.expanduser("~/.local/bin"),
        "/usr/local/bin", "/opt/homebrew/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    return ":".join(p for p in candidates if os.path.isdir(p))


def plist_xml(base: str, python: Optional[str] = None, path: Optional[str] = None,
              data_dir: Optional[str] = None) -> str:
    """The single launchd agent — the one bootstrap artifact (macOS).

    If ``data_dir`` is provided (or ``WORKFORCE_DATA_DIR`` is set by the
    caller), the plist bakes it into EnvironmentVariables and uses it as
    WorkingDirectory so the daemon always finds its state without needing the
    user to ``cd`` into the repo.
    """
    from xml.sax.saxutils import escape
    python = python or sys.executable
    path = path or default_service_path()
    workdir = data_dir if data_dir else base
    log = os.path.join(workdir, "daemon.log") if data_dir else os.path.join(base, "local", "daemon.log")
    env_block = "        <key>PATH</key><string>%s</string>" % escape(path)
    if data_dir:
        env_block += "\n        <key>WORKFORCE_DATA_DIR</key><string>%s</string>" % escape(data_dir)
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.workforce.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>%s</string>
        <string>-m</string>
        <string>workforce</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
%s
    </dict>
    <key>WorkingDirectory</key><string>%s</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>AbandonProcessGroup</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>ExitTimeOut</key><integer>3900</integer>
    <key>StandardOutPath</key><string>%s</string>
    <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
""" % (escape(python), env_block, escape(workdir), escape(log), escape(log))
