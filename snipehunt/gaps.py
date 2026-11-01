"""Where the collection has holes, by what has actually sold on eBay.

The ledger's cards (sales grouped by card, lots and slabs left out) are the
market's checklist: every card of the player that has changed hands. Each
inventory row is matched to its ledger card (ledger.match_row); whatever is left
is a gap. Gaps are sized two ways: the cheap ones that sell all the time (a few
dollars each, the fastest way to fill a year) and the big ones (what the
missing centrepieces go for). A gap with a live listing shows where to buy it.

Nothing here reaches eBay or Claude; it is arithmetic on what is already kept.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import datetime

from . import config, ledger, sales

EASY_UNDER = 6.0      # a gap that usually goes for this or less, delivered
MIN_SALES = 2         # a card sold once may have been mispriced; twice is a market
EASY = 15             # how many easy fills to list
BIG = 10              # how many big fish to list
FAMILIES_SHOWN = 12
LIVE_WINDOW = 6 * 3600

# Product families, most specific first: a title belongs to the first that names it.
FAMILIES = ["Topps Now", "Bowman Now", "Topps Chrome Update", "Topps Chrome Sapphire", "Topps Chrome Black",
            "Topps Chrome Platinum", "Cosmic Chrome", "Topps Chrome", "Bowman Draft", "Bowman Chrome",
            "Bowman Sapphire", "Bowman Sterling", "Bowman's Best", "Bowman Platinum", "Bowman",
            "Heritage", "Stadium Club", "Allen & Ginter", "Allen and Ginter", "Gypsy Queen", "Big League",
            "Archives", "Gallery", "Finest", "Gilded", "Dynasty", "Inception", "Pristine", "Tier One",
            "Museum", "Triple Threads", "Tribute", "Definitive", "Five Star", "Luminaries", "Transcendent",
            "Topps 206", "Holiday", "Opening Day", "Update", "Series 2", "Series 1", "Living", "Fire",
            "Pro Debut", "Select", "Prizm", "Mosaic", "Donruss", "Diamond Kings", "Chronicles", "Immaculate",
            "Flawless", "National Treasures", "Contenders", "Elite", "Leaf", "Perfect Game"]
_FAMILY = [(f, re.compile(r"\b" + r"\s+".join("(?:&|and)" if w == "&" else re.escape(w).replace("'", "'?")
                                             for w in f.split()) + r"\b", re.I)) for f in FAMILIES]


def family(title: str) -> str:
    for name, rx in _FAMILY:
        if rx.search(title):
            return name
    return "Other"


def _median(g: dict) -> float:
    return round(statistics.median(s["total"] for s in g["sales"]), 2)


def _is_card(g: dict) -> bool:
    """A distinct card, not a graded copy of one (a slab is a condition, not a gap)."""
    return g["traits"][0] is None


def live_by_card(con, gs: list[dict], now: datetime) -> dict[int, dict]:
    """The cheapest live listing of each ledger card, by the card's id()."""
    since = int(now.timestamp()) - LIVE_WINDOW
    out: dict[int, dict] = {}
    for r in con.execute("SELECT item_id, title, price, shipping, is_auction, end_ts FROM seen "
                         "WHERE closed_at IS NULL AND last_seen >= ? AND price IS NOT NULL", (since,)):
        g = ledger.find(gs, r["title"])
        if not g:
            continue
        total = sales.delivered(r["price"] or 0, r["shipping"])
        cur = out.get(id(g))
        # a fixed price beats an auction's opening bid, whatever the numbers say
        better = cur is None or (cur["auction"] and not r["is_auction"]) or (
            bool(cur["auction"]) == bool(r["is_auction"]) and total < cur["total"])
        if better:
            out[id(g)] = {"item_id": r["item_id"], "total": total, "auction": bool(r["is_auction"]),
                          "ends": datetime.fromtimestamp(r["end_ts"], config.TZ).isoformat(timespec="minutes")
                          if r["end_ts"] else None, "url": f"https://www.ebay.com/itm/{r['item_id']}"}
    return out


def _entry(g: dict, live: dict | None) -> dict:
    newest = g["sales"][0]
    return {"title": g["title"][:90], "median": _median(g), "n": len(g["sales"]),
            "last": datetime.fromtimestamp(newest["sold_at"], config.TZ).date().isoformat(),
            "family": family(g["title"]), "image": g["image"],
            "url": f"https://www.ebay.com/itm/{g['item_id']}", "live": live}


def view(con, rows: list[dict], gs: list[dict], now: datetime) -> dict:
    """The gaps, for the dashboard."""
    cards = [g for g in gs if _is_card(g)]
    owned_ids = set()
    for r in rows:
        if (r.get("status") or "").strip().lower().startswith("sold"):
            continue
        g = ledger.match_row(gs, r)
        if g:
            owned_ids.add(id(g))
    gaps = [g for g in cards if id(g) not in owned_ids]
    live = live_by_card(con, gaps, now) if gaps else {}

    def bucket(key):
        acc: dict[str, dict] = defaultdict(lambda: {"known": 0, "owned": 0, "medians": [], "easy_cost": 0.0})
        for g in cards:
            b = acc[key(g)]
            b["known"] += 1
            b["medians"].append(_median(g))
            if id(g) in owned_ids:
                b["owned"] += 1
            elif len(g["sales"]) >= MIN_SALES and _median(g) <= EASY_UNDER:
                b["easy_cost"] += _median(g)
        out = []
        for name, b in acc.items():
            out.append({"name": name, "known": b["known"], "owned": b["owned"], "gap": b["known"] - b["owned"],
                        "pct": round(100 * b["owned"] / b["known"]) if b["known"] else 0,
                        "typical": round(statistics.median(b["medians"]), 2),
                        "easy_cost": round(b["easy_cost"], 2)})
        return out

    years = sorted(bucket(lambda g: sales._year(g["title"]) or "?"), key=lambda y: y["name"])
    families = sorted(bucket(lambda g: family(g["title"])), key=lambda f: (-f["gap"], f["name"]))
    liquid = [g for g in gaps if len(g["sales"]) >= MIN_SALES]
    easy = sorted((g for g in liquid if _median(g) <= EASY_UNDER), key=lambda g: (-len(g["sales"]), _median(g)))
    big = sorted(liquid, key=lambda g: -_median(g))
    return {
        "known": len(cards), "owned": len(owned_ids), "gap": len(gaps),
        "pct": round(100 * len(owned_ids) / len(cards)) if cards else 0,
        "easy_n": len(easy), "easy_cost": round(sum(_median(g) for g in easy), 2),
        "live_n": sum(1 for g in gaps if id(g) in live),
        "years": years,
        "families": families[:FAMILIES_SHOWN],
        "easy": [_entry(g, live.get(id(g))) for g in easy[:EASY]],
        "big": [_entry(g, live.get(id(g))) for g in big[:BIG]],
        # gaps on sale at a fixed price now, the best price against the usual first
        # (an auction's opening bid says nothing about what it will go for)
        "buyable": sorted((_entry(g, live[id(g)]) for g in liquid if id(g) in live and not live[id(g)]["auction"]),
                          key=lambda e: e["live"]["total"] / max(e["median"], 0.01))[:EASY],
    }
