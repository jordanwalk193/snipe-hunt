"""Fresh bids on the picks that end soon.

The hourly search sees each auction once an hour, but a snipe is decided in the
last few hours, when bids move fastest. Between searches, every fire or green
auction pick ending within WINDOW_HOURS is read again (one eBay call each, this
minute's bid, never the day's cache), at most once every EVERY_MINUTES, so the
dashboard's "bid now" is never more than about half an hour old where it matters.
No Claude; read-only.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta

from . import config, db, usage
from .ebay_search import _money

WINDOW_HOURS = 3
EVERY_MINUTES = 25
MAX_READS = 40          # per run: the window rarely holds more good auctions
TIERS = ("fire", "green")
LANE = "bids"


def due(con, now: datetime) -> list[dict]:
    """Good auction picks ending within the window whose bid wasn't read lately."""
    soon = now + timedelta(hours=WINDOW_HOURS)
    stale = int(now.timestamp()) - EVERY_MINUTES * 60
    rows = con.execute(
        "SELECT p.data, s.last_seen, s.bid_at FROM picks p JOIN seen s USING (item_id) "
        "WHERE s.closed_at IS NULL AND s.last_seen >= ?", (int(now.timestamp()) - 2 * 3600,))
    out = []
    for r in rows:
        pick = json.loads(r["data"])
        end = pick.get("end_time")
        if (pick.get("is_auction") and pick.get("tier") in TIERS and end
                and now < datetime.fromisoformat(end) <= soon
                and max(r["last_seen"] or 0, r["bid_at"] or 0) < stale):
            out.append(pick)
    return sorted(out, key=lambda p: p["end_time"])[:MAX_READS]


def refresh(con, client, now: datetime | None = None) -> int:
    """Read the bid of every pick due; returns how many were updated."""
    now = now or datetime.now(config.TZ)
    done = 0
    for pick in due(con, now):
        if not usage.ebay_allowed(LANE, con):
            break
        raw = client.item(pick["item_id"], lane=LANE, fresh=True)
        if not raw:
            continue
        price, _ = _money(raw.get("currentBidPrice") or raw.get("price"))
        if price is None:
            continue
        db.note_bid(con, pick["item_id"], price, raw.get("bidCount"), int(time.time()))
        done += 1
    con.commit()
    if done:
        print(f"bids: {done} ending soon read again", file=sys.stderr)
    return done


def wanted(con, now: datetime) -> bool:
    """Is any pick due a fresh read? (The scheduler starts the job only then.)"""
    return bool(due(con, now))
