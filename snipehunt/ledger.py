"""What the ledger says over time: which cards are rising or falling, and what the
collection is worth by the latest sales.

- trends: a card that has sold TREND_MIN times or more inside WINDOW_DAYS is
  compared, its sales of the last RECENT_DAYS against the ones before. A move
  of MOVE_PCT either way is a trend; picks of that card carry the tag.
- worth: every card in the inventory priced by the newest sale of the same
  card in the ledger (what you paid, when the ledger has none), with the
  week's movers against the week before.

Nothing here reaches eBay or Claude; it is arithmetic on sales already kept.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime

from . import config, db, sales

TREND_MIN = 3
RECENT_DAYS = 30
WINDOW_DAYS = 120
MOVE_PCT = 10
WORTH_KEY = "worth_weeks"   # kv: {"cur": {"week", "values"}, "prev": {...}}
TOP = 8


LOT = re.compile(r"\blots?\b|\(\d+\)|\bx\s?\d+\b|\b\d+\s?(cards|card lot)\b", re.I)


def groups(con) -> list[dict]:
    """Every card in the ledger with its sales, newest first (sales.history, uncapped).
    Lots are left out: five cards for $9 says nothing about one."""
    out: list[dict] = []
    for row in sales.with_calls(con):
        if LOT.search(row["title"] or ""):
            continue
        key, kind = sales.card_key(row["title"]), sales.traits(row["title"])
        g = next((g for g in out if g["traits"] == kind and _alike(g, key, row["title"])), None)
        if g is None:
            g = {"title": row["title"], "image": row["image"], "item_id": row["item_id"],
                 "key": key, "traits": kind, "sales": []}
            out.append(g)
        g["sales"].append(row)
    return out


def _alike(g: dict, key: frozenset, title: str) -> bool:
    """sales._same, with the group's key and traits already in hand."""
    if not key or not g["key"]:
        return False
    if len(key & g["key"]) / len(key | g["key"]) < sales.SAME_CARD:
        return False
    if _stems(key ^ g["key"]) & _stems(PARALLEL_WORDS):
        return False   # "Refractor" on one title only: a parallel, not the same card
    return sales._number_match(title, g["title"]) is not False and (
        getattr(sales, "same_player", None) is None or sales.same_player(title, g["title"]))


def find(gs: list[dict], title: str) -> dict | None:
    key, kind = sales.card_key(title), sales.traits(title)
    return next((g for g in gs if g["traits"] == kind and _alike(g, key, title)), None)



# ------------------------------------------------------------ the collection's own cards
# An inventory row is structured (year, set, card, parallel, number, serial); a sale
# title is not. Matching by word overlap alone misses the plain cards (a title's
# extra words dilute it) and mixes up parallels, so a row is matched by its parts.
# Words that mark a parallel: a title shows the row's, and none the row lacks.
PARALLEL_WORDS = frozenset("""refractor xfractor fractor prism prizm gold blue red green orange
purple pink black aqua sapphire mojo wave speckle shimmer atomic sepia negative foil foilboard
holo silver rainbow lava raywave magenta fuchsia lime yellow teal bronze platinum plate
superfractor ssp variation image photo mini glitter pulsar velocity scope cracked ice disco camo
tiger zebra giraffe snakeskin elephant hyper neon lazer laser aurora cosmic stars nebula
galactic eclipse padparadscha metallic sparkle mega glossy logofractor geometric seams lightboard
onyx amethyst emerald ruby sandglitter""".split())
# Words that name a product: a title with one the row's set lacks is another set.
SET_WORDS = frozenset("""chrome bowman heritage sapphire update holiday archives finest gallery
stadium club gilded dynasty inception select mosaic donruss optic diamond kings allen ginter
big league opening day pristine tier museum triple threads tribute definitive five star
luminaries transcendent 206 leaf perfect game pro debut living now fire gypsy queen elite
chronicles immaculate flawless national treasures spectra obsidian absolute contenders draft
minors sterling best invicta platinum""".split())
# set words a title may leave out without being another set
SOFT_SET_WORDS = frozenset("edition series topps panini bowmans".split())
_RUN = re.compile(r"/\s?(\d{1,5})\b")
# words of the row's "card" field that say nothing about which card it is
GENERIC = frozenset("base insert prospect prospects rookie card design variation parallel paper".split())


def _stems(ws) -> frozenset[str]:
    """"refractors" is "refractor": the plural a title uses must not hide a parallel."""
    return frozenset(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in ws)


def row_parts(row: dict) -> dict:
    """What identifies an inventory row to a sale title."""
    serial = _RUN.search(str(row.get("serial") or ""))
    return {
        "year": str(row.get("year") or "").strip(),
        "set": _stems(sales.words(str(row.get("set") or ""))) - {"base"},
        "own": (_stems(sales.words(f"{row.get('set') or ''} {row.get('card') or ''} {row.get('parallel') or ''}"))
                - {"base"}),
        "card": frozenset(w for w in _stems(sales.words(str(row.get("card") or "")))
                          if w not in GENERIC and w not in SOFT_SET_WORDS and not w.isdigit()),
        "parallel": _stems(sales.words(str(row.get("parallel") or ""))) - {"base"},
        "number": str(row.get("card_no") or "").strip(),
        "run": serial.group(1) if serial else None,
        "auto": str(row.get("auto") or "").strip().upper() == "Y",
        "graded": bool(str(row.get("graded") or "").strip()),
    }


def _row_fit(parts: dict, g: dict) -> tuple | None:
    """How well a ledger card fits the row: None when it is another card, else a
    sort key (higher is better)."""
    title = g["title"]
    if not parts["year"] or sales._year(title) != parts["year"]:
        return None
    grade, auto, run = g["traits"]
    if bool(auto) != parts["auto"] or (grade is not None) != parts["graded"] or run != parts["run"]:
        return None
    numbered = None
    if parts["number"]:
        numbered = sales._number_match(f"#{parts['number']}", title)
        if numbered is False:
            return None
    tw = _stems(sales.words(title))
    if not parts["parallel"] <= tw or not parts["card"] <= tw:
        return None
    hard = parts["set"] - SOFT_SET_WORDS
    if not hard <= tw and not (numbered is True and len(hard & tw) * 2 >= len(hard)):
        return None   # an insert's title may drop "Chrome" when its number says which card
    if not parts["number"] and not parts["card"] and sales.card_number(title):
        return None   # "2026 Topps Now" alone can't claim a numbered card
    number_words = _stems(sales.words(f"{parts['number']} {parts['run'] or ''}")) | {parts["year"]}
    extra = tw - parts["own"] - number_words
    if extra & _stems(PARALLEL_WORDS | SET_WORDS):
        return None
    return (numbered is True, len(parts["set"] & tw), len(g["sales"]))


def match_row(gs: list[dict], row: dict) -> dict | None:
    """The ledger card an inventory row is, by year, number, parallel, print run,
    autograph and grade (not by word overlap: see row_parts)."""
    parts = row_parts(row)
    best, best_fit = None, None
    for g in gs:
        fit = _row_fit(parts, g)
        if fit is not None and (best_fit is None or fit > best_fit):
            best, best_fit = g, fit
    return best

def trend(g: dict, now: float) -> dict | None:
    """How this card's price moved: the last RECENT_DAYS against the sales before."""
    pts = sorted((s["sold_at"], s["total"]) for s in g["sales"] if s["sold_at"] >= now - WINDOW_DAYS * 86400)
    if len(pts) < TREND_MIN:
        return None
    cut = now - RECENT_DAYS * 86400
    prior, recent = [t for ts, t in pts if ts < cut], [t for ts, t in pts if ts >= cut]
    if not recent or len(prior) < 2:   # too lopsided: compare the older half with the newer
        half = len(pts) // 2
        prior, recent = [t for _, t in pts[:half]], [t for _, t in pts[half:]]
    was, is_now = statistics.median(prior), statistics.median(recent)
    if was <= 0:
        return None
    pct = round(100 * (is_now - was) / was)
    return {"pct": pct, "n": len(pts), "now": round(is_now, 2), "was": round(was, 2),
            "dir": "up" if pct >= MOVE_PCT else "down" if pct <= -MOVE_PCT else "flat"}


def trends(gs: list[dict], now: float, limit: int = TOP) -> dict:
    """For the dashboard: the cards moving most, up and down."""
    moved = []
    for g in gs:
        t = trend(g, now)
        if t and t["dir"] != "flat":
            moved.append({"card": g["title"][:90], "image": g["image"],
                          "url": f"https://www.ebay.com/itm/{g['item_id']}", **t})
    moved.sort(key=lambda m: -abs(m["pct"]))
    return {"cards": sum(1 for g in gs if len(g["sales"]) >= TREND_MIN),
            "up": [m for m in moved if m["dir"] == "up"][:limit],
            "down": [m for m in moved if m["dir"] == "down"][:limit]}


def tag(picks: list[dict], gs: list[dict], now: float) -> None:
    """Give each pick of a card with a trend its "trend"."""
    for p in picks:
        g = find(gs, p.get("title") or p.get("card") or "")
        t = trend(g, now) if g else None
        if t and t["dir"] != "flat":
            p["trend"] = t
        else:
            p.pop("trend", None)


VALUE_SALES = 3   # a card is worth the median of its newest few sales, so one mis-titled sale can't move it


def value(g: dict | None) -> float | None:
    """What a ledger card is worth: the median of its newest VALUE_SALES sales.
    The newest sale alone let one stray (a slab or a parallel sold under a bare title)
    make a $2 base card look like $25."""
    if not g or not g.get("sales"):
        return None
    totals = sorted(float(s["total"]) for s in g["sales"][:VALUE_SALES])
    mid = len(totals) // 2
    return round(totals[mid] if len(totals) % 2 else (totals[mid - 1] + totals[mid]) / 2, 2)


def worth(con, rows: list[dict], gs: list[dict], now: datetime) -> dict:
    """The collection by the ledger: each card at the median of its newest sales (value())."""
    from . import report   # report imports this module's neighbours; kept local
    week = now.strftime("%G-W%V")
    cards, paid_all, value_all, valued = [], 0.0, 0.0, 0
    for r in rows:
        status = (r.get("status") or "").strip().lower()
        if status.startswith("sold"):
            continue
        try:
            paid = float(r.get("price_paid") or 0)
        except ValueError:
            paid = 0.0
        name = report.card_name(r)
        g = match_row(gs, r)
        sale = g["sales"][0] if g else None
        value = globals()["value"](g) if sale else paid
        valued += sale is not None
        paid_all += paid
        value_all += value
        cards.append({"id": r.get("id"), "card": name, "paid": paid, "value": round(value, 2),
                      "by": "sales" if sale else "paid", "n": len(g["sales"]) if g else 0,
                      "at": datetime.fromtimestamp(sale["sold_at"], config.TZ).date().isoformat() if sale else None})
    # week over week: what each card was worth last week, from the kv
    kept = json.loads(db.kv_get(con, WORTH_KEY, "{}") or "{}")
    cur = {str(c["id"]): c["value"] for c in cards if c["by"] == "sales"}
    if kept.get("cur", {}).get("week") != week:
        kept = {"prev": kept.get("cur"), "cur": {"week": week, "values": cur}}
    else:
        kept["cur"] = {"week": week, "values": cur}
    db.kv_set(con, WORTH_KEY, json.dumps(kept))
    con.commit()
    prev = (kept.get("prev") or {}).get("values") or {}
    movers = []
    for c in cards:
        was = prev.get(str(c["id"]))
        if was and c["by"] == "sales" and abs(c["value"] - was) >= 1:
            pct = round(100 * (c["value"] - was) / was)
            if abs(pct) >= MOVE_PCT:
                movers.append({**c, "was": was, "pct": pct})
    movers.sort(key=lambda m: -abs(m["pct"]))
    return {"n": len(cards), "valued": valued, "paid": round(paid_all, 2), "value": round(value_all, 2),
            "gain": round(value_all - paid_all, 2), "week": week,
            "movers": movers[:TOP], "top": sorted(cards, key=lambda c: -c["value"])[:TOP],
            "since": (kept.get("prev") or {}).get("week")}
