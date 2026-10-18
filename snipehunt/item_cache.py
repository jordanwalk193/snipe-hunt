"""Single-listing reads, kept so the same live listing is never read twice a day.

Item specifics and photos don't change while a listing runs, and every read is
an eBay call out of a shared daily allowance. A cached answer is used only
while the listing is live and only on the day it was read: the price and the
sold count do change, and sales.py needs the final result after it ends.
Entries are deleted an hour after the listing ends, or after a day.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

from . import config, db

KEEP_AFTER_END = 3600
DROP = ("description",)   # the seller's HTML: large, and nothing reads it
SCHEMA = ("CREATE TABLE IF NOT EXISTS item_cache (item_id TEXT PRIMARY KEY, data TEXT NOT NULL,"
          " end_ts INTEGER, day TEXT NOT NULL, fetched_at INTEGER NOT NULL)")


def _con(con):
    con = con or db.connect()
    con.execute(SCHEMA)
    return con


def _end_ts(raw: dict) -> float | None:
    end = raw.get("itemEndDate")
    try:
        return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() if end else None
    except ValueError:
        return None


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, config.TZ).date().isoformat()


def get(con, item_id: str, now: float | None = None) -> dict | None:
    now = now or time.time()
    try:
        con = _con(con)
        row = con.execute("SELECT data, end_ts, day FROM item_cache WHERE item_id=?",
                          (str(item_id),)).fetchone()
    except Exception as exc:   # bookkeeping: a broken cache only costs a call
        print(f"item_cache: {exc}", file=sys.stderr)
        return None
    if row is None or row["day"] != _today(now):
        return None
    if row["end_ts"] is not None and row["end_ts"] <= now:
        return None   # ended: sales.py needs eBay's final answer, not this
    return json.loads(row["data"])


def put(con, item_id: str, raw: dict, now: float | None = None) -> None:
    now = now or time.time()
    try:
        con = _con(con)
        body = {k: v for k, v in raw.items() if k not in DROP}
        con.execute("INSERT OR REPLACE INTO item_cache VALUES (?,?,?,?,?)",
                    (str(item_id), json.dumps(body), _end_ts(raw), _today(now), int(now)))
        con.execute("DELETE FROM item_cache WHERE (end_ts IS NOT NULL AND end_ts<?)"
                    " OR fetched_at<?", (now - KEEP_AFTER_END, now - 86400))
        con.commit()
    except Exception as exc:
        print(f"item_cache: {exc}", file=sys.stderr)
