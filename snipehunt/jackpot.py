"""Jackpots: a card selling for a sliver of what it is worth.

A $20 card for a dollar happens a few times a year: a typo in the price, a rushed
clear-out, a title nobody searches. It is gone in minutes. A jackpot skips the
alert settings, quiet hours and the bedtime bundle: it gets one loud alert of its
own (time-sensitive on iPhone, urgent on ntfy), at once, and the top of the
dashboard. Only recommends: you buy or snipe it yourself.

The bar is high on purpose, because a real jackpot and a scam look the same:
  - the card is worth MIN_VALUE or more,
  - you would pay at most SHARE_SALES of that, shipping counted, when the value
    comes from recorded sales, or SHARE_GUESS when it is only an estimate,
  - Buy It Now and Best Offer listings at their price; an auction only in its
    last AUCTION_HOURS, at its current bid,
  - never a fake autograph, an unverified price, or a seller the scam guard
    doubts.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from . import config, db, sales, settings

MIN_VALUE = 20.00
SHARE_SALES = 0.20      # 80% or more off a value backed by sales
SHARE_GUESS = 0.12      # 88% or more off a value that is only an estimate
AUCTION_HOURS = 2
SENT_KEY = "jackpots_sent"
KEEP_SENT = 300


def pay(pick: dict) -> float:
    """What it costs delivered right now: the price (an auction's current bid) plus
    shipping, or the Buy It Now when the pick is a jackpot through that."""
    ship = 0.0 if pick.get("combined_ship") else (
        sales.UNKNOWN_SHIPPING if pick.get("shipping") is None else float(pick["shipping"]))
    if pick.get("jackpot_bin") and pick.get("bin_price"):
        return round(float(pick["bin_price"]) + ship, 2)
    return round(float(pick.get("bid_now") or pick.get("price") or 0) + ship, 2)


def bin_pay(pick: dict) -> float | None:
    """An auction's Buy It Now delivered, while it still has one."""
    if not pick.get("is_auction") or not pick.get("bin_price"):
        return None
    ship = 0.0 if pick.get("combined_ship") else (
        sales.UNKNOWN_SHIPPING if pick.get("shipping") is None else float(pick["shipping"]))
    return round(float(pick["bin_price"]) + ship, 2)


def check(pick: dict, now: datetime | None = None) -> str | None:
    """Why this pick is a jackpot ("sales" or "guess"), or None when it isn't."""
    now = now or datetime.now(config.TZ)
    value = pick.get("value")
    if (not value or value < MIN_VALUE or pick.get("tier") in (None, "drop")
            or pick.get("unverified") or pick.get("scam_risk") or pick.get("new_seller")
            or pick.get("fake_auto") or "fake_auto" in (pick.get("flags") or [])
            or (pick.get("slab") or {}).get("problem")):
        return None
    basis = "sales" if pick.get("max_basis") == "sales" else "guess"
    share = SHARE_SALES if basis == "sales" else SHARE_GUESS
    if pick.get("is_auction"):
        # An auction's Buy It Now is a plain Buy It Now until someone bids: no
        # need to wait for the last hours, and every minute counts.
        via_bin = bin_pay(pick)
        if via_bin is not None and via_bin <= value * share and not pick.get("bids"):
            pick["jackpot_bin"] = True
            return basis
        pick.pop("jackpot_bin", None)
        end = pick.get("end_time")
        if not end or not now < datetime.fromisoformat(end) <= now + timedelta(hours=AUCTION_HOURS):
            return None
        if pick.get("max") is not None and float(pick.get("price") or 0) >= pick["max"]:
            return None   # already bid past what the bot would pay
    return basis if pay(pick) <= value * share else None


def mark(picks: list[dict], now: datetime | None = None) -> list[dict]:
    """Give each jackpot pick its `jackpot` field ("sales" or "guess"); returns the jackpots."""
    found = []
    for p in picks:
        why = check(p, now)
        if why:
            p["jackpot"] = why
            found.append(p)
        else:
            p.pop("jackpot", None)
    return sorted(found, key=lambda p: pay(p) / p["value"])


def message(pick: dict) -> tuple[str, str]:
    value, cost = float(pick["value"]), pay(pick)
    off = round(100 * (1 - cost / value))
    title = f"🚨 JACKPOT: ${value:.0f} card for ${cost:.2f}"
    if pick.get("jackpot_bin"):
        act = (f"It's an auction with a Buy It Now of ${float(pick['bin_price']):.2f}: "
               "buy it now, before the first bid removes that option.")
    elif pick.get("is_auction"):
        ends = datetime.fromisoformat(pick["end_time"]).strftime("%I:%M %p").lstrip("0")
        act = (f"Auction ends {ends}, bid now ${float(pick.get('bid_now') or pick['price']):.2f}."
               + (f" Snipe {pick['max']:.2f} now." if pick.get("max") else " Snipe it now."))
    else:
        act = "Buy it now, think later."
    basis = (f"Worth about ${value:.2f} going by recorded sales."
             if pick.get("jackpot", check(pick)) == "sales"
             else f"Worth about ${value:.2f} by the bot's estimate: check the comps as you buy.")
    lines = [pick.get("card") or pick.get("title") or "", f"{off}% under its value. {act}", basis]
    if pick.get("why"):
        lines.append(str(pick["why"])[:200])
    lines.append(f"Item {pick['item_id']}")
    return title, "\n".join(lines)


def alert(con, picks: list[dict], now: datetime | None = None, send: bool = True) -> list[dict]:
    """One alert per jackpot not sent before. Returns the ones sent (or that would be)."""
    from . import notify   # notify imports score; kept local
    now = now or datetime.now(config.TZ)
    if not settings.load(con)["jackpot_alerts"]:
        return []
    sent_ids = json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]")
    fresh = [p for p in mark(picks, now) if p["item_id"] not in sent_ids]
    if not fresh or not send:
        return fresh
    if not notify.configured():
        return []
    done = []
    for p in fresh:
        title, body = message(p)
        try:
            notify.send(title, body, url=p.get("url"), image=p.get("image"), urgent=True)
        except Exception as exc:   # the next run tries again
            print(f"jackpot: {exc}", file=sys.stderr)
            break
        db.mark_alerted(con, p["item_id"], p.get("total") or pay(p))
        sent_ids.append(p["item_id"])
        done.append(p)
    db.kv_set(con, SENT_KEY, json.dumps(sent_ids[-KEEP_SENT:]))
    db.bump(con, "jackpots_sent", len(done))
    con.commit()
    if done:
        print(f"jackpot: {len(done)} sent", file=sys.stderr)
    return done
