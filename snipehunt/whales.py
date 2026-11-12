"""Where the big money goes: what people pay the most for, and how far above
the usual price they go.

From the sales ledger alone. Every sale is put in a tier by what it is (a plain
card, a numbered parallel, a relic, an autograph, a slab) so the ladder shows
what each step is worth; the biggest sales ever are listed; a sale at OVERPAID
times what the same card usually goes for is called out as one; and the
collection is measured against it: which of your cards sit in the money tiers,
and what they are worth there.

Nothing here reaches eBay or Claude.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import datetime

from . import config, ledger, sales
from .gaps import family

TOP = 15
OVERPAID = 3.0          # a sale at this multiple of the card's usual price
OVERPAID_MIN_SALES = 3  # of a card sold at least this often
DRIVERS = 8
TOP_SHARE = 0.05        # the top 5% of sales: what share of the dollars they are
RECENT_DAYS = 90

ONE = re.compile(r"\b1\s?/\s?1\b|\bone of one\b|\bsuperfractor\b|\bprinting plate\b", re.I)
RELIC = re.compile(r"\b(relic|patch|jersey|bat|memorabilia|game[- ]used|swatch)\b", re.I)
TIERS = ["1 of 1", "Autograph", "Slab PSA 10", "Slab, other", "Relic", "Numbered", "Plain"]


def tier(title: str) -> str:
    grade, auto, run = sales.traits(title)
    if ONE.search(title):
        return "1 of 1"
    if auto:
        return "Autograph"
    if grade:
        return "Slab PSA 10" if grade in ("psa10",) else "Slab, other"
    if RELIC.search(title):
        return "Relic"
    if run:
        return "Numbered"
    return "Plain"


def row_tier(row: dict) -> str:
    """The tier of an inventory row, from its own fields."""
    serial = str(row.get("serial") or "")
    if re.search(r"\b1\s?/\s?1\b", serial) or re.search(r"superfractor|plate", str(row.get("parallel") or ""), re.I):
        return "1 of 1"
    if str(row.get("auto") or "").upper() == "Y":
        return "Autograph"
    if str(row.get("graded") or "").strip():
        return "Slab PSA 10" if "10" in str(row.get("graded")) else "Slab, other"
    if str(row.get("relic") or "").upper() == "Y":
        return "Relic"
    if serial.strip():
        return "Numbered"
    return "Plain"


def _sale(s: dict) -> dict:
    return {"title": s["title"][:90], "total": round(s["total"], 2), "kind": s["kind"] or "auction",
            "bids": s["bids"], "date": datetime.fromtimestamp(s["sold_at"], config.TZ).date().isoformat(),
            "image": s["image"], "url": f"https://www.ebay.com/itm/{s['item_id']}",
            "tier": tier(s["title"]), "family": family(s["title"])}


def ladder(rows: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for s in rows:
        by[tier(s["title"])].append(s["total"])
    plain = statistics.median(by["Plain"]) if by.get("Plain") else None
    out = []
    for name in TIERS:
        xs = sorted(by.get(name, []))
        if not xs:
            continue
        med = statistics.median(xs)
        out.append({"tier": name, "n": len(xs), "median": round(med, 2), "p75": round(xs[int(0.75 * (len(xs) - 1))], 2),
                    "max": round(xs[-1], 2), "x": round(med / plain, 1) if plain else None})
    return out


def overpaid(gs: list[dict]) -> list[dict]:
    out = []
    for g in gs:
        if len(g["sales"]) < OVERPAID_MIN_SALES:
            continue
        for s in g["sales"]:
            others = [o["total"] for o in g["sales"] if o is not s]
            usual = statistics.median(others)
            if usual > 0 and s["total"] >= OVERPAID * usual:
                out.append({**_sale(s), "usual": round(usual, 2), "x": round(s["total"] / usual, 1),
                            "over": round(s["total"] - usual, 2)})
    out.sort(key=lambda o: -o["over"])
    return out


def trend(rows: list[dict], now: float, tiers=("Autograph", "Slab PSA 10")) -> dict | None:
    """The money tiers lately against before: are the big prices rising?"""
    cut = now - RECENT_DAYS * 86400
    recent = [s["total"] for s in rows if tier(s["title"]) in tiers and s["sold_at"] >= cut]
    prior = [s["total"] for s in rows if tier(s["title"]) in tiers and s["sold_at"] < cut]
    if len(recent) < 5 or len(prior) < 5:
        return None
    was, is_now = statistics.median(prior), statistics.median(recent)
    return {"tiers": list(tiers), "now": round(is_now, 2), "was": round(was, 2), "n": len(recent),
            "pct": round(100 * (is_now - was) / was) if was else 0}


def yours(rows: list[dict], gs: list[dict]) -> dict:
    """Your cards in the money tiers, at the ledger's newest price."""
    from . import report   # report imports this module's neighbours
    mine, dupes = [], 0.0
    for r in rows:
        if (r.get("status") or "").strip().lower().startswith("sold"):
            continue
        t = row_tier(r)
        if t == "Plain":
            continue
        g = ledger.match_row(gs, r)
        try:
            paid = float(r.get("price_paid") or 0)
        except ValueError:
            paid = 0.0
        value = ledger.value(g)
        mine.append({"card": report.card_name(r)[:90], "tier": t, "paid": paid, "value": value,
                     "image": g["image"] if g else None})
        if str(r.get("dupe") or "").upper() == "Y" and value:
            dupes += value
    mine.sort(key=lambda c: -(c["value"] or 0))
    priced = [c for c in mine if c["value"] is not None]
    return {"n": len(mine), "by_tier": dict(sorted(((t, sum(1 for c in mine if c["tier"] == t)) for t in TIERS if any(c["tier"] == t for c in mine)))),
            "paid": round(sum(c["paid"] for c in mine), 2), "value": round(sum(c["value"] for c in priced), 2),
            "valued": len(priced), "top": mine[:5], "dupes_value": round(dupes, 2)}


def view(con, rows: list[dict], gs: list[dict], now: datetime) -> dict:
    sold = [s for s in sales.with_calls(con) if not ledger.LOT.search(s["title"] or "")]
    sold.sort(key=lambda s: -s["total"])
    dollars = sum(s["total"] for s in sold)
    k = max(1, round(TOP_SHARE * len(sold)))
    top_dollars = sum(s["total"] for s in sold[:k])
    over = overpaid(gs)
    return {
        "n": len(sold), "dollars": round(dollars, 2), "median": round(statistics.median(s["total"] for s in sold), 2) if sold else 0,
        "top": [_sale(s) for s in sold[:TOP]],
        "ladder": ladder(sold),
        "drivers": sorted(({"family": f, "n": len(xs), "median": round(statistics.median(xs), 2), "max": round(max(xs), 2)}
                           for f, xs in _by_family(sold).items() if len(xs) >= 3), key=lambda d: -d["median"])[:DRIVERS],
        "overpaid": over[:TOP], "overpaid_n": len(over), "overpaid_dollars": round(sum(o["over"] for o in over), 2),
        "concentration": {"top_n": k, "share": round(100 * top_dollars / dollars) if dollars else 0,
                          "dollars": round(top_dollars, 2)},
        "trend": trend(sold, now.timestamp()),
        "yours": yours(rows, gs),
    }


def _by_family(sold: list[dict]) -> dict[str, list[float]]:
    by = defaultdict(list)
    for s in sold:
        by[family(s["title"])].append(s["total"])
    return by
