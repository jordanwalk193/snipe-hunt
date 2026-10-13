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
DAILY_AT = (9, 0)
WEEKLY_AT = (6, 18, 45)    # weekday (Monday is 0), hour, minute: Sunday 6:45pm
LATE = {"hourly": 50 * 60, "daily": 6 * 3600, "weekly": 6 * 3600}   # still worth running
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


def last_due(job: str, now: datetime) -> datetime:
    """The most recent moment this job was scheduled to start."""
    if job == "hourly":
        due = now.replace(minute=HOURLY_MINUTE, second=0, microsecond=0)
        return due if due <= now else due - timedelta(hours=1)
    if job == "daily":
        due = now.replace(hour=DAILY_AT[0], minute=DAILY_AT[1], second=0, microsecond=0)
        return due if due <= now else due - timedelta(days=1)
    weekday, hour, minute = WEEKLY_AT
    due = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return due if due <= now else due - timedelta(days=7)


def due_jobs(con, now: datetime) -> list[str]:
    """Jobs whose scheduled time has passed since they last started."""
    out = []
    for job in ("hourly", "daily", "weekly"):
        due = last_due(job, now)
        started = float(db.kv_get(con, f"started:{job}", "0") or 0)
        if started < due.timestamp() and (now - due).total_seconds() <= LATE[job]:
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


def run_forever() -> None:
    con = db.connect()
    # A fresh install shouldn't fire every job the moment it starts.
    for job in ("daily", "weekly"):
        if db.kv_get(con, f"started:{job}") is None:
            db.kv_set(con, f"started:{job}", str(time.time()))
    con.commit()
    while True:
        try:
            if not running():
                with _lock:
                    job = _wanted.pop(0) if _wanted else None
                job = job or next(iter(due_jobs(con, datetime.now(config.TZ))), None)
                if job:
                    _start(con, job)
        except Exception as exc:   # the clock must keep ticking
            print(f"scheduler: {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(TICK)
