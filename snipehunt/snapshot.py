"""Everything the Home Assistant integration shows, as one snapshot.

api.py serves this to the Home Assistant integration, which turns it into
sensors and feeds the dashboard cards: live picks, the collection, snipe
results, the Gixen list, the bot's health and its settings.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timedelta

from . import config, db, gixen, inventory, profile, report, sales, scheduler, settings
from . import usage
from .notify import TIER_ORDER

VERSION = 1
LIVE_WINDOW = 2 * 3600   # a pick is live if its listing was in a recent search
RECORD_DAYS = 30
MAX_PICKS = 300
SKIPPED_KEY = "skipped_picks"
LAST_ACTION_KEY = "last_action"
GOOD = ("fire", "green")


def _short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _slim(pick: dict) -> dict:
    return {
        "item_id": pick["item_id"], "card": _short(pick.get("card"), 90),
        "tier": pick.get("tier"), "price": pick.get("price"),
        "shipping": pick.get("shipping"), "total": pick.get("total"),
        "max": pick.get("max"), "max_basis": pick.get("max_basis"),
        "is_auction": bool(pick.get("is_auction")),
        "best_offer": bool(pick.get("best_offer")),
        "end_time": pick.get("end_time"), "seller": pick.get("seller"),
        "why": _short(pick.get("why"), 170), "url": pick.get("url"),
        "image": pick.get("image"), "snipe": pick.get("snipe"),
        "bid_now": pick.get("bid_now"), "value": pick.get("value"),
        "deal": pick.get("deal"), "deal_pct": pick.get("deal_pct"),
        "edge": pick.get("edge"),
    }


def skipped_ids(con) -> list[str]:
    """Listings hidden from the dashboard with the Skip button."""
    return json.loads(db.kv_get(con, SKIPPED_KEY, "[]"))


def live_picks(con, now: datetime) -> list[dict]:
    """Picks whose listing is still up and, for auctions, still running."""
    rows = con.execute(
        "SELECT p.data FROM picks p JOIN seen s USING (item_id) "
        "WHERE s.last_seen >= ?", (int(now.timestamp()) - LIVE_WINDOW,))
    skipped = set(skipped_ids(con))
    picks = [json.loads(r["data"]) for r in rows]
    picks = [p for p in picks if p["item_id"] not in skipped]
    return [p for p in picks if not (
        p.get("is_auction") and p.get("end_time")
        and datetime.fromisoformat(p["end_time"]) <= now)]


def ranked(picks: list[dict]) -> list[dict]:
    """Best first: tier, then cheapest, then ending soonest."""
    return sorted(picks, key=lambda p: (TIER_ORDER.get(p.get("tier"), 9),
                                        p.get("total") or 0, p.get("end_time") or "9"))


def need_snipe(picks: list[dict], now: datetime, hours: float,
               gixen_on: bool) -> list[str]:
    """Good auctions ending soon that have no snipe (all of them if Gixen is off)."""
    soon = (now + timedelta(hours=hours)).isoformat()
    return [p["item_id"] for p in sorted(picks, key=lambda p: p.get("end_time") or "9")
            if p.get("is_auction") and p.get("end_time") and p.get("tier") in GOOD
            and p["end_time"] <= soon and not (gixen_on and p.get("snipe") is not None)]


def _price(row: dict) -> float:
    try:
        return float(row["price_paid"])
    except (KeyError, ValueError):
        return 0.0


def _row(row: dict) -> dict:
    return {"card": _short(report.card_name(row), 90), "status": row.get("status"),
            "paid": _price(row), "date": row.get("order_date"),
            "seller": row.get("seller"), "page": report.binder_page(row)}


def cards(rows: list[dict]) -> list[dict]:
    """Every card, compact, for the searchable binder list."""
    out = []
    for r in rows:
        flags = "".join(letter for letter, on in (
            ("R", r.get("rc") == "Y"), ("A", r.get("auto") == "Y"),
            ("L", r.get("relic") == "Y"), ("N", bool(r.get("serial"))),
            ("G", bool(r.get("graded"))), ("F", r.get("front_page") == "Y"),
            ("D", r.get("dupe") == "Y")) if on)
        out.append({"id": r.get("id"), "n": _short(report.card_name(r), 80),
                    "s": r.get("status"), "p": _price(r), "d": r.get("order_date"),
                    "pg": report.binder_page(r), "f": flags,
                    "sl": _short(r.get("seller"), 20)})
    return out


def collection(rows: list[dict], now: datetime) -> dict:
    owned = [r for r in rows if r.get("status") != "Sold-Traded"]
    tot = report.totals(rows)
    month = now.strftime("%Y-%m")
    this_month = [r for r in owned if (r.get("order_date") or "").startswith(month)]
    priced = sorted((r for r in owned if _price(r) > 0), key=_price, reverse=True)
    years = Counter(r.get("year") or "TBD" for r in owned)
    return {
        "cards": tot["cards"], "spent": tot["spent"],
        "avg_paid": round(tot["spent"] / len(priced), 2) if priced else 0,
        "month": month, "month_cards": len(this_month),
        "month_spent": round(sum(_price(r) for r in this_month), 2),
        "by_status": dict(Counter(r.get("status") or "?" for r in owned)),
        "by_year": dict(sorted(years.items())),
        "rookies": sum(r.get("rc") == "Y" for r in owned),
        "autos": sum(r.get("auto") == "Y" for r in owned),
        "relics": sum(r.get("relic") == "Y" for r in owned),
        "numbered": sum(bool(r.get("serial")) for r in owned),
        "graded": sum(bool(r.get("graded")) for r in owned),
        "front_page": sum(r.get("front_page") == "Y" for r in owned),
        "dupes": sum(r.get("dupe") == "Y" for r in owned),
        "priciest": [_row(r) for r in priced[:5]],
    }


def pipeline(rows: list[dict], picks: list[dict]) -> dict:
    """Cards on the way: unpaid wins by seller, packages in transit, to file."""
    unpaid = [{"seller": g["seller"], "advice": g["advice"],
               "total": round(sum(_price(c) for c in g["cards"]), 2),
               "cards": [_short(report.card_name(c), 70) for c in g["cards"][:6]]}
              for g in report.unpaid_by_seller(rows, picks)]
    return {
        "unpaid": unpaid[:6],
        "unpaid_total": round(sum(g["total"] for g in unpaid), 2),
        "unpaid_cards": sum(r.get("status") in report.UNPAID for r in rows),
        "in_transit": sum(r.get("status") == "In transit" for r in rows),
        "to_file": sum(r.get("status") == "Delivered" for r in rows),
    }


def record(con, now: datetime) -> dict:
    """Snipe results from the Gixen and eBay emails over the last month."""
    since = (now - timedelta(days=RECORD_DAYS)).date().isoformat()
    week = (now - timedelta(days=7)).date().isoformat()
    wins, losses = report.wins_and_losses(db.mail_events(con, since))
    decided = len(wins) + len(losses)

    def brief(ev: dict) -> dict:
        return {"title": _short(ev.get("title"), 80), "date": ev.get("date"),
                "price": ev.get("price"), "seller": ev.get("seller")}
    return {
        "rate": round(100 * len(wins) / decided) if decided else None,
        "days": RECORD_DAYS, "wins": len(wins), "losses": len(losses),
        "gixen_wins": sum(e.get("source") == "gixen" for e in wins),
        "gixen_losses": sum(e.get("source") == "gixen" for e in losses),
        "wins_week": sum(e.get("date", "") >= week for e in wins),
        "losses_week": sum(e.get("date", "") >= week for e in losses),
        "recent_losses": [brief(e) for e in reversed(losses[-5:])],
    }


def gixen_view(sniped: list[dict], problem: str | None, read_at: float,
               picks: list[dict], now: datetime) -> dict:
    """Scheduled snipes, with the bot's own max next to yours where it has one."""
    by_pick = {p["item_id"]: p for p in picks}
    upcoming = sorted((s for s in sniped if s["status"] == "SCHEDULED" and s["end_time"]
                       and datetime.fromisoformat(s["end_time"]) > now),
                      key=lambda s: s["end_time"])
    rows = []
    for s in upcoming:
        pick = by_pick.get(s["item_id"], {})
        rows.append({"item_id": s["item_id"], "end_time": s["end_time"],
                     "max_bid": s["max_bid"], "current_bid": s.get("current_bid"),
                     "outbid": bool(s["max_bid"] and (s.get("current_bid") or 0)
                                    >= s["max_bid"]),
                     "title": _short(pick.get("card") or s["title"], 90),
                     "seller": s.get("seller"),
                     "bot_max": pick.get("max"), "tier": pick.get("tier"),
                     "url": f"https://www.ebay.com/itm/{s['item_id']}"})
    alive = [r for r in rows if not r["outbid"]]
    return {
        "available": bool(read_at), "problem": problem,
        "read_at": datetime.fromtimestamp(read_at, config.TZ).isoformat()
        if read_at else None,
        "snipes": rows, "outbid_count": len(rows) - len(alive),
        "committed": round(sum(r["max_bid"] or 0 for r in alive), 2),
    }


def wantlist() -> list[dict]:
    targets = []
    if not config.WANTLIST.exists():
        return targets
    for line in config.WANTLIST.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if (len(cells) >= 2 and cells[0] != "Target"
                and not re.fullmatch(r"[-: ]*", cells[0])
                and line.lstrip().startswith("|")):
            targets.append({"target": cells[0], "ceiling": cells[1],
                            "notes": cells[2] if len(cells) > 2 else ""})
    return targets


def status(con, picks: list[dict], now: datetime) -> dict:
    ts = int(now.timestamp())
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]   # noqa: E731
    stamp = config.HEALTHZ.read_text().split() if config.HEALTHZ.exists() else []
    last_ok = int(stamp[0]) if stamp and stamp[0].isdigit() else None
    failing = [k.split(":", 1)[1] for k in ("failing:hourly", "failing:daily",
                                            "failing:weekly")
               if db.kv_get(con, k) == "1"]
    last_search = one("SELECT max(last_seen) FROM seen") or 0
    stale = ts - last_search > LIVE_WINDOW
    tiers = Counter(p.get("tier") for p in picks)
    auctions = sum(bool(p.get("is_auction") and p.get("end_time")) for p in picks)
    return {
        "state": "failing" if failing else ("stale" if stale else "ok"),
        "failing": failing,
        "last_ok": datetime.fromtimestamp(last_ok, config.TZ).isoformat()
        if last_ok else None,
        "last_ok_job": stamp[1] if len(stamp) > 1 else None,
        "last_search": datetime.fromtimestamp(last_search, config.TZ).isoformat()
        if last_search else None,
        "listings": one("SELECT count(*) FROM seen WHERE last_seen >= ?",
                        ts - LIVE_WINDOW),
        "seen_total": one("SELECT count(*) FROM seen"),
        "new_24h": one("SELECT count(*) FROM seen WHERE first_seen >= ?", ts - 86400),
        "scored_24h": one("SELECT count(*) FROM seen WHERE scored_at >= ?", ts - 86400),
        "picks_24h": one("SELECT count(*) FROM picks WHERE created_at >= ?", ts - 86400),
        "alerts_24h": one("SELECT count(*) FROM seen WHERE alerted_at >= ?", ts - 86400),
        "live_picks": len(picks), "auctions": auctions, "buy_now": len(picks) - auctions,
        "fire": tiers["fire"], "green": tiers["green"], "yellow": tiers["yellow"],
        "sales_based": sum(p.get("max_basis") == "sales" for p in picks),
        "sold_prices": one("SELECT count(*) FROM sales"),
        "sold_prices_7d": one("SELECT count(*) FROM sales WHERE sold_at >= ?",
                              ts - 7 * 86400),
        "held_overnight": len(json.loads(db.kv_get(con, "pending_picks", "[]"))),
        "searching": scheduler.running("hourly"),
        "skipped": len(skipped_ids(con)),
        "last_report": max((p.name for p in config.REPORTS_DIR.glob("*.md")),
                           default=None),
    }


def snapshot(con=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    con = con or db.connect()
    cfg = settings.load(con)
    _, rows = inventory.read()
    picks = live_picks(con, now)
    gix = None
    if cfg["gixen_enabled"] and config.get("GIXEN_USER"):
        sniped, problem, read_at = gixen.snipes(con)
        by_item = {s["item_id"]: s for s in sniped}
        for p in picks:   # lets every pick list show which ones are covered
            p["snipe"] = by_item.get(p["item_id"], {}).get("max_bid")
            p["bid_now"] = by_item.get(p["item_id"], {}).get("current_bid")
        gix = gixen_view(sniped, problem, read_at, picks, now)
    picks = ranked(picks)
    state = status(con, picks, now)
    state.update(usage.summary(con, now))
    state["need_snipe"] = need_snipe(picks, now, cfg["need_snipe_hours"],
                                     bool(gix and gix["available"]))
    return {
        "api": VERSION,
        "profile": profile.get().public(),
        "generated": now.isoformat(timespec="seconds"),
        "status": state,
        "picks": [_slim(p) for p in picks[:MAX_PICKS]],
        "gixen": gix,
        "cards": cards(rows),
        "collection": collection(rows, now),
        "pipeline": pipeline(rows, picks),
        "record": record(con, now),
        "market": sales.market(con, now),
        "wantlist": wantlist(),
        "settings": {key: cfg[key] for key in settings.visible()},
        "last_action": json.loads(db.kv_get(con, LAST_ACTION_KEY, "null")),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Print the snapshot the integration reads")
    ap.add_argument("--full", action="store_true", help="print all of it as JSON")
    args = ap.parse_args()
    snap = snapshot()
    if args.full:
        print(json.dumps(snap, indent=1, ensure_ascii=False))
        return
    for key, value in snap.items():
        size = len(json.dumps(value, ensure_ascii=False).encode())
        count = f"{len(value)} rows, " if isinstance(value, list) else ""
        print(f"{key:12} {count}{size} bytes")


if __name__ == "__main__":
    main()
