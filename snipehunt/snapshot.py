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

from . import (config, db, gixen, inventory, jackpot, lowball, musthave, offers, profile,
               rainbow, rarity, report, sales, scheduler, score, settings, shortlist,
               snipecheck, spending, tune, watch)
from . import bidnow, brag, calibrate, closes, steals, usage
from .notify import TIER_ORDER

VERSION = 1
LIVE_WINDOW = 2 * 3600   # a pick is live if its listing was in a recent search
RECORD_DAYS = 30
MAX_PICKS = 300
SKIPPED_KEY = "skipped_picks"
MARKED_KEY = "marked_sniped"   # auctions you said you put a snipe on, until Gixen shows it
MARK_DAYS = 14                 # a mark older than this is forgotten (actions.mark_sniped)
LAST_ACTION_KEY = "last_action"
GOOD = ("fire", "green")


def _short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _slim(pick: dict) -> dict:
    return {
        "item_id": pick["item_id"], "card": _short(pick.get("card"), 90),
        "title": pick.get("title"),
        "tier": pick.get("tier"), "price": pick.get("price"),
        "shipping": pick.get("shipping"), "total": pick.get("total"),
        "max": pick.get("max"), "max_basis": pick.get("max_basis"),
        "is_auction": bool(pick.get("is_auction")),
        "best_offer": bool(pick.get("best_offer")),
        "end_time": pick.get("end_time"), "seller": pick.get("seller"),
        "why": _short(pick.get("why"), 170), "url": pick.get("url"),
        "image": pick.get("image"), "snipe": pick.get("snipe"),
        # only when there is something to say, so most picks stay as they were
        **{k: pick[k] for k in ("marked", "bin_price", "closes", "jackpot_bin") if pick.get(k)},
        "bid_now": pick.get("bid_now"), "value": pick.get("value"),
        # when eBay last gave this price (a search, or bids.py near the end), and
        # an auction's bid count then
        "price_at": pick.get("price_at"), "bids": pick.get("bids"),
        "jackpot": pick.get("jackpot"),   # "sales" or "guess" when it is one (jackpot.py)
        "deal": pick.get("deal"), "deal_pct": pick.get("deal_pct"),
        "edge": pick.get("edge"), "fast": bool(pick.get("fast")),
        "feedback": pick.get("feedback"),
        "unverified": bool(pick.get("unverified")), "want": bool(pick.get("want")),
        "seller_feedback": pick.get("seller_feedback"), "seller_pct": pick.get("seller_pct"),
        "new_seller": bool(pick.get("new_seller")),
        # older picks were saved before the win bid existed; work it out for them too
        "win": sales.win_bid(pick.get("value"), pick.get("shipping"), pick.get("max"))
        if pick.get("is_auction") else None,
        # team mode: who is on the card
        **({"player": pick["player"]} if pick.get("player") else {}),
        "usual_paid": pick.get("usual_paid"),   # what you usually pay for this kind of card
    }


def _with_last_sold(con, picks: list[dict]) -> list[dict]:
    """Each pick with the latest recorded sale of the same card, when there is one."""
    rows = sales.with_calls(con)
    for pick in picks:
        pick["last_sold"] = sales.last_sold(rows, pick.get("title") or pick["card"],
                                            pick["item_id"]) if rows else None
        pick.pop("title", None)
    return picks


def marked(con) -> dict[str, dict]:
    """The "Added to Gixen" marks: item id -> {"at": epoch, "max": amount}."""
    return json.loads(db.kv_get(con, MARKED_KEY, "{}") or "{}")


def skipped_ids(con) -> list[str]:
    """Listings hidden from the dashboard with the Skip button."""
    return json.loads(db.kv_get(con, SKIPPED_KEY, "[]"))


def live_picks(con, now: datetime) -> list[dict]:
    """Picks whose listing is still up and, for auctions, still running."""
    rows = con.execute(
        "SELECT p.data, s.price, s.shipping, s.bids, s.last_seen, s.bid_at "
        "FROM picks p JOIN seen s USING (item_id) "
        "WHERE s.last_seen >= ?", (int(now.timestamp()) - LIVE_WINDOW,))
    skipped = set(skipped_ids(con))
    picks = []
    for r in rows:
        p = held_to_steals(current(json.loads(r["data"]), r["price"], r["shipping"]))
        if p is not None:   # when eBay last told us this price, and the bids behind it
            at = max(r["last_seen"] or 0, r["bid_at"] or 0)
            p["price_at"] = datetime.fromtimestamp(at, config.TZ).isoformat(
                timespec="minutes") if at else None
            if p.get("is_auction"):
                p["bids"] = r["bids"]
        picks.append(p)
    picks = [p for p in picks if p and p["item_id"] not in skipped]
    return [p for p in picks if not (
        p.get("is_auction") and p.get("end_time")
        and datetime.fromisoformat(p["end_time"]) <= now)]


def current(pick: dict, price: float | None, shipping: float | None) -> dict | None:
    """The pick at the latest search's price, or None once it is no longer a pick.

    A pick is saved when it is scored, but bids keep rising and Buy It Nows get
    cut: the dashboard must show what eBay shows now. An auction already bid up
    to the bot's max can't be won at that max, so it leaves the list."""
    if price is None or (price == pick.get("price") and shipping in (None, pick.get("shipping"))):
        return pick
    pick = dict(pick, price=price)
    if shipping is not None:
        pick["shipping"] = shipping
    ship = 0.0 if pick.get("combined_ship") else (pick.get("shipping") or 0.0)
    pick["total"] = round(price + ship, 2)
    if pick.get("is_auction") and pick.get("max") is not None and price >= pick["max"]:
        return None
    return score.rerate(pick)


def held_to_steals(pick: dict | None) -> dict | None:
    """Steals mode: a saved pick held to max_share at today's price (picks saved before
    the setting changed too). Unchanged with max_share at 1.0."""
    if pick is None:
        return None
    fitted = steals.fit(pick)
    if fitted is None:
        return None
    return score.rerate(fitted) if fitted is not pick else pick


def ranked(picks: list[dict]) -> list[dict]:
    """Best first: real steals ahead of fair-price picks, then cards that slipped
    through the cracks, then tier, most deal stars, cheapest, ending soonest."""
    return sorted(picks, key=lambda p: (
        p.get("deal") == 1, not p.get("edge"), TIER_ORDER.get(p.get("tier"), 9),
        -(p.get("deal") or 0), p.get("total") or 0, p.get("end_time") or "9"))


BOARD_SIZE = 25


def steal_order(picks: list[dict]) -> list[dict]:
    """Team mode's board: the biggest discount to the going rate first, then stars,
    then the cheapest. A pick with a guessed price or one star isn't a steal."""
    good = [p for p in picks if not p.get("unverified") and (p.get("deal") or 0) >= 2]
    return sorted(good, key=lambda p: (-(p.get("deal_pct") or 0), -(p.get("deal") or 0),
                                       p.get("total") or 0))


def by_player(picks: list[dict]) -> list[dict]:
    """Each player's live picks: how many, the best steal and the cheapest card."""
    groups: dict[str, list[dict]] = {}
    for p in picks:
        groups.setdefault(p.get("player") or "unknown", []).append(p)
    out = []
    for name, group in groups.items():
        best = max(group, key=lambda p: (p.get("deal_pct") or 0, p.get("deal") or 0))
        cheap = min(group, key=lambda p: p.get("total") or 0)
        out.append({"player": name, "picks": len(group), "best": best["item_id"],
                    "best_deal_pct": best.get("deal_pct"), "best_card": best.get("card"),
                    "cheapest_total": cheap.get("total"), "cheapest": cheap["item_id"],
                    "saved": round(sum(max(0.0, (p.get("value") or 0) - (p.get("total") or 0))
                                       for p in group), 2)})
    out.sort(key=lambda g: (-g["picks"], -(g["best_deal_pct"] or 0)))
    return out


def team_view(con, picks: list[dict], cfg: dict) -> dict:
    """What a team bot's dashboard adds: the steal board, players, the free funnel,
    seller bundles and the day's Claude budget."""
    from .jobs import team_budget   # jobs imports most of the package; kept local
    who = profile.get()
    board = steal_order(picks)
    return {"name": who.team, "roster": list(who.roster),
            "board": [_slim(p) for p in board[:BOARD_SIZE]],
            "players": by_player(picks), **shortlist.load(con),
            "budget": team_budget(con, cfg), "per_run": cfg["team_claude_per_run"]}


def need_snipe(picks: list[dict], now: datetime, hours: float,
               gixen_on: bool) -> list[str]:
    """Good auctions ending soon that have no snipe (all of them if Gixen is off)."""
    soon = (now + timedelta(hours=hours)).isoformat()
    return [p["item_id"] for p in sorted(picks, key=lambda p: p.get("end_time") or "9")
            if p.get("is_auction") and p.get("end_time") and p.get("tier") in GOOD
            and p["end_time"] <= soon and not (gixen_on and p.get("snipe") is not None)
            and not p.get("marked")]


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



WEEKLY_REPORT = re.compile(r"\d{4}-\d{2}-\d{2}\.md")   # reports/YYYY-MM-DD.md


def latest_report() -> str | None:
    """The newest weekly report. Only dated names count: any other .md file in
    the reports folder could sort after every date and hide the real ones."""
    return max((p.name for p in config.REPORTS_DIR.glob("*.md")
                if WEEKLY_REPORT.fullmatch(p.name)), default=None)


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
        "feedback": one("SELECT count(*) FROM feedback WHERE source='you'"),
        "last_report": latest_report(),
        "last_self_review": max((p.name for p in config.REPORTS_DIR.glob("reflect-*.md")),
                                default=None),
    }


def live_listings(con, now: datetime) -> list[dict]:
    """Every listing from the recent searches: its title and delivered price."""
    since = int(now.timestamp()) - LIVE_WINDOW
    return [{"title": r["title"], "total": sales.delivered(r["price"] or 0, r["shipping"])}
            for r in con.execute("SELECT title, price, shipping FROM seen "
                                 "WHERE last_seen >= ? AND price IS NOT NULL", (since,))]


def lowball_view(con, now: datetime) -> dict:
    found = lowball.load(con)
    if found.get("targets"):
        found["targets"] = [t for t in found["targets"]
                            if datetime.fromisoformat(t["end_time"]) > now]
    return found


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
        # how each of your amounts sits against what the card goes for
        checks, gix["analysis"] = snipecheck.check(con, gix["snipes"])
        # and anything else wrong with them: wrong card, a dupe, sniped twice...
        # (from Gixen's own titles: the rows show the bot's short card name, which
        # can leave out the player's name)
        raw = {x["item_id"]: x for x in sniped}
        found = snipecheck.doubts(con, [raw.get(r["item_id"], r) for r in gix["snipes"]], rows)
        for row, verdict, doubt in zip(gix["snipes"], checks, found):
            row["check"], row["doubts"] = verdict, doubt
        gix["analysis"]["flagged"] = sum(
            bool(d) or c["verdict"] == "over" for c, d in zip(checks, found))
    # "Added to Gixen" marks: shown until Gixen's list confirms the snipe, so a pick
    # isn't sniped twice while the list is minutes old
    marks = marked(con)
    closing = closes.learn(con)
    for p in picks:
        if p["item_id"] in marks and p.get("snipe") is None:
            p["marked"] = marks[p["item_id"]]
        if p.get("is_auction") and not p.get("closes"):   # picks scored before this was learned
            closes.annotate(p, closing)
    given = {f["item_id"]: f["reason"] for f in db.feedback(con)[::-1] if f["source"] == "you"}
    for p in picks:   # a "good call" stays on the board; the dashboard marks it
        p["feedback"] = given.get(p["item_id"])
    picks = ranked(picks)
    # a card at a sliver of its value: the dashboard's loudest banner
    jackpots = jackpot.mark(picks, now)
    spend = spending.summary(rows, now.date(), cfg["monthly_budget"])
    for p in picks:   # "you usually pay" next to each pick, once there is a history
        usual = spending.usual_for(spend, p.get("title") or p.get("card") or "")
        p["usual_paid"] = usual["usual"] if usual else None
    state = status(con, picks, now)
    state.update(usage.summary(con, now))
    state["need_snipe"] = need_snipe(picks, now, cfg["need_snipe_hours"],
                                     bool(gix and gix["available"]))
    return {
        "api": VERSION,
        "profile": profile.get().public(),
        "generated": now.isoformat(timespec="seconds"),
        "status": state,
        "picks": _with_last_sold(con, [_slim(p) for p in picks[:MAX_PICKS]]),
        "gixen": gix,
        # Whether this bot has a Gixen account at all: without one the dashboard
        # leaves the snipe panels out instead of asking to turn Gixen on.
        "gixen_account": bool(config.get("GIXEN_USER")),
        # $20+ cards at a sliver of their value, the best first (also flagged in picks)
        "jackpots": [_slim(p) for p in jackpots],
        "cards": cards(rows),
        "collection": collection(rows, now),
        "pipeline": pipeline(rows, picks),
        "record": record(con, now),
        "market": sales.market(con, now),
        "rarest": rarity.load(con, now.isoformat(timespec="minutes")),
        "rarest_census": rarity.census(con),
        "moves": watch.load(con, now.isoformat(timespec="minutes")),
        # Buy It Nows up a month with Best Offer on, each with an amount to offer
        "offers": offers.load(con),
        # cheap auctions of good cards, each with one low snipe (ended ones left out)
        "lowball": lowball_view(con, now),
        "wantlist": wantlist(),
        # the must-have list and rainbows (optional files): owned, for sale now, live picks
        **_chase_lists(con, rows, picks, now),
        # what you have paid by month and kind of card, and what you will likely pay next
        "spending": spend,
        # what the weekly self-review changed on its own, and how each change turned out
        "tuning": {"on": cfg["self_tuning"], "log": tune.view(con),
                   "tunables": tune.current(con)},
        # how the bot corrects its own guessed going rates, per price band
        "calibration": {"on": cfg["calibrate"], "bands": calibrate.factors(con)},
        # where steals happen: how auctions close by seller and by ending hour
        "closes": closes.summary(closing),
        "settings": {key: cfg[key] for key in settings.visible()},
        "last_action": json.loads(db.kv_get(con, LAST_ACTION_KEY, "null")),
        **({"team": team_view(con, picks, cfg)} if profile.get().mode == "team" else {}),
        **steals_view(con, picks, now, cfg),
    }


def steals_view(con, picks: list[dict], now: datetime, cfg: dict) -> dict:
    """Steals mode and the "don't think, just buy" lists. Read-only; a failure here
    leaves these keys out rather than taking the whole snapshot down."""
    try:
        return {
            "steals": {"on": steals.on(), "max_share": cfg["max_share"]},
            # auctions ending within the hour at 10% of value or less, soonest first
            "bid_now": [dict(_slim(p), pay=p["pay"], off_pct=p["off_pct"])
                        for p in bidnow.find(con, now)],
            # the listings closest to a jackpot or a bid-now, soonest ending first
            "radar": steals.radar(con, now, picks),
            # 2+ picks from one seller, shipping paid once where it combines
            "bundles": steals.bundles(picks, cfg["combined_ship_sellers"]),
            "savings": steals.savings(con, now),
            # Buy It Nows cut into steal range in the last day
            "dropped": [dict(_slim(p), was=p["was"], cut_pct=p.get("cut_pct"))
                        for p in steals.dropped_into_range(con, picks, now)],
            "brag": brag.board(con, now),
        }
    except Exception as exc:
        import sys
        print(f"snapshot: steals views: {exc}", file=sys.stderr)
        return {}


def _chase_lists(con, rows: list[dict], picks: list[dict], now: datetime) -> dict:
    """"must_have" and "rainbows", read only when their files exist (most bots have neither)."""
    if not (musthave.FILE.exists() or rainbow.FILE.exists()):
        return {"must_have": musthave.view(rows, [], []), "rainbows": []}
    listing_rows = live_listings(con, now)
    return {"must_have": musthave.view(rows, listing_rows, picks),
            "rainbows": rainbow.view(rows, listing_rows, picks)}


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
