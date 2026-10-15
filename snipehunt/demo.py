"""A made-up snapshot, so you can see the dashboard before you have any keys.

Start the bot with SNIPEHUNT_DEMO=1 and it serves this instead of real data:
no eBay, no Claude, no alerts, and the buttons change nothing. Every card,
seller and price below is invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote

from . import config, profile, sales, settings, snapshot, snipecheck

# (tier, card, price, shipping, max, value, hours until it ends or None for Buy It Now,
#  basis, snipe, why, colour)
# Which demo picks slipped through the cracks, by position in PICKS.
EDGES = {0: "sleeper", 2: "hidden", 4: "mispriced"}
PICKS = [
    ("fire", "2023 Chrome Rookie Auto Gold Refractor /50", 18.50, 4.99, 45.45, 110, 1.4, "sales",
     None, "Rookie auto /50 sitting at $18.50; last three sold $98 to $125.", "#c9a227"),
    ("fire", "2022 Draft Chrome 1st Auto Blue /150 PSA 10", 31.00, 5.25, 60.60, 140, 5.2, "sales",
     60.60, "Want-list hit. PSA 10s sell around $140 shipped.", "#1e63c9"),
    ("fire", "2024 Triple Threads Patch Auto /35", 4.95, 4.99, 25.25, 70, 26.0, "estimate",
     None, "Patch auto /35 with no bids yet. Check the photo, title has no team.", "#8e24aa"),
    ("green", "2023 Chrome Sapphire Green /99 PSA 9", 4.25, 4.99, 9.09, 22, 0.6, "estimate",
     8.08, "Numbered and graded; combined shipping with your other wins.", "#2e9d57"),
    ("green", "2023 Cosmic Chrome Nucleus Refractor /199", 6.00, 1.25, 8.08, 16, None, "sales",
     None, "Buy It Now under the last four sales. Free returns.", "#d2452f"),
    ("green", "2021 Bowman's Best Refractor /250", 0.99, 4.50, 6.06, 14, 9.5, "estimate",
     None, "Starts at $0.99 with one watcher.", "#00897b"),
    ("yellow", "2023 Heritage High Number RC variation", 0.99, 1.36, 3.03, 6, 3.1, "estimate",
     None, "Short-print variation you don't own, from a cheap-shipping seller.", "#b0872c"),
    ("yellow", "2023 Holiday Mega Box Metallic RC", 1.25, 0.75, 2.02, 4.5, None, "estimate",
     None, "$2 shipped. New photo for the binder.", "#b03a48"),
    ("yellow", "2024 Archives 1961 design #3", 0.50, 1.36, 2.02, 4, 30.0, "estimate",
     None, "Cheap oddball design.", "#5d6d7e"),
]
CARDS = [
    ("2023 Chrome Rookie #209", "In binder", 3.50, "R"), ("2023 Chrome Refractor #209", "In binder", 9.09, "R"),
    ("2022 Draft Chrome 1st prospect #BD-57", "In binder", 12.12, "F"),
    ("2023 Bowman's Best Auto /99", "In binder", 45.45, "RANF"), ("2021 Bowman Platinum Top Prospects", "In binder", 1.51, ""),
    ("2023 Stadium Club Chrome RC", "Delivered", 2.78, "R"), ("2024 Pristine Northern Lights /50", "Delivered", 15.16, "N"),
    ("2023 Update Black Gold insert", "Delivered", 1.58, "R"), ("2024 Triple Threads Onyx /50", "In transit", 9.99, "N"),
    ("2023 Holiday Relic", "In transit", 1.99, "RL"), ("2022 Chrome Cracked Ice /150", "Won", 3.13, "N"),
    ("2023 Prizm Draft Picks Silver", "Awaiting payment", 1.51, ""), ("2020 Leaf Metal Auto 12/25", "In binder", 26.73, "ANF"),
    ("2023 Chrome Rookie #209", "In binder", 2.02, "RD"),
]
SELLERS = ["cardbarn_consign", "topshelfbreaks", "dugout_deals", "rookie_row"]
# (card, final bid, shipping, bids, hours ago, the bot's call as (tier, max, value) or None, colour)
SOLD = [
    ("2023 Chrome Rookie Auto Refractor /499", 38.00, 4.99, 14, 2, ("fire", 45.45, 62), "#c9a227"),
    ("2022 Chrome Sapphire Prospects PSA 10", 36.00, 4.99, 15, 5, ("green", 30.30, 44), "#1e63c9"),
    ("2023 Finest Purple Refractor /250 BGS 9.5", 18.50, 5.99, 10, 9, ("green", 20.20, 27), "#8e24aa"),
    ("2023 Cosmic Chrome Rookie #151", 1.76, 4.99, 3, 13, None, "#d2452f"),
    ("2021 Platinum Top Prospects Auto", 22.50, 4.99, 14, 20, ("fire", 25.25, 32), "#00897b"),
    ("2023 Chrome Update Sepia Refractor RC", 3.75, 1.25, 5, 26, ("yellow", 4.04, 6), "#b0872c"),
    ("2024 Chrome Green Geometric Refractor /99", 3.10, 4.99, 3, 31, None, "#2e9d57"),
    ("2020 Bowman's Best Auto PSA 9", 46.01, 4.99, 15, 40, ("fire", 35.35, 55), "#b03a48"),
    ("2023 Holiday Santa Hat variation SP", 2.25, 1.36, 4, 52, ("yellow", 3.03, 4.5), "#5d6d7e"),
    ("2022 Bowman Chrome Prospects #BCP-20", 0.99, 1.10, 1, 70, None, "#546e7a"),
]


def _art(colour: str, label: str) -> str:
    """A tiny stand-in card picture, drawn inline so the demo needs no image host."""
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='120' height='168'>"
           f"<defs><linearGradient id='g' x2='1' y2='1'><stop offset='0' stop-color='{colour}'/>"
           f"<stop offset='1' stop-color='#11151c'/></linearGradient></defs>"
           f"<rect width='120' height='168' rx='8' fill='url(#g)'/>"
           f"<rect x='10' y='10' width='100' height='112' rx='4' fill='#ffffff22'/>"
           f"<circle cx='60' cy='56' r='20' fill='#ffffff55'/><rect x='34' y='82' width='52' height='40' rx='12' fill='#ffffff55'/>"
           f"<text x='60' y='148' font-family='Arial' font-size='15' font-weight='700' fill='#fff' "
           f"text-anchor='middle'>{label}</text></svg>")
    return "data:image/svg+xml;utf8," + quote(svg)


# (card, print run, auto, price, shipping, hours until it ends or None, bids, colour)
RAREST = [
    ("2023 Chrome Update Rookie Auto Superfractor 1/1 PSA 9", 1, True, 2499.0, 0.0, None, 0, "#c9a227"),
    ("2024 Dynasty Rookie Patch Auto Printing Plate 1/1", 1, True, 310.0, 4.99, 30.0, 12, "#263238"),
    ("2022 Trinity Patch Auto Emerald 2/3", 3, True, 189.0, 5.25, None, 0, "#2e9d57"),
    ("2023 Dynasty Rookie Patch Auto Silver 4/5", 5, True, 152.5, 4.99, 9.0, 21, "#78909c"),
    ("2023 Cosmic Chrome Supernova Red /5", 5, False, 64.0, 4.99, 50.0, 6, "#d2452f"),
    ("2023 Three and Two Quad Patch Auto 1/8", 8, True, 95.0, 4.5, None, 0, "#8e24aa"),
    ("2024 Sterling Swings Relic Auto Blue 1/10", 10, True, 41.0, 4.99, 3.5, 9, "#1e63c9"),
    ("2023 Chrome Sapphire Padparadscha /10 PSA 10", 10, False, 120.0, 0.0, None, 0, "#e57373"),
    ("2023 Museum Collection Signature Swatches /15", 15, True, 58.0, 4.99, 70.0, 2, "#00897b"),
    ("2025 Archives Fan Favorites Auto Gold 09/18", 18, True, 33.0, 1.25, 26.0, 4, "#b0872c"),
]


def _rarest(now: datetime) -> list[dict]:
    out = []
    for n, (card, run, auto, price, ship, hours, bids, colour) in enumerate(RAREST):
        item_id = str(410000300007 + n * 5417)
        out.append({
            "item_id": item_id, "title": card, "run": run, "auto": auto,
            "graded": "PSA" in card, "price": price, "shipping": ship,
            "is_auction": bool(hours), "best_offer": not hours and n % 2 == 0, "bids": bids,
            "end_time": (now + timedelta(hours=hours)).isoformat(timespec="minutes")
            if hours else None,
            "seller": SELLERS[n % len(SELLERS)], "url": f"https://www.ebay.com/itm/{item_id}",
            "image": _art(colour, "1/1" if run == 1 else f"/{run}"),
            "first_seen": int((now - timedelta(hours=2 if n in (1, 6) else 90)).timestamp()),
            "owned": n == 5})
    return out


# (card, old price, new price, shipping, relist?, hours ago, colour)
MOVES = [
    ("2023 Chrome Update Rookie Auto Blue Refractor /150", 89.99, 59.99, 4.99, False, 1, "#1e63c9"),
    ("2022 Sterling Prospect Auto Speckle /99 PSA 10", 75.00, 54.00, 0.0, True, 3, "#8e24aa"),
    ("2023 Finest Rookie Design Variation Auto /25", 140.00, 110.00, 5.25, False, 7, "#2e9d57"),
    ("2021 Draft Chrome Sapphire Green /50", 48.00, 39.99, 4.99, False, 20, "#00897b"),
    ("2024 Heritage Real One Auto Red Ink /75", 60.00, 60.00, 4.99, True, 26, "#d2452f"),
]
# (card, prices oldest first, colour)
HISTORY = [
    ("2023 Chrome Rookie Refractor #45 PSA 10", [31.0, 29.5, 27.0, 28.5, 24.99, 23.5], "#1e63c9"),
    ("2022 Draft Chrome 1st Auto Blue /150 PSA 10", [162.0, 149.0, 140.99], "#8e24aa"),
    ("2023 Cosmic Chrome Nucleus Refractor /199", [13.5, 15.0, 16.75, 18.0], "#d2452f"),
    ("2023 Heritage High Number RC variation", [5.5, 6.25, 5.75], "#b0872c"),
    ("2021 Bowman's Best Refractor /250", [12.0, 14.5], "#00897b"),
]


def _moves(now: datetime) -> list[dict]:
    out = []
    for n, (card, old, new, ship, relist, hours, colour) in enumerate(MOVES):
        item_id = str(410000400009 + n * 4273)
        out.append({
            "kind": "relist" if relist else "drop", "item_id": item_id, "title": card,
            "old": old, "price": new, "cut_pct": round(100 * (1 - new / old)), "shipping": ship,
            "is_auction": False, "best_offer": n % 2 == 0, "end_time": None,
            "seller": SELLERS[n % len(SELLERS)], "url": f"https://www.ebay.com/itm/{item_id}",
            "image": _art(colour, "CUT" if not relist else "BACK"),
            "at": int((now - timedelta(hours=hours)).timestamp())})
    return out


def _history(now: datetime) -> list[dict]:
    return [{"title": card, "image": _art(colour, "SOLD"), "n": len(prices), "prices": prices,
             "last": prices[-1], "low": min(prices), "high": max(prices),
             "typical": round(sorted(prices)[len(prices) // 2], 2),
             "last_at": (now - timedelta(days=1 + n)).isoformat(timespec="minutes"),
             "url": f"https://www.ebay.com/itm/{410000500001 + n * 3301}"}
            for n, (card, prices, colour) in enumerate(HISTORY)]


def _market(now: datetime) -> dict:
    sold = []
    for n, (card, price, ship, bids, hours, call, colour) in enumerate(SOLD):
        item_id = str(410000200003 + n * 6151)
        sold.append({
            "item_id": item_id, "title": card, "price": price, "shipping": ship,
            "total": round(price + ship, 2), "estimated": False, "bids": bids,
            "kind": "auction",
            "sold_at": (now - timedelta(hours=hours)).isoformat(timespec="minutes"),
            "image": _art(colour, "SOLD"), "url": f"https://www.ebay.com/itm/{item_id}",
            "call": call and {"tier": call[0], "max": call[1], "value": float(call[2]),
                              "under_max": price <= call[1]}})
    return {"count": 212, "count_7d": 41, "calls": 31, "under_max": 22, "over_max": 9,
            "under_win": 28, "value_gap_pct": 14, "value_bias_pct": 6, "valued": 28,
            "bands": [
                {"band": "under $10", "calls": 12, "value_gap_pct": 22, "value_bias_pct": -18},
                {"band": "$10 to $50", "calls": 13, "value_gap_pct": 11, "value_bias_pct": 6},
                {"band": "over $50", "calls": 3, "value_gap_pct": 9, "value_bias_pct": 8}],
            "sold": sold, "history": _history(now), "missed_steals": 0, "missed": []}


def _checked(gixen: dict, picks: list[dict]) -> dict:
    """The demo snipes with the same verdicts the real bot gives."""
    values = {p["item_id"]: p for p in picks}
    checks = []
    for n, snipe in enumerate(gixen["snipes"]):
        pick = values.get(snipe["item_id"])
        rate = ({"value": pick["value"], "basis": pick["max_basis"], "note": pick["why"]}
                if pick else {"value": round((snipe["max_bid"] or 10) * (0.8 + 0.5 * n), 2),
                              "basis": "estimate", "note": "No recent sale found; priced "
                              "from similar cards."})
        checks.append(snipecheck.judge(snipe, rate, 4.99))
        snipe["check"] = checks[-1]
    live = [(s, c) for s, c in zip(gixen["snipes"], checks) if c["verdict"] != "outbid"]
    gixen["analysis"] = {
        **{name: sum(c["verdict"] == name for c in checks) for name in snipecheck.VERDICTS},
        "snipes": len(checks), "committed": round(sum(s["max_bid"] for s, _ in live), 2),
        "worth": round(sum(c["value"] for _, c in live), 2)}
    return gixen


def build(now: datetime | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    who = profile.get()
    picks, snipes = [], []
    for n, (tier, card, price, ship, max_bid, value, hours, basis, snipe, why, colour) in enumerate(PICKS):
        item_id = str(410000000001 + n * 7919)
        end = (now + timedelta(hours=hours)).isoformat(timespec="minutes") if hours else None
        pay = (max_bid if hours else price) + ship
        pct = round(100 * (1 - pay / value))
        stars = next((s for floor, s in ((50, 5), (35, 4), (20, 3), (10, 2)) if pct >= floor), 1)
        label = card.split("/")[-1].split()[0] if "/" in card else "RC"
        picks.append({
            "item_id": item_id, "card": card, "tier": tier, "price": price, "shipping": ship,
            "total": round(price + ship, 2), "max": max_bid, "max_basis": basis,
            "is_auction": bool(hours), "best_offer": not hours and n % 2 == 0, "end_time": end,
            "seller": SELLERS[n % len(SELLERS)], "why": why,
            "url": f"https://www.ebay.com/itm/{item_id}", "image": _art(colour, "/" + label if "/" in card else label),
            "snipe": snipe, "bid_now": price if snipe else None, "value": float(value),
            "deal": stars, "deal_pct": max(pct, 0), "edge": EDGES.get(n), "fast": n == 4,
            "feedback": None, "unverified": False, "want": False,
            "win": sales.win_bid(float(value), ship, max_bid) if hours else None,
            "last_sold": {"total": round(value * 0.96, 2), "price": round(value * 0.96 - ship, 2),
                          "n": 2 + n % 3, "at": (now - timedelta(days=3 + n)).date().isoformat()}
            if basis == "sales" else None})
        if snipe:
            snipes.append({"item_id": item_id, "end_time": end, "max_bid": snipe,
                           "current_bid": price, "outbid": False, "title": card,
                           "seller": SELLERS[n % len(SELLERS)], "bot_max": max_bid, "tier": tier,
                           "url": f"https://www.ebay.com/itm/{item_id}"})
    snipes.append({"item_id": "410000099999", "max_bid": 15.15, "current_bid": 17.20,
                   "end_time": (now + timedelta(hours=2.2)).isoformat(timespec="minutes"),
                   "outbid": True, "title": "2021 Platinum Top Prospects Auto", "bot_max": None,
                   "seller": SELLERS[0], "tier": None, "url": "https://www.ebay.com/itm/410000099999"})
    cards = [{"id": str(n + 1), "n": name, "s": status, "p": paid,
              "d": (now - timedelta(days=3 * (len(CARDS) - n))).date().isoformat(),
              "pg": who.binder_page(name[:4]) if "F" not in flags else "front page",
              "f": flags, "sl": SELLERS[n % len(SELLERS)]}
             for n, (name, status, paid, flags) in enumerate(CARDS)]
    spent = round(sum(c["p"] for c in cards), 2)
    month = now.strftime("%Y-%m")
    this_month = [c for c in cards if c["d"].startswith(month)]
    years: dict[str, int] = {}
    for c in cards:
        years[c["n"][:4]] = years.get(c["n"][:4], 0) + 1
    unpaid = [c for c in cards if c["s"] in ("Won", "Awaiting payment")]
    cfg = {**settings.DEFAULTS, "monthly_budget": 150.0, "sellers": SELLERS[:2]}
    return {
        "api": snapshot.VERSION, "demo": True, "profile": who.public(),
        "generated": now.isoformat(timespec="seconds"),
        "status": {
            "state": "ok", "failing": [], "last_ok_job": "hourly",
            "last_ok": (now - timedelta(minutes=12)).isoformat(timespec="seconds"),
            "last_search": (now - timedelta(minutes=14)).isoformat(timespec="seconds"),
            "listings": 642, "seen_total": 2318, "new_24h": 187, "scored_24h": 187,
            "picks_24h": 14, "alerts_24h": 6, "live_picks": len(picks),
            "auctions": sum(p["is_auction"] for p in picks),
            "buy_now": sum(not p["is_auction"] for p in picks),
            "fire": 3, "green": 3, "yellow": 3, "sales_based": 3, "held_overnight": 0,
            "sold_prices": 212, "sold_prices_7d": 41,
            "searching": False, "skipped": 0, "feedback": 0, "last_report": None,
            "last_self_review": None,
            "cost_today": 0.84, "calls_today": 19, "cost_month": 11.37,
            "need_snipe": [p["item_id"] for p in picks if p["is_auction"] and p["snipe"] is None
                           and p["tier"] in ("fire", "green")
                           and p["end_time"] <= (now + timedelta(hours=24)).isoformat()],
        },
        "picks": picks,
        "gixen": _checked({"available": True, "problem": None,
                  "read_at": (now - timedelta(minutes=6)).isoformat(timespec="seconds"),
                  "snipes": sorted(snipes, key=lambda s: s["end_time"]), "outbid_count": 1,
                  "committed": round(sum(s["max_bid"] for s in snipes if not s["outbid"]), 2)},
                         picks),
        "cards": cards,
        "collection": {
            "cards": len(cards), "spent": spent, "avg_paid": round(spent / len(cards), 2),
            "month": month, "month_cards": len(this_month),
            "month_spent": round(sum(c["p"] for c in this_month), 2),
            "by_status": {s: sum(c["s"] == s for c in cards) for s in {c["s"] for c in cards}},
            "by_year": dict(sorted(years.items())),
            "rookies": sum("R" in c["f"] for c in cards), "autos": sum("A" in c["f"] for c in cards),
            "relics": sum("L" in c["f"] for c in cards), "numbered": sum("N" in c["f"] for c in cards),
            "graded": 2, "front_page": sum("F" in c["f"] for c in cards),
            "dupes": sum("D" in c["f"] for c in cards),
            "priciest": [{"card": c["n"], "status": c["s"], "paid": c["p"], "date": c["d"],
                          "seller": c["sl"], "page": c["pg"]}
                         for c in sorted(cards, key=lambda c: -c["p"])[:5]]},
        "pipeline": {
            "unpaid": [{"seller": unpaid[0]["sl"], "advice": "pay now", "total": unpaid[0]["p"],
                        "cards": [unpaid[0]["n"]]},
                       {"seller": unpaid[1]["sl"], "total": unpaid[1]["p"], "cards": [unpaid[1]["n"]],
                        "advice": "hold: another snipe from this seller is still open"}],
            "unpaid_total": round(sum(c["p"] for c in unpaid), 2), "unpaid_cards": len(unpaid),
            "in_transit": sum(c["s"] == "In transit" for c in cards),
            "to_file": sum(c["s"] == "Delivered" for c in cards)},
        "record": {"rate": 58, "days": 30, "wins": 18, "losses": 13, "gixen_wins": 11,
                   "gixen_losses": 13, "wins_week": 5, "losses_week": 3,
                   "recent_losses": [{"title": "2023 Chrome Superfractor 1/1", "price": None,
                                      "date": (now - timedelta(days=1)).date().isoformat(),
                                      "seller": SELLERS[1]}]},
        "market": _market(now),
        "tuning": {"on": True, "log": [], "tunables": {}},
        "rarest": _rarest(now),
        "rarest_census": {"one_of_one": 4, "to_10": 11, "to_50": 37},
        "moves": _moves(now),
        "wantlist": snapshot.wantlist(),
        "settings": {key: cfg[key] for key in settings.visible()},
        "last_action": None,
    }
