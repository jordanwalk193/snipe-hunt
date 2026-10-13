"""A made-up snapshot, so you can see the dashboard before you have any keys.

Start the bot with SNIPEHUNT_DEMO=1 and it serves this instead of real data:
no eBay, no Claude, no alerts, and the buttons change nothing. Every card,
seller and price below is invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote

from . import config, profile, settings, snapshot

# (tier, card, price, shipping, max, value, hours until it ends or None for Buy It Now,
#  basis, snipe, why, colour)
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
            "deal": stars, "deal_pct": max(pct, 0)})
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
            "searching": False, "skipped": 0, "last_report": None,
            "cost_today": 0.84, "calls_today": 19, "cost_month": 11.37,
            "need_snipe": [p["item_id"] for p in picks if p["is_auction"] and p["snipe"] is None
                           and p["tier"] in ("fire", "green")
                           and p["end_time"] <= (now + timedelta(hours=24)).isoformat()],
        },
        "picks": picks,
        "gixen": {"available": True, "problem": None,
                  "read_at": (now - timedelta(minutes=6)).isoformat(timespec="seconds"),
                  "snipes": sorted(snipes, key=lambda s: s["end_time"]), "outbid_count": 1,
                  "committed": round(sum(s["max_bid"] for s in snipes if not s["outbid"]), 2)},
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
        "wantlist": snapshot.wantlist(),
        "settings": {key: cfg[key] for key in settings.visible()},
        "last_action": None,
    }
