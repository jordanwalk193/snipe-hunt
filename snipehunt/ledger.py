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
import statistics
from datetime import datetime

from . import config, db, sales

TREND_MIN = 3
RECENT_DAYS = 30
WINDOW_DAYS = 120
MOVE_PCT = 10
WORTH_KEY = "worth_weeks"   # kv: {"cur": {"week", "values"}, "prev": {...}}
TOP = 8


def groups(con) -> list[dict]:
    """Every card in the ledger with its sales, newest first (sales.history, uncapped)."""
    out: list[dict] = []
    for row in sales.with_calls(con):
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
    return sales._number_match(title, g["title"]) is not False and (
        getattr(sales, "same_player", None) is None or sales.same_player(title, g["title"]))


def find(gs: list[dict], title: str) -> dict | None:
    key, kind = sales.card_key(title), sales.traits(title)
    return next((g for g in gs if g["traits"] == kind and _alike(g, key, title)), None)


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


def worth(con, rows: list[dict], gs: list[dict], now: datetime) -> dict:
    """The collection by the ledger: each card at the newest sale of the same card."""
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
        g = find(gs, name) if name else None
        sale = g["sales"][0] if g else None
        value = sale["total"] if sale else paid
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
