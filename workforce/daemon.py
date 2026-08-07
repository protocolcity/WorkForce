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
    tick's **host local** minute (wf-146 — seed_ops / contracts promise
    LOCAL machine time). Heartbeat ``last_tick`` and ledger stamps stay
    UTC (``...Z``). Missed minutes (host asleep) are not replayed —
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

from . import capacity as capacity_mod, engine, roster as roster_mod
from .schedule import fire_minute_key, matches_at, maybe_cron, next_fire_utc

HEARTBEAT = "daemon.json"
EVENT_CURSOR = "event_cursors.json"
STALE_TICK_SECS = 180  # heartbeat older than this = daemon presumed dead
# Desk base for event-trigger. Same default as board.
DESK_URL = os.environ.get(
    "WORKFORCE_DESK", os.environ.get("WORKFORCE_DESK", "http://127.0.0.1:8799")
)
# Debounce: do not re-fire the same worker more often than this (seconds).
EVENT_FIRE_COOLDOWN_SECS = 90

# wf-149 — adaptive idle backoff ladder: streak depth past threshold picks the
# effective cadence floor. Caps at the daily heartbeat, never a full stop — the
# daily probe doubles as the desk-reachability canary.
ADAPTIVE_BACKOFF_LADDER = (3600, 14400, 86400)
# wf-149 — wake debounce: WorkLane debounces per hand (~10s) on its side too;
# this daemon-side floor keeps a bulk-file storm from stacking probe threads.
WAKE_DEBOUNCE_SECS = 10.0


def adaptive_backoff_secs(worker: "roster_mod.Worker", streak: int) -> int:
    """wf-149 — effective idle backoff for a lane at this empty-run streak.

    0 = no gate (below threshold, or adaptive disabled on the seat).
    """
    threshold = max(1, int(getattr(worker, "empty_run_threshold", 3) or 3))
    if streak < threshold or not bool(getattr(worker, "empty_run_adaptive", True)):
        return 0
    step = min(streak - threshold, len(ADAPTIVE_BACKOFF_LADDER) - 1)
    return ADAPTIVE_BACKOFF_LADDER[step]


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
        self._cap_day: str = ""  # last UTC day the capacity hook ran
        self._cost_day: str = ""  # last UTC day the daily cost hook ran
        # wf-149 wake-on-route: worker -> last wake stamp (ISO, resets adaptive
        # backoff) and monotonic ts (debounce). Memory-only — a restart forgets
        # wakes, which is safe: the clock fire is the guaranteed fallback.
        self._wakes: Dict[str, str] = {}
        self._wake_monotonic: Dict[str, float] = {}

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
                nf = next_fire_utc(cron, now) if cron else None
                workers[name] = {
                    "schedule": roster.workers[name].schedule,
                    "owned": bool(cron),
                    "next_fire": _iso(nf),
                }
        payload = {
            "pid": os.getpid(),
            "started_at": _iso(self.started_at),
            "last_tick": _iso(now),
            "state": "draining" if self._draining else "scheduling",
            "in_flight": self.in_flight(),
            "fired": dict(self._fired),   # last-fired minute per worker; reloaded on restart
            "wakes": dict(self._wakes),   # wf-149 last wake stamp per worker (streak floor)
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

    def _heartbeat_reconcile_lane(self, worker: roster_mod.Worker) -> None:
        """wf-155 — empty-pause path: release stranded claims without a shift."""
        try:
            report = engine.heartbeat_reconcile(worker, self.local_root)
        except Exception as exc:
            self._log("WARN heartbeat reconcile %s: %s" % (worker.name, exc))
            return
        released = report.get("released") or []
        cleaned = bool(report.get("lock_cleaned"))
        errors = report.get("errors") or []
        if released or cleaned or errors:
            self._log(
                "heartbeat reconcile %s: lock_cleaned=%s released=%s errors=%d"
                % (
                    worker.name,
                    cleaned,
                    ",".join(released) if released else "-",
                    len(errors),
                )
            )

    def _fire_allowed_by_empty_policy(
        self, worker: roster_mod.Worker, now: Optional[datetime.datetime] = None
    ) -> bool:
        """ALWAYS_WORK §4 / wf-111+wf-125+wf-149 — cron suppress after empty streak.

        Precedence once streak >= threshold:
        1. empty_run_pause=True + queue_url: probe queue; suppress if still empty,
           allow when work appears (queue-probe gate, wf-125).
        2. empty_run_backoff > 0: fixed time-gate — withhold until newest empty is
           older than backoff seconds.
        3. empty_run_adaptive (default): ladder time-gate — cadence stretches with
           streak depth (1h → 4h → daily heartbeat) and resets to base on any wake
           or non-empty probe.
        4. Otherwise: allow (adaptive=False, backoff=0 = signal-only mode).

        The streak is floored at the last wake stamp, so a wake returns the lane
        to base cadence in every mode. Manual fire_now bypasses this gate.
        """
        threshold = max(1, int(getattr(worker, "empty_run_threshold", 3) or 3))
        streak, last_ts = engine.empty_run_streak(
            self.local_root, worker.name, since_ts=self._wakes.get(worker.name))
        if streak < threshold:
            return True

        # Queue-probe gate: suppress until ready appears or fire_now
        pause = bool(getattr(worker, "empty_run_pause", False))
        if pause and getattr(worker, "queue_url", ""):
            count = engine.queue_probe_count(worker)
            if count is not None and count <= 0:
                return False  # queue still empty — hold until ready
            return True  # count > 0 or probe failed (fail open → let engine decide)

        # Fixed time-gate or adaptive ladder
        backoff = int(getattr(worker, "empty_run_backoff", 0) or 0)
        if backoff <= 0:
            backoff = adaptive_backoff_secs(worker, streak)
        if backoff <= 0 or not last_ts:
            return True
        try:
            last = datetime.datetime.strptime(
                last_ts, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return True
        age = ((now or _utcnow()) - last).total_seconds()
        return age >= backoff

    def _should_suppress_max_fires_per_day(
        self, worker: roster_mod.Worker, now: Optional[datetime.datetime] = None
    ) -> Tuple[bool, int, int]:
        """wf-166 — optional cron suppress after N START events on the local day.

        Returns (suppress, fires_today, max_fires_per_day).
        max_fires_per_day=0 disables (unlimited). Day boundary is host local
        wall via schedule.host_wall (same as cron fields). Manual fire_now
        does not call this gate.
        """
        max_n = int(getattr(worker, "max_fires_per_day", 0) or 0)
        if max_n <= 0:
            return False, 0, 0
        count = engine.fires_on_local_day(
            self.local_root, worker.name, now=now or _utcnow()
        )
        if count >= max_n:
            return True, count, max_n
        return False, count, max_n

    def _should_suppress_vendor_limit(
        self, worker: roster_mod.Worker, now: Optional[datetime.datetime] = None
    ) -> Tuple[bool, int, int]:
        """wf-126 — optional cron suppress after vendor_limit streak.

        Returns (suppress, streak, backoff_secs).
        threshold=0 disables entirely; backoff=0 = signal-only (never suppress).
        Suppresses when streak >= threshold AND newest capacity fail is within backoff seconds.
        Gate clears automatically when a non-vendor_limit finished shift lands or window expires.
        """
        threshold = int(getattr(worker, "vendor_limit_threshold", 3) or 0)
        if threshold == 0:
            return False, 0, 0
        backoff = int(getattr(worker, "vendor_limit_backoff", 0) or 0)
        if backoff <= 0:
            return False, 0, 0  # signal-only mode; no suppression
        shifts = capacity_mod._read_worker_shifts(self.local_root, worker.name)
        streak = capacity_mod.consecutive_capacity_streak(shifts)
        if streak < threshold:
            return False, streak, backoff
        newest_ts: Optional[str] = None
        for s in shifts:
            if capacity_mod.is_capacity_outcome(s.get("outcome") or "", s.get("reason") or ""):
                newest_ts = s.get("end_ts") or s.get("ts") or None
                break
        if not newest_ts:
            return False, streak, backoff
        try:
            last = datetime.datetime.strptime(
                newest_ts, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return False, streak, backoff
        age = ((now or _utcnow()) - last).total_seconds()
        if age < backoff:
            return True, streak, backoff
        return False, streak, backoff

    def tick(self, now: Optional[datetime.datetime] = None, wait: bool = False) -> int:
        """One scheduler pass; returns the number of workers fired."""
        now = now or _utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        else:
            now = now.astimezone(datetime.timezone.utc)
        # Fire-slot identity is local wall (cron fields); stamps stay UTC.
        minute_key = fire_minute_key(now)
        roster = self._roster()
        fired = 0
        if roster and not self._draining:
            for name in sorted(roster.workers):
                w = roster.workers[name]
                cron = maybe_cron(w.schedule)
                if not cron or not matches_at(cron, now):
                    continue
                if self._fired.get(name) == minute_key:
                    continue  # already fired this minute (late/duplicate tick)
                prev = self._threads.get(name)
                if prev and prev.is_alive():
                    # engine lock would SKIP anyway; don't stack threads on it
                    self._log("skip %s: previous shift thread still running" % name)
                    self._fired[name] = minute_key
                    continue
                if not self._fire_allowed_by_empty_policy(w, now):
                    if getattr(w, "empty_run_pause", False):
                        self._log("skip %s: empty-run pause (streak>=threshold, queue still empty)" % name)
                        # wf-155: pause mode never fires → never hits dispatch
                        # empty-SKIP reconcile. Run heartbeat reconcile here so
                        # stranded in_progress cannot starve the seat forever.
                        self._heartbeat_reconcile_lane(w)
                    else:
                        eff = int(getattr(w, "empty_run_backoff", 0) or 0)
                        if eff <= 0:
                            s, _ = engine.empty_run_streak(
                                self.local_root, name,
                                since_ts=self._wakes.get(name))
                            eff = adaptive_backoff_secs(w, s)
                        self._log(
                            "skip %s: empty-run backoff (streak>=threshold, within "
                            "%ds)" % (name, eff)
                    )
                    self._fired[name] = minute_key
                    continue
                suppress, streak, backoff_secs = self._should_suppress_vendor_limit(w, now)
                if suppress:
                    self._log(
                        "skip %s: vendor-limit backoff (streak=%d, within %ds)"
                        % (name, streak, backoff_secs)
                    )
                    self._fired[name] = minute_key
                    continue
                day_sup, day_count, day_max = self._should_suppress_max_fires_per_day(
                    w, now
                )
                if day_sup:
                    self._log(
                        "skip %s: max_fires_per_day (count=%d >= max=%d local day)"
                        % (name, day_count, day_max)
                    )
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
        if roster:
            self._run_cost_hook(roster, now)
            self._run_capacity_hook(roster, now)
        return fired

    def _run_cost_hook(
        self, roster: roster_mod.Roster, now: datetime.datetime
    ) -> None:
        """Daily cost-rollup wire — writes local/reports/cost/YYYY-MM-DD.md once per UTC day.

        Idempotent: write_daily_cost_report skips if the file already exists.
        Only called when roster is available; exceptions are logged, not raised.
        """
        day = now.strftime("%Y-%m-%d")
        if self._cost_day == day:
            return
        self._cost_day = day
        try:
            from .reports import write_daily_cost_report
            write_daily_cost_report(self.local_root, now.date(), roster.workers)
            self._log("daily cost report written for %s" % day)
        except Exception as exc:
            self._log("cost hook error: %s" % exc)

    def _run_capacity_hook(
        self, roster: roster_mod.Roster, now: datetime.datetime
    ) -> None:
        """Capacity detector wire — runs live once per UTC day.

        Dry-run is the CLI default; here we use live so the daemon drops
        a For You gold card without a host session. Idempotent via inbox_label.
        Only called when roster is available; exceptions are logged, not raised.
        """
        day = now.strftime("%Y-%m-%d")
        if self._cap_day == day:
            return
        self._cap_day = day
        try:
            alerts = capacity_mod.detect_capacity_alerts(roster, self.local_root)
        except Exception as exc:
            self._log("capacity hook detect error: %s" % exc)
            return
        if not alerts:
            return
        try:
            report_path = capacity_mod.write_capacity_report(
                self.local_root, alerts, day=day
            )
        except Exception as exc:
            self._log("capacity hook write_report error: %s" % exc)
            report_path = ""
        workspace = os.path.dirname(self.base.rstrip(os.sep))
        rel = (
            capacity_mod.city_rel_report_path(report_path, workspace)
            if report_path
            else ""
        )
        for a in alerts:
            try:
                receipt = capacity_mod.drop_capacity_for_you(
                    a,
                    report_path=report_path,
                    dry_run=False,
                    city_rel_path=rel,
                )
                self._log(
                    "capacity drop pool=%s action=%s"
                    % (a["pool"], receipt.get("action"))
                )
            except Exception as exc:
                self._log(
                    "capacity hook drop error pool=%s: %s" % (a["pool"], exc)
                )

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

    def wake_now(self, name: str) -> Tuple[bool, str]:
        """wf-149 wake-on-route — the route event IS the dispatch signal.

        Same engine path as a clock fire: probe first, spawn only if the queue
        has work, §3 lock keeps single-flight. Softer contract than fire_now —
        a wake during an in-flight shift is a clean ok-no-op (the lock would
        SKIP anyway), rapid wakes debounce, and every wake stamps the lane so
        the adaptive idle backoff resets to base cadence. Callers treat this
        as fire-and-forget; the clock fire stays the guaranteed fallback.
        """
        roster = self._roster()
        if not roster:
            return False, "roster unreadable"
        if name not in roster.workers:
            return False, "no such worker"
        mono = time.monotonic()
        last = self._wake_monotonic.get(name)
        if last is not None and (mono - last) < WAKE_DEBOUNCE_SECS:
            return True, "debounced (woken <%ds ago)" % int(WAKE_DEBOUNCE_SECS)
        self._wake_monotonic[name] = mono
        self._wakes[name] = _iso(_utcnow())  # reset adaptive backoff to base
        if self._draining:
            return False, "draining — no new fires"
        prev = self._threads.get(name)
        if prev and prev.is_alive():
            return True, "shift in flight — wake noted (backoff reset)"
        t = threading.Thread(target=self._fire, args=(roster.workers[name],),
                             daemon=False, name="wake-%s" % name)
        self._threads[name] = t
        t.start()
        self._log("wake dispatch %s" % name)
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
        # Next match after now (local wall eval); keys + ISO stay consistent
        # with tick.
        nxt = next_fire_utc(cron, now)
        if nxt is None:
            return False, "no upcoming fire", None, None
        minute_key = fire_minute_key(nxt)
        self._fired[name] = minute_key
        self._write_heartbeat(now, roster)
        # Following fire after the skipped one (for response)
        following = next_fire_utc(cron, nxt + datetime.timedelta(minutes=1))
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

    def startup_reconcile(self, roster: Optional[roster_mod.Roster] = None) -> dict:
        """wf-155 — on boot, clean orphan locks and release stranded claims.

        Uses the prior heartbeat's ``in_flight`` list so a SIGKILL mid-shift
        still reconciles even if the lock dir was already wiped. Never touches
        a live lock. Safe to call from tests with a fixture roster.
        """
        hb = read_heartbeat(self.local_root)
        prior = []
        if hb and isinstance(hb.get("in_flight"), list):
            prior = [str(x) for x in hb["in_flight"]]
        rost = roster if roster is not None else self._roster()
        workers = rost.workers if rost else {}
        report = engine.startup_reconcile(
            workers, self.local_root, prior_in_flight=prior,
        )
        cleaned = int(report.get("lock_cleaned") or 0)
        released = report.get("released") or []
        errors = report.get("errors") or []
        if cleaned or released or errors:
            self._log(
                "startup reconcile: locks_cleaned=%d released=%s errors=%d"
                % (cleaned, ",".join(released) if released else "-", len(errors))
            )
        else:
            self._log("startup reconcile: clean (no orphan locks / strands)")
        return report

    def run(self, with_board: bool = True) -> int:
        hb = read_heartbeat(self.local_root)
        if hb and hb.get("pid") != os.getpid() and pid_alive(hb.get("pid", 0)):
            print("daemon already running (pid %s) — refusing to double-start"
                  % hb["pid"], file=sys.stderr)
            return 1
        self._log("start pid=%d base=%s" % (os.getpid(), self.base))
        # wf-155: un-strand in_progress claims left by a killed predecessor
        # before the first clock tick (else empty ready probe → catch-22).
        try:
            self.startup_reconcile()
        except Exception as exc:
            self._log("WARN startup reconcile failed: %s" % exc)
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
