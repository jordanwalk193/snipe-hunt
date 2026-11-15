"""Is that autograph the maker's, or signed in person? Read the description to know.

"Bowman Chrome Refractor Autograph" can be a pack-pulled, certified auto or a
base card signed at the ballpark (an "IP" or "TTM" auto, worth the base card
plus a few dollars). The title often doesn't say; the seller's description
usually does ("aftermarket, unauthenticated ... in-person (IP)"). Search
results carry only a short description, so each listing that says auto (in its
title, or Autographed: Yes in its specifics) gets one read-only item call, ever:
the verdict is kept in the table `autocheck`. The listing gets its plain-text
description, `auto_proof` (filters.auto_proof) and, for an aftermarket
signature, the "aftermarket_auto" flag, before Claude scores it.

At most PER_RUN reads a run, counted in eBay's "item" lane. A listing not read
yet has no proof, so it can't be a jackpot (jackpot.check) until it is.
"""
from __future__ import annotations

import sys
import time

from . import aspects, filters, usage

PER_RUN = 25            # item calls per run; the rest wait for the next run
KEEP_DAYS = 120         # a verdict is kept this long (listings rarely run longer)
LANE = "item"           # the eBay lane these calls count in (usage.py)
SCHEMA = ("CREATE TABLE IF NOT EXISTS autocheck (item_id TEXT PRIMARY KEY, aftermarket TEXT,"
          " proof INTEGER, description TEXT, checked_at INTEGER NOT NULL)")


def claims_auto(item, names: dict | None = None) -> bool:
    """The title says auto, or the item specifics say signed."""
    if filters.AUTO_WORDS.search(item.title):
        return True
    names = names or aspects.FALLBACK
    return (item.aspects or {}).get(names["autographed"], "").strip().lower() == "yes"


def _apply(item, description: str) -> None:
    item.description = description or ""
    item.auto_proof = filters.auto_proof(item.title, f"{item.short_description} {item.description}",
                                         item.aspects)
    filters.flag(item)


def enrich(con, listings: list, client=None, now: float | None = None,
           limit: int = PER_RUN) -> int:
    """Read the description of each listing here that claims an auto; returns how
    many were checked (from the table or from eBay). Reads at most `limit` new ones."""
    from .ebay_search import EbayClient, description_text   # ebay_search imports settings; kept local
    now = now or time.time()
    con.execute(SCHEMA)
    con.execute("DELETE FROM autocheck WHERE checked_at < ?", (now - KEEP_DAYS * 86400,))
    names = aspects.names() if any(i.aspects for i in listings) else aspects.FALLBACK
    done, calls = 0, 0
    for item in listings:
        if getattr(item, "match", "yes") == "no" or not claims_auto(item, names):
            continue
        row = con.execute("SELECT description FROM autocheck WHERE item_id=?",
                          (item.item_id,)).fetchone()
        if row is None:
            if calls >= limit or not usage.ebay_allowed(LANE, con):
                continue
            client = client or EbayClient(con)
            calls += 1
            try:
                raw = client.item(item.item_id, lane=LANE)
            except Exception as exc:   # one bad item shouldn't stop the hunt
                print(f"autocheck: {item.item_id}: {exc}", file=sys.stderr)
                continue
            text = description_text(raw)
            _apply(item, text)
            con.execute("INSERT OR REPLACE INTO autocheck VALUES (?,?,?,?,?)",
                        (item.item_id, filters.aftermarket_auto(item.title, text, item.aspects),
                         int(item.auto_proof), text, int(now)))
        else:
            _apply(item, row["description"])
        done += 1
    con.commit()
    return done
