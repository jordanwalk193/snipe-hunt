"""Bid now, think later: auctions ending within the hour at 10% of their value or less.

Steals mode (steals.py) turns this on. Every ten minutes the fast lane looks at
the auction picks and the lowball targets ending within WINDOW_MINUTES. One
whose current bid plus shipping is SHARE of its going rate or less is on the
list (the dashboard's `bid_now`, soonest ending first so nothing slips through),
and gets one time-sensitive push after a fresh read of its bid. An auction's
price only goes up, so a listing already past SHARE at its last known bid can't
qualify later and costs no eBay call.

The same doubts as a jackpot keep a listing off: a fake autograph, an
unverified price, a seller the scam guard doubts, a slab with a problem.
Only recommends: you place the bid or the Gixen snipe yourself.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from . import config, db, jackpot, lowball, settings
from .ebay_search import _money

WINDOW_MINUTES = 60
SHARE = 0.10            # 90% or more under the going rate, shipping counted
MIN_LEFT_MINUTES = 2    # too late to place a bid by hand
SENT_KEY = "bidnow_sent"
KEEP_SENT = 300
MAX_READS = 25          # fresh bid reads per run; the window rarely holds this many


def _from_target(t: dict) -> dict:
    """A lowball target in the shape of a pick."""
    return {"item_id": t["item_id"], "card": t["title"], "title": t["title"], "url": t["url"],
            "image": t.get("image"), "price": t["price"], "shipping": t.get("shipping"),
            "bids": t.get("bids"), "end_time": t["end_time"], "value": t["value"],
            "max_basis": "sales" if t.get("basis") in ("sales", "sold") else "estimate",
            "max": t.get("snipe"), "is_auction": True, "tier": "lowball",
            "seller": t.get("seller"), "sniped": t.get("sniped", False)}


def _trusted(p: dict) -> bool:
    return not (p.get("tier") == "drop" or p.get("unverified") or p.get("scam_risk")
                or p.get("new_seller") or p.get("fake_auto")
                or "fake_auto" in (p.get("flags") or [])
                or (p.get("slab") or {}).get("problem"))


def qualifies(p: dict, now: datetime) -> bool:
    end = p.get("end_time")
    if not (p.get("is_auction") and end and p.get("value") and _trusted(p)):
        return False
    ends = datetime.fromisoformat(end)
    return (now + timedelta(minutes=MIN_LEFT_MINUTES) < ends
            <= now + timedelta(minutes=WINDOW_MINUTES)
            and jackpot.pay(p) <= SHARE * float(p["value"]))


def candidates(con, now: datetime) -> list[dict]:
    """Auction picks and lowball targets at their latest known bid, one per item."""
    from . import snapshot   # snapshot imports most modules; kept local
    out = {p["item_id"]: p for p in snapshot.live_picks(con, now) if p.get("is_auction")}
    for t in lowball.load(con).get("targets", []):
        if t["item_id"] not in out:
            p = _from_target(t)
            row = con.execute("SELECT price, bids FROM seen WHERE item_id=?",
                              (p["item_id"],)).fetchone()
            if row and row["price"] is not None and row["price"] > p["price"]:
                p["price"], p["bids"] = row["price"], row["bids"]   # a later read
            out[p["item_id"]] = p
    return list(out.values())


def find(con, now: datetime | None = None) -> list[dict]:
    """The list, soonest ending first, each with what it costs and how far under value."""
    now = now or datetime.now(config.TZ)
    found = []
    for p in candidates(con, now):
        if qualifies(p, now):
            cost = jackpot.pay(p)
            found.append(dict(p, pay=cost, off_pct=round(100 * (1 - cost / float(p["value"])))))
    return sorted(found, key=lambda p: p["end_time"])


def message(p: dict, now: datetime) -> tuple[str, str]:
    ends = datetime.fromisoformat(p["end_time"]).astimezone(config.TZ)
    left = max(1, round((ends - now).total_seconds() / 60))
    title = f"⏱️ BID NOW: ${float(p['value']):.0f} card for ${p['pay']:.2f}, ends in {left} min"
    bid = float(p.get("bid_now") or p["price"])
    lines = [str(p.get("card") or p["title"])[:90],
             f"{p['off_pct']}% under its value. Bid now ${bid:.2f}"
             + (f" · {p['bids']} bid{'s' if p['bids'] != 1 else ''}" if p.get("bids") is not None
                else "") + f" · ends {ends:%-I:%M %p}.",
             (f"Worth about ${float(p['value']):.2f} going by recorded sales."
              if p.get("max_basis") == "sales"
              else f"Worth about ${float(p['value']):.2f} by the bot's estimate."),
             # cheap auctions get bid up in the last minutes: bid the max, not today's price
             (f"Bid up to {float(p['max']):.2f}. " if p.get("max") else "")
             + "Bid now, think later." + (" Already in Gixen." if p.get("sniped") else ""),
             f"Item {p['item_id']}"]
    return title, "\n".join(lines)


def alert(con, client, now: datetime | None = None, send: bool = True) -> list[dict]:
    """Read each new listing's bid again, and push the ones that still qualify. Returns them."""
    from . import notify   # notify imports score; kept local
    now = now or datetime.now(config.TZ)
    if not settings.load(con)["bid_now_alerts"]:
        return []
    done_ids = set(json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]"))
    done_ids |= set(json.loads(db.kv_get(con, jackpot.SENT_KEY, "[]") or "[]"))
    hits = []
    for p in [p for p in find(con, now) if p["item_id"] not in done_ids][:MAX_READS]:
        raw = client.item(p["item_id"])
        if not raw:
            continue
        price, _ = _money(raw.get("currentBidPrice") or raw.get("price"))
        if price is None:
            continue
        bids = raw.get("bidCount")
        db.note_bid(con, p["item_id"], price, bids, int(now.timestamp()))
        p = dict(p, price=price, bid_now=price, bids=bids)
        if qualifies(p, now):
            cost = jackpot.pay(p)
            hits.append(dict(p, pay=cost, off_pct=round(100 * (1 - cost / float(p["value"])))))
    con.commit()
    if not hits or not send or not notify.configured():
        return hits
    sent = json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]")
    done = []
    for p in hits:
        title, body = message(p, now)
        try:
            notify.send(title, body, url=p.get("url"), image=p.get("image"), urgent=True)
        except Exception as exc:   # the next run, ten minutes on, tries again
            print(f"bidnow: {exc}", file=sys.stderr)
            break
        sent.append(p["item_id"])
        done.append(p)
    db.kv_set(con, SENT_KEY, json.dumps(sent[-KEEP_SENT:]))
    db.bump(con, "bid_now_sent", len(done))
    con.commit()
    if done:
        print(f"bidnow: {len(done)} sent", file=sys.stderr)
    return done
