"""The rarest cards for sale right now, read from the listing titles.

Nothing here asks Claude or the web: a serial-numbered card says its print run
in the title ("/25", "07/50", "1/1", "Superfractor"), so every hourly search
can rank what is on eBay by how few copies exist.
"""
from __future__ import annotations

import json
import re

from . import db
from .ebay_search import Listing

KEY = "rarest"
MAX_RUN = 999     # a bigger number after a slash is a mass print run, not a rarity
ONE_OF_ONE = re.compile(r"\b(1\s*/\s*1|one of one|1 of 1|superfractor|super ?fractor|"
                        r"printing plate|print plate)\b", re.I)
# "07/50" or "/50". Not "10/12/23" (a date) and not "9.5/10" (a grade).
SERIAL = re.compile(r"(?<![\d/.])(?:#?\s?(\d{1,3})\s?)?/\s?(\d{1,3})(?![\d/.])")
AUTO = re.compile(r"\b(auto|autos|autograph|autographed|signed|rpa)\b", re.I)
GRADED = re.compile(r"\b(psa|bgs|sgc|cgc|beckett)\b", re.I)
# Not one card with one print run: lots, and copies that were never packed out.
NOT_A_CARD = re.compile(r"\b(lot|lots|you pick|u pick|pick your|complete set|reprint|rp|custom|"
                        r"digital|bunt|break|case break|box|hobby|blaster|sealed)\b", re.I)


def print_run(title: str) -> int | None:
    """How many copies of this card exist, when the title says so."""
    if NOT_A_CARD.search(title):
        return None
    if ONE_OF_ONE.search(title):
        return 1
    runs = []
    for copy, run in SERIAL.findall(title):
        run = int(run)
        if not 1 <= run <= MAX_RUN or (copy and not 1 <= int(copy) <= run):
            continue
        runs.append(run)
    return min(runs) if runs else None


def rarest(listings: list[Listing], limit: int = 10) -> list[dict]:
    """The listings with the smallest print runs: fewest copies first, then autographs."""
    found = []
    for item in listings:
        if item.match != "yes" or item.currency != "USD":
            continue
        run = print_run(item.title)
        if run is None:
            continue
        found.append((run, not AUTO.search(item.title), item.price, item))
    found.sort(key=lambda row: row[:3])
    return [{
        "item_id": item.item_id, "title": item.title, "run": run,
        "auto": not no_auto, "graded": bool(GRADED.search(item.title)),
        "price": item.price, "shipping": item.shipping, "is_auction": item.is_auction,
        "best_offer": "BEST_OFFER" in item.buying_options, "bids": item.bid_count,
        "end_time": item.end_time, "seller": item.seller, "url": item.url,
        "image": item.image,
    } for run, no_auto, _, item in found[:limit]]


def owned(row: dict, inventory: list[dict]) -> bool:
    """Is this rare card already in the collection? Same year, same print run, like words."""
    from . import sales   # sales imports nothing from here; kept local to stay that way
    year = re.search(r"\b(19|20)\d\d\b", row["title"])
    mine = sales.words(row["title"])
    for card in inventory:
        serial = re.search(r"/\s?(\d+)", card.get("serial") or "")
        run = 1 if "1/1" in (card.get("serial") or "") else (int(serial.group(1)) if serial else None)
        if run != row["run"] or not year or card.get("year") != year.group(0):
            continue
        theirs = sales.words(" ".join(str(card.get(k) or "") for k in
                                      ("year", "set", "card", "parallel", "card_no")))
        if theirs and len(mine & theirs) / len(theirs) >= 0.6:
            return True
    return False


def save(con, listings: list[Listing], inventory: list[dict] | None = None) -> int:
    """Keep the current top of the list for the dashboard. Returns how many made it."""
    top = rarest(listings, limit=25)
    for row in top:
        first = con.execute("SELECT first_seen FROM seen WHERE item_id=?",
                            (row["item_id"],)).fetchone()
        row["first_seen"] = first["first_seen"] if first else None
        row["owned"] = owned(row, inventory or [])
    # how many of each print run are for sale at all, for the panel's summary line
    runs = [print_run(i.title) for i in listings if i.match == "yes"]
    census = {"one_of_one": sum(r == 1 for r in runs), "to_10": sum(r is not None and 1 < r <= 10 for r in runs),
              "to_50": sum(r is not None and 10 < r <= 50 for r in runs)}
    db.kv_set(con, KEY + ":census", json.dumps(census))
    db.kv_set(con, KEY, json.dumps(top, ensure_ascii=False))
    return len(top)


def load(con, now_iso: str, limit: int = 10) -> list[dict]:
    """The saved list, without auctions that have ended since the last search."""
    rows = json.loads(db.kv_get(con, KEY, "[]"))
    live = [r for r in rows if not (r["is_auction"] and r["end_time"] and r["end_time"] <= now_iso)]
    return live[:limit]


def census(con) -> dict:
    """How many 1/1s, /10-or-fewer and /50-or-fewer cards were for sale at the last search."""
    return json.loads(db.kv_get(con, KEY + ":census", "{}"))
