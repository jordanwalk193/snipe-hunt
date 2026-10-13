"""What Claude has cost, so the dashboard can show it.

The Claude CLI reports a dollar figure for every call. With an API key that is
what you are billed; on a Claude subscription it is what the same work would
have cost through the API. One running total is kept per day.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from . import config, db

KEY = "claude_cost"
KEEP_DAYS = 62


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
