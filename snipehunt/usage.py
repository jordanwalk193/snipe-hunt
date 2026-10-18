"""What Claude and eBay have cost, so the dashboard can show it and lanes can stop in time.

The Claude CLI reports a dollar figure for every call. With an API key that is
what you are billed; on a Claude subscription it is what the same work would
have cost through the API. One running total is kept per day. eBay calls are
counted per lane, against the app's daily allowance (see ebay_allowed).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from . import config, db

KEY = "claude_cost"
KEEP_DAYS = 62
JOB_ENV = "SNIPEHUNT_JOB"   # set by jobs.main to the name of the job that is running


def record(usd) -> None:
    """Add one call's cost to today's total. Never raises: this is bookkeeping."""
    try:
        usd = float(usd)
        con = db.connect()
        days = json.loads(db.kv_get(con, KEY, "{}"))
        today = datetime.now(config.TZ).date().isoformat()
        entry = days.setdefault(today, {"usd": 0.0, "calls": 0})
        entry["usd"] = round(entry["usd"] + usd, 4)
        entry["calls"] += 1
        db.kv_set(con, KEY, json.dumps(dict(sorted(days.items())[-KEEP_DAYS:])))
        job = os.environ.get(JOB_ENV) or "other"   # which job spent it, for the self-review
        db.bump(con, f"cost:{job}", usd, today)
        db.bump(con, f"claude_calls:{job}", 1, today)
        con.commit()
        con.close()
    except Exception as exc:
        print(f"usage: could not record a Claude cost: {exc}", file=sys.stderr)


def summary(con, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    days = json.loads(db.kv_get(con, KEY, "{}"))
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    return {
        "cost_today": round(days.get(today, {}).get("usd", 0.0), 2),
        "calls_today": days.get(today, {}).get("calls", 0),
        "cost_month": round(sum(d["usd"] for day, d in days.items()
                                if day.startswith(month)), 2),
    }


# eBay calls. The Browse API allows an app EBAY_DAY_LIMIT calls a day (setting
# ebay_daily_cap), shared by every lane. Past WARN_AT of it, lanes stop in
# CUT_ORDER: each group stops at its own share of the cap, so the two main
# searches and the sold-price look-ups are the last to stop.
WARN_AT = 0.80
CUT_ORDER = (("midtier", 0.80), ("extra", 0.80), ("hidden", 0.85), ("typo", 0.90),
             ("keyword", 0.95))
LANE_KEY = "ebay_lane"      # stats key prefix: ebay_lane:<lane>, per day
_today: dict = {}            # what earlier runs used today: read once per process
_warned: list = []


def lane_of(label: str) -> str:
    """Which lane a search label belongs to, for counting and for the cut order."""
    label = label or "other"
    if label in ("auctions-ending", "newly-listed"):
        return "core"
    for prefix, lane in (("midtier-item", "midtier-item"), ("midtier", "midtier"),
                         ("typo-", "typo"), ("extra-", "extra"), ("sellers", "extra"),
                         ("no-name", "hidden"), ("hidden", "hidden"), ("item", "item"),
                         ("aspects", "aspects"), ("other", "other")):
        if label.startswith(prefix):
            return lane
    return "keyword"   # "<name> auto", roster and "ending-" searches


def _earlier(con=None) -> dict:
    """Today's eBay calls by lane, and in all, from the runs that already finished."""
    day = datetime.now(config.TZ).date().isoformat()
    if _today.get("day") != day:
        try:
            totals = db.stat_totals(con or db.connect(), day, day)
        except Exception:   # no database yet
            totals = {}
        _today.clear()
        _today.update(day=day, total=sum(n for k, n in totals.items()
                                         if k.startswith("ebay_calls:")),
                      lanes={k.split(":", 1)[1]: n for k, n in totals.items()
                             if k.startswith(LANE_KEY + ":")})
    return _today


def ebay_used(lane: str | None = None, con=None) -> int:
    """eBay calls made today, in all or in one lane, this run included."""
    from . import ebay_search   # it imports this module; kept local
    earlier = _earlier(con)
    if lane is None:
        return int(earlier["total"] + ebay_search.API_CALLS)
    return int(earlier["lanes"].get(lane, 0) + ebay_search.LANE_CALLS[lane])


def ebay_allowed(lane: str, con=None) -> bool:
    """May this lane make another eBay call today? Logs once when the warning line is crossed."""
    from . import settings
    cap = settings.load(con)["ebay_daily_cap"]
    used = ebay_used(con=con)
    if used >= cap * WARN_AT and not _warned:
        _warned.append(used)
        print(f"usage: {used} of {cap} eBay calls used today; cutting lanes "
              "(midtier and extra first)", file=sys.stderr)
    share = dict(CUT_ORDER).get(lane, 1.0)
    return used < cap * share


def save_lanes(con, lanes) -> None:
    """Add this run's calls per lane to today's stats (ops.py does it as a job ends)."""
    for lane, n in lanes.items():
        db.bump(con, f"{LANE_KEY}:{lane}", n)
