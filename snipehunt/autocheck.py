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
        if row is None or row["description"] is None:   # never read, or the last read failed
            if calls >= limit or not usage.ebay_allowed(LANE, con):
                continue
            client = client or EbayClient(con)
            calls += 1
            try:
                raw = client.item(item.item_id, lane=LANE)
            except Exception as exc:   # one bad item shouldn't stop the hunt
                print(f"autocheck: {item.item_id}: {exc}", file=sys.stderr)
                # tried: scored now with no proof (never a jackpot), read again next run
                con.execute("INSERT OR REPLACE INTO autocheck VALUES (?,?,?,?,?)",
                            (item.item_id, None, 0, None, int(now)))
                _apply(item, "")
                done += 1
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


def unread(con, listings: list) -> set[str]:
    """Item ids of listings here that claim an auto and haven't been read yet: they
    wait for the next run instead of being scored without their description (a pick
    is scored once, so a read that came later would never reach it)."""
    con.execute(SCHEMA)
    names = aspects.names() if any(i.aspects for i in listings) else aspects.FALLBACK
    return {i.item_id for i in listings
            if getattr(i, "match", "yes") != "no" and claims_auto(i, names)
            and con.execute("SELECT 1 FROM autocheck WHERE item_id=?", (i.item_id,)).fetchone() is None}


PATCH_DAYS = 14   # stored picks this recent take a read that came after them


def patch_picks(con, now: float | None = None) -> int:
    """Give stored picks of autographs what their description says, once it has been
    read: proof, or an aftermarket signature (and the caps that go with it). Picks
    saved before this check existed have no proof at all. Returns how many changed."""
    import json
    from . import db, score   # score imports most of the package; kept local
    now = now or time.time()
    con.execute(SCHEMA)
    changed = 0
    rows = con.execute(
        "SELECT p.item_id, p.data, a.description FROM picks p JOIN autocheck a USING (item_id) "
        "WHERE p.created_at >= ? AND a.description IS NOT NULL", (now - PATCH_DAYS * 86400,)).fetchall()
    for item_id, data, description in rows:
        pick = json.loads(data)
        before = json.dumps(pick, sort_keys=True)
        title = pick.get("title") or ""
        word = filters.aftermarket_auto(title, description or "")
        if word:
            pick.update(ip_auto=True, aftermarket=word)
        if word or filters.says_auto(pick):
            pick["auto_proof"] = filters.auto_proof(title, description or "")
        if filters.serial_unconfirmed(pick.get("card") or "", title, {}, description or ""):
            pick["serial_unconfirmed"] = True
        else:
            pick.pop("serial_unconfirmed", None)
        pick = score.apply_caps(pick)
        if json.dumps(pick, sort_keys=True) != before:
            db.save_pick(con, pick)
            changed += 1
    con.commit()
    return changed
