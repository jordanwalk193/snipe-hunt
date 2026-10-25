"""Buy It Now listings worth an offer: up a month or more and taking offers.

A listing that has sat for 30+ days with Best Offer on usually takes well under
its asking price. This only suggests an amount for you to send yourself; the
bot never makes an offer. The going rate comes from the bot's own pick for the
listing, its record of what the same card sold for, or a kept appraisal
(snipecheck.py), so most runs ask Claude nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import db, musthave, pricing, sales, snipecheck
from .ebay_search import Listing

KEY = "offers"
AFTER_DAYS = 30      # how long a listing must have been up
MIN_ASK = 3.00       # not worth haggling below this
MAX_ASK = 300.00     # past this an offer is a real purchase decision, not a lowball
ASK_SHARE = 0.70     # what a stale listing usually takes, as a share of the asking price
RATE_SHARE = 0.80    # and never more than this share of what the card goes for
MIN_SHARE = 0.50     # sellers decline offers under about half the asking price
KEEP = 40


def days_up(listed_at: str | None, now: datetime) -> int | None:
    if not listed_at:
        return None
    try:
        start = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - start).days


def suggest(ask: float, shipping: float | None, rate: float | None) -> float | None:
    """What to offer on a listing asking `ask` (plus shipping) for a card that goes for
    `rate` delivered. None when a fair offer would be under half the asking price."""
    cap = ask * ASK_SHARE
    if rate:
        cap = min(cap, rate * RATE_SHARE - (shipping if shipping is not None else
                                            sales.UNKNOWN_SHIPPING))
    offer = pricing.odd_cents(cap) if cap >= 1 else None
    if offer is None or offer < max(1, ask * MIN_SHARE):
        return None   # asking far over what it goes for: an offer that low gets declined
    return offer


def rejected(con) -> set[str]:
    """Listings you hid, or marked a bad pick (a wrong value would make a wrong offer)."""
    return set(json.loads(db.kv_get(con, "skipped_picks", "[]") or "[]")) | {
        f["item_id"] for f in db.feedback(con) if f["source"] == "you"
        and f["reason"] != "good_pick"}


def find(con, listings: list[Listing], inventory: list[dict],
         now: datetime | None = None) -> list[dict]:
    """The stale Best Offer listings on cards not already owned, the best priced
    first, must-haves (data/musthave.md) ahead of the rest at each level."""
    now = now or datetime.now(timezone.utc)
    rows, cache, skip, out = sales.with_calls(con), snipecheck._cache(con), rejected(con), []
    wanted = musthave.read()
    for item in listings:
        days = days_up(item.listed_at, now)
        if (item.is_auction or "BEST_OFFER" not in item.buying_options
                or item.match != "yes" or item.currency != "USD"
                or not MIN_ASK <= item.price <= MAX_ASK
                or days is None or days < AFTER_DAYS or item.item_id in skip
                or snipecheck.owned_copy(item.title, inventory)):
            continue
        rate = snipecheck.going_rate(con, {"item_id": item.item_id, "title": item.title},
                                     rows, cache)
        value = float(rate["value"]) if rate and rate.get("value") else None
        offer = suggest(item.price, item.shipping, value)
        if offer is None:
            continue
        must = next((c["card"] for c in wanted if c["year"] in item.title
                     and musthave.matches(c["search"], item.title)), None)
        out.append({
            "item_id": item.item_id, "title": item.title, "url": item.url, "image": item.image,
            "price": item.price, "shipping": item.shipping, "days": days, "offer": offer,
            "value": value, "basis": rate.get("basis") if value else None,
            "must_have": must, "seller": item.seller, "saved": round(item.price - offer, 2)})
    # priced ones first, by how far under the going rate the offer is; then the
    # ones nothing has priced yet, cheapest first; must-haves ahead in each group
    out.sort(key=lambda o: (o["value"] is None, o["must_have"] is None,
                            o["offer"] / o["value"] if o["value"] else o["price"]))
    return out


def _appraise(con, listings: list[Listing], inventory: list[dict], limit: int = 20) -> int:
    """Have the snipe checker price the offer targets nothing has priced yet (it keeps
    each answer 30 days, so a listing is priced at most once a month)."""
    todo = [o for o in find(con, listings, inventory) if o["value"] is None][:limit]
    return snipecheck.appraise(
        con, [{"item_id": o["item_id"], "title": o["title"], "current_bid": o["price"],
               "end_time": None} for o in todo], doing="is thinking of making offers on")


def save(con, listings: list[Listing], inventory: list[dict], appraise: bool = True) -> int:
    if appraise:
        _appraise(con, listings, inventory)
    found = find(con, listings, inventory)
    db.kv_set(con, KEY, json.dumps(found[:KEEP], ensure_ascii=False))
    return len(found)


def load(con, limit: int = KEEP) -> list[dict]:
    return json.loads(db.kv_get(con, KEY, "[]") or "[]")[:limit]
