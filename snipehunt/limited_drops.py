"""Topps Now / Bowman Now and other print-to-order cards of the player.

topps.com sits behind bot protection that often returns 403 to servers, so the
direct check is a best-effort first try and Claude's web search does the real
work. Nothing here buys anything: it only reports what is on sale.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime

import requests

from . import config, db, profile
from .score import ask_claude, extract_json

TOPPS_SEARCH = "https://www.topps.com/search/suggest.json"
UA = "snipe-hunt/1.0 (personal collection tracker)"

DAILY_PROMPT = """Today is {today} ({tz}). Use web search to find out whether \
{player}, has a print-to-order or limited-time card ON SALE RIGHT NOW from the \
manufacturer: Topps Now, Bowman Now, Panini Instant, Topps Now Card of the \
Month, Throwback Thursday, Topps Living Set, or similar. Check the \
manufacturer's own listings, and recent posts from Topps, Panini, Cardboard \
Connection, Beckett and Blowout forums. {name} must be pictured or named on the \
card (multi-player cards count).

Only report cards whose sale window is open today or opens within 24 hours. \
Ignore cards whose window already closed, and ignore secondary-market (eBay) \
copies. If you cannot confirm any, return an empty list; do not guess.

Reply with ONLY one JSON object, no prose:
{{"drops": [{{"name": "…", "product": "Topps Now|Bowman Now|…", "card_no": "…", \
"price": 11.99, "deadline": "ISO datetime in {tz}, or null if unknown", \
"print_run": "number if announced, else null", "url": "manufacturer page", \
"source": "where you confirmed it"}}], "checked": ["sites you checked"]}}"""

WEEKLY_PROMPT = """Today is {today}. {collector} collects {player}. Use web \
search to check the checklists of {sport} card products released in the last \
45 days or releasing in the next 30{products} for {name} cards.

Compare with the inventory below and list {name} cards NOT in it. Keep it to \
cards you actually found on a checklist; name the source. Note that early \
eBay copies of brand-new releases are usually overpriced.

Reply with ONLY one JSON object, no prose:
{{"new_cards": [{{"product": "…", "release_date": "YYYY-MM-DD or null", \
"card": "name, number, notable parallels/autos", "note": "short", \
"source": "…"}}], "checked": ["products checked"]}}

=== inventory.csv ===
{inventory}"""


def topps_direct() -> list[dict] | None:
    """Try topps.com product search. None means blocked or unavailable."""
    try:
        resp = requests.get(
            TOPPS_SEARCH, timeout=20, headers={"User-Agent": UA},
            params={"q": profile.get().search_name, "resources[type]": "product",
                    "resources[limit]": 10})
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        products = resp.json()["resources"]["results"]["products"]
    except (ValueError, KeyError):
        return None
    hits = []
    for p in products:
        title = p.get("title", "")
        if profile.get().last.lower() in title.lower() and p.get("available", True):
            hits.append({"name": title, "product": "topps.com", "card_no": None,
                         "price": float(p["price"]) if p.get("price") else None,
                         "deadline": None, "print_run": None,
                         "url": "https://www.topps.com" + p.get("url", ""),
                         "source": "topps.com search"})
    return hits


def check_drops(verbose: bool = False) -> list[dict]:
    today = datetime.now(config.TZ).strftime("%A %Y-%m-%d %H:%M")
    direct = topps_direct()
    if verbose:
        print("drops: topps.com direct "
              + ("blocked (403), using web search" if direct is None
                 else f"ok, {len(direct)} products"), file=sys.stderr)
    who = profile.get()
    reply = extract_json(ask_claude(DAILY_PROMPT.format(
        today=today, tz=config.TZ.key, player=who.described, name=who.player), timeout=900))
    drops = list(reply.get("drops", []) or [])
    names = {d.get("name", "").lower() for d in drops}
    drops += [d for d in (direct or []) if d["name"].lower() not in names]
    if verbose:
        print(f"drops: {len(drops)} live; checked {reply.get('checked')}", file=sys.stderr)
    return drops


def drop_key(drop: dict) -> str:
    basis = f"{drop.get('product')}|{drop.get('card_no')}|{drop.get('name')}".lower()
    return "drop:" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def as_pick(drop: dict) -> dict:
    """Shape a drop like a scoring pick so notify can bundle it."""
    deadline = drop.get("deadline")
    why = "Print-to-order; gone after the window closes"
    if deadline:
        try:
            end = datetime.fromisoformat(deadline)
            end = end.replace(tzinfo=config.TZ) if end.tzinfo is None else end.astimezone(config.TZ)
            why = f"On sale until {end.strftime('%a %-I:%M %p %Z')}"
        except ValueError:
            why = f"On sale until {deadline}"
    if drop.get("print_run"):
        why += f" · print run {drop['print_run']}"
    price = float(drop.get("price") or 0)
    return {"item_id": drop_key(drop), "tier": "drop", "price": price,
            "card": f"{drop.get('product', 'Drop')}: {drop.get('name', '')}"[:90],
            "shipping": None, "total": price, "max": None, "is_auction": False,
            "end_time": None, "why": why, "url": drop.get("url"), "image": None}


def format_drop(drop: dict) -> str:
    p = as_pick(drop)
    price = f"${p['price']:.2f}" if p["price"] else "price ?"
    return f"⏰ {p['card']}\n{price} at original price\n{p['why']}\n{drop.get('url', '')}"


def new_drops(con, drops: list[dict]) -> list[dict]:
    fresh = []
    for d in drops:
        key = drop_key(d)
        db.touch(con, key, d.get("name", ""))
        if db.may_alert(con, key, float(d.get("price") or 0) or 1.0):
            fresh.append(d)
    return fresh


def check_new_releases(verbose: bool = False) -> list[dict]:
    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    who = profile.get()
    reply = extract_json(ask_claude(
        WEEKLY_PROMPT.format(
            today=today, collector=who.collector, player=who.described, name=who.player,
            sport=who.sport, products=f" ({who.products})" if who.products else "",
            inventory=config.INVENTORY.read_text()),
        timeout=1200))
    if verbose:
        print(f"releases: checked {reply.get('checked')}", file=sys.stderr)
    return list(reply.get("new_cards", []) or [])


def main() -> None:
    ap = argparse.ArgumentParser(description="Check limited-time drops")
    ap.add_argument("--releases", action="store_true", help="weekly checklist check")
    args = ap.parse_args()
    result = check_new_releases(True) if args.releases else check_drops(True)
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
