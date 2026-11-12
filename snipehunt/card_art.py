"""Pictures of the cards in the collection, for the dashboard's binder.

Each inventory row that has an eBay item number gets the photo from the listing it was
bought from. The ledger (sales) already holds many of those; the rest are looked up on
eBay a few an hour and cached in card_art, including misses, which are retried weekly.
A card with no listing photo falls back to the ledger's newest sale of the same card.
"""
from __future__ import annotations

import sys
import time

from . import ledger, usage

PER_RUN = 25                 # eBay item lookups per hourly run
LANE = "art"                 # the eBay lane these calls count in (usage.py): cut first when the cap runs low
RETRY_MISS = 7 * 86400       # a listing eBay had dropped is asked about again after a week


def small(url: str | None, size: int = 500) -> str | None:
    """eBay's s-l1600/s-l225 picture URL at a dashboard-friendly size."""
    if not url:
        return None
    for big in ("s-l1600", "s-l1200", "s-l800", "s-l500", "s-l300", "s-l225", "s-l140"):
        if big in url:
            return url.replace(big, f"s-l{size}")
    return url


def known(con) -> dict[str, str | None]:
    """item_id -> picture (None = eBay has no listing for it) from the cache and the ledger."""
    out: dict[str, str | None] = {}
    for item_id, image in con.execute("SELECT item_id, image FROM card_art"):
        out[item_id] = image
    for item_id, image in con.execute(
            "SELECT item_id, image FROM sales WHERE image IS NOT NULL AND image != ''"):
        if not out.get(item_id):
            out[item_id] = image
    return out


def fill(con, rows: list[dict], client=None, limit: int = PER_RUN, now: float | None = None) -> int:
    """Look up pictures for owned cards that have none yet; returns how many were found."""
    now = now or time.time()
    have = known(con)
    stale = {i for (i,) in con.execute(
        "SELECT item_id FROM card_art WHERE image IS NULL AND checked_at < ?", (now - RETRY_MISS,))}
    todo = []
    for r in reversed(rows):                        # newest buys first
        item_id = (r.get("ebay_item_id") or "").strip()
        if item_id.isdigit() and (item_id not in have or item_id in stale) and item_id not in todo:
            todo.append(item_id)
    found = 0
    for item_id in todo[:limit]:
        if not usage.ebay_allowed(LANE, con):
            break
        if client is None:
            from .ebay_search import EbayClient
            client = EbayClient(con)
        try:
            raw = client.item(item_id, lane=LANE)
        except Exception as exc:                    # rate limits etc: try again next hour
            print(f"card_art: {item_id}: {exc}", file=sys.stderr)
            break
        image = ((raw or {}).get("image") or {}).get("imageUrl")
        con.execute("INSERT OR REPLACE INTO card_art (item_id, image, checked_at) VALUES (?,?,?)",
                    (item_id, image, int(now)))
        found += bool(image)
    con.commit()
    return found


def picture(row: dict, art: dict[str, str | None], groups: list[dict] | None) -> tuple[str | None, str]:
    """(picture, where from) for one inventory row: "own" = the very card, "same" = a sale of the same card."""
    image = art.get((row.get("ebay_item_id") or "").strip())
    if image:
        return small(image), "own"
    g = ledger.match_row(groups, row) if groups else None
    if g and g.get("image"):
        return small(g["image"]), "same"
    return None, ""
