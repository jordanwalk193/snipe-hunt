"""Ops plumbing: health timestamp, Uptime Kuma push, one alert per failure."""
from __future__ import annotations

import sys
import time
import traceback
from typing import Callable

import requests

from . import config, db, notify, profile


def heartbeat(job: str, ok: bool = True, msg: str = "") -> None:
    """Touch the healthz file on success and ping Kuma if a push URL is set."""
    if ok:
        config.ensure_state_dir()
        config.HEALTHZ.write_text(f"{int(time.time())} {job} ok\n")
    url = config.get("KUMA_PUSH_URL")
    if not url:
        return
    try:
        requests.get(url, params={"status": "up" if ok else "down",
                                  "msg": f"{job}: {msg or 'OK'}"[:200]}, timeout=15)
    except requests.RequestException as exc:
        print(f"ops: Kuma push failed: {exc}", file=sys.stderr)


def guarded(job: str, fn: Callable[[], str | None], dry_run: bool = False) -> int:
    """Run a job. A failed run sends one notification, not one per retry."""
    con = db.connect()
    key = f"failing:{job}"
    try:
        summary = fn() or "OK"
    except Exception as exc:
        traceback.print_exc()
        reason = f"{type(exc).__name__}: {exc}"[:300]
        if dry_run:
            return 1
        if db.kv_get(con, key) != "1":
            try:
                notify.send(f"{profile.get().app_name}: {job} run failed", reason)
            except Exception as nexc:  # the phone alert is best effort
                print(f"ops: could not send failure alert: {nexc}", file=sys.stderr)
        db.kv_set(con, key, "1")
        con.commit()
        heartbeat(job, ok=False, msg=reason)
        return 1
    print(f"{job}: {summary}", file=sys.stderr)
    if not dry_run:
        db.kv_set(con, key, "0")
        con.commit()
        heartbeat(job, msg=summary)
    return 0
