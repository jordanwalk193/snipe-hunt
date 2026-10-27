"""Runs the jobs on a clock, inside the one long-running process.

Each job is started as its own `python -m snipehunt.jobs <name>` so a crash or
a slow web lookup in one never takes the API down, and only one job runs at a
time. Times are in the bot's time zone (`SNIPEHUNT_TZ`).
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

from . import config, db

HOURLY_MINUTE = 0
LAST_CALL_MINUTE = 30      # lowball last call and fresh bids, between hourly runs (no Claude)
DAILY_AT = (9, 0)
WEEKLY_AT = (6, 18, 45)    # weekday (Monday is 0), hour, minute: Sunday 6:45pm
REFLECT_AT = (6, 19, 30)   # the self-review, once the weekly report is out: Sunday 7:30pm
LATE = {"hourly": 50 * 60, "lastcall": 20 * 60, "daily": 6 * 3600, "weekly": 6 * 3600,
        "reflect": 6 * 3600}   # still worth running
TICK = 20

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_name: str | None = None
_wanted: list[str] = []


def running(job: str | None = None) -> bool:
    """Is a job running right now (or waiting its turn)?"""
    with _lock:
        busy = _proc is not None and _proc.poll() is None
        return (busy and (job is None or _name == job)) or (job in _wanted)


def trigger(job: str) -> bool:
    """Ask for a job to run as soon as possible. False if it is already due or running."""
    if running(job):
        return False
    with _lock:
        _wanted.append(job)
    return True


def fast_minutes() -> int:
    """How often the fast lane runs; 0 when it is off (see jobs.fast)."""
    try:
        return max(0, min(59, int(config.get("FAST_LANE_MINUTES") or 0)))
    except ValueError:
        return 0


def last_due(job: str, now: datetime) -> datetime:
    """The most recent moment this job was scheduled to start."""
    if job == "fast":
        every = fast_minutes() or 60
        return now.replace(minute=now.minute - now.minute % every, second=0, microsecond=0)
    if job in ("hourly", "lastcall"):
        minute = HOURLY_MINUTE if job == "hourly" else LAST_CALL_MINUTE
        due = now.replace(minute=minute, second=0, microsecond=0)
        return due if due <= now else due - timedelta(hours=1)
    if job == "daily":
        due = now.replace(hour=DAILY_AT[0], minute=DAILY_AT[1], second=0, microsecond=0)
        return due if due <= now else due - timedelta(days=1)
    weekday, hour, minute = REFLECT_AT if job == "reflect" else WEEKLY_AT
    due = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return due if due <= now else due - timedelta(days=7)


def _last_call_wanted(con, now: datetime) -> bool:
    """Is any lowball target ending within last call's window? Otherwise there is
    nothing to look up, and the job isn't started at all."""
    import json
    from . import lowball   # it imports the scoring side; kept out of the clock's imports
    try:
        targets = json.loads(db.kv_get(con, lowball.KEY, "{}") or "{}").get("targets") or []
    except ValueError:
        return False
    soon = now + timedelta(hours=lowball.LAST_CALL_HOURS)
    return any(now < datetime.fromisoformat(t["end_time"]) <= soon for t in targets
               if t.get("end_time") and t.get("value", 0) >= lowball.CALL_MIN_VALUE)


def _bids_wanted(con, now: datetime) -> bool:
    """Is a good auction pick ending soon due a fresh bid read (bids.py)?"""
    from . import bids
    try:
        return bids.wanted(con, now)
    except Exception:   # a busy or half-built database: try again next tick
        return False


def _bid_now_wanted(con, now: datetime) -> bool:
    """With bid-now alerts on, is any auction already on the bid-now list (bidnow.py)?"""
    from . import bidnow, settings
    try:
        return bool(settings.load(con)["bid_now_alerts"] and bidnow.find(con, now))
    except Exception:   # a busy or half-built database: try again next tick
        return False


def due_jobs(con, now: datetime) -> list[str]:
    """Jobs whose scheduled time has passed since they last started."""
    out = []
    for job in ("hourly", "daily", "weekly", "reflect", "lastcall") + (
            ("fast",) if fast_minutes() else ()):
        due = last_due(job, now)
        started = float(db.kv_get(con, f"started:{job}", "0") or 0)
        late = LATE.get(job, fast_minutes() * 60)
        if job == "fast" and due.minute == HOURLY_MINUTE:
            continue   # the hourly run is searching anyway
        if job == "lastcall" and not (_last_call_wanted(con, now) or _bids_wanted(con, now)
                                      or _bid_now_wanted(con, now)):
            continue
        if started < due.timestamp() and (now - due).total_seconds() <= late:
            out.append(job)
    return out


def _start(con, job: str) -> None:
    global _proc, _name
    db.kv_set(con, f"started:{job}", str(time.time()))
    con.commit()
    print(f"scheduler: starting {job}", file=sys.stderr)
    with _lock:
        _name = job
        _proc = subprocess.Popen([sys.executable, "-m", "snipehunt.jobs", job])


def tick(con, now: datetime) -> str | None:
    """Start the next job if nothing is running: a requested one first, then a due one."""
    if running():
        return None
    with _lock:
        job = _wanted.pop(0) if _wanted else None
    job = job or next(iter(due_jobs(con, now)), None)
    if job:
        _start(con, job)
    return job


def run_forever() -> None:
    con = db.connect()
    # A fresh install shouldn't fire every job the moment it starts.
    for job in ("daily", "weekly", "reflect"):
        if db.kv_get(con, f"started:{job}") is None:
            db.kv_set(con, f"started:{job}", str(time.time()))
    con.commit()
    while True:
        try:
            tick(con, datetime.now(config.TZ))
        except Exception as exc:   # the clock must keep ticking
            print(f"scheduler: {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(TICK)
