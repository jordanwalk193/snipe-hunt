"""Price drops and relists: listings whose seller has blinked.

The hourly search already sees every listing, so it can notice two things for
free: a fixed price that was cut, and a card that ended unsold and came back.
Both mean a seller who wants it gone. Nothing here asks Claude.
"""
from __future__ import annotations

import json
import time

from . import db
from .ebay_search import Listing

KEY = "moves"
DROP = 0.15              # a fixed price cut by this much is worth showing
RELIST_DAYS = 30         # how far back an ended listing still counts as the same card
GONE = 2 * 3600          # not seen for this long means the old listing has ended
KEEP_HOURS = 72
KEEP = 60


def spot(con, item: Listing, now: float | None = None) -> dict | None:
    """What changed about this listing since the last search. Call before db.touch."""
    now = now or time.time()
    row = con.execute("SELECT price FROM seen WHERE item_id=?", (item.item_id,)).fetchone()
    if row is not None:
        old = row["price"]
        if item.is_auction or not old or item.price > old * (1 - DROP):
            return None
        return _move("drop", item, old, now)
    if not item.seller:
        return None
    before = con.execute(
        "SELECT s.item_id, s.price FROM seen s LEFT JOIN sales x USING (item_id) "
        "WHERE s.title=? AND s.seller=? AND s.item_id<>? AND s.last_seen<? AND s.last_seen>? "
        "AND x.item_id IS NULL ORDER BY s.last_seen DESC LIMIT 1",
        (item.title, item.seller, item.item_id, now - GONE, now - RELIST_DAYS * 86400)
    ).fetchone()
    if before is None:
        return None
    return _move("relist", item, before["price"], now)


def _move(kind: str, item: Listing, old: float | None, now: float) -> dict:
    cut = round(100 * (1 - item.price / old)) if old else None
    return {"kind": kind, "item_id": item.item_id, "title": item.title, "old": old,
            "price": item.price, "cut_pct": cut, "shipping": item.shipping,
            "is_auction": item.is_auction, "best_offer": "BEST_OFFER" in item.buying_options,
            "end_time": item.end_time, "seller": item.seller, "url": item.url,
            "image": item.image, "at": int(now)}


def save(con, moves: list[dict], now: float | None = None) -> None:
    """Add this search's moves to the ones kept for the dashboard."""
    now = now or time.time()
    fresh = {m["item_id"] for m in moves}
    kept = [m for m in json.loads(db.kv_get(con, KEY, "[]"))
            if m["item_id"] not in fresh and m["at"] > now - KEEP_HOURS * 3600]
    db.kv_set(con, KEY, json.dumps((moves + kept)[:KEEP], ensure_ascii=False))


def load(con, now_iso: str, limit: int = 20) -> list[dict]:
    """The latest moves, biggest cut first, without auctions that have ended."""
    rows = [m for m in json.loads(db.kv_get(con, KEY, "[]"))
            if not (m["is_auction"] and m["end_time"] and m["end_time"] <= now_iso)]
    rows.sort(key=lambda m: (-(m["cut_pct"] or 0), -m["at"]))
    return rows[:limit]
