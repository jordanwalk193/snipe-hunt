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

A "mega" jackpot (a MEGA_VALUE card for MEGA_SHARE of its value or less) is the
once-a-month kind: a critical push that sounds even on silent. Every jackpot
alert also fires a Home Assistant event (EVENT) for automations to act on.

Ledger jackpots (scan, verify, ledger_run) come from any live listing the price
ledger alone prices, whether Claude picked it or not: a card the scorer passed
on, or one already in the collection (worth buying to flip or trade). No Claude
call; each candidate is read again on eBay before anything is sent.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta

from . import config, db, sales, settings

MIN_VALUE = 20.00
SHARE_SALES = 0.20      # 80% or more off a value backed by sales
SHARE_GUESS = 0.12      # 88% or more off a value that is only an estimate
AUCTION_HOURS = 2
SENT_KEY = "jackpots_sent"
KEEP_SENT = 300
MEGA_VALUE = 40.00      # the once-a-month kind: a card worth this much or more...
MEGA_SHARE = 0.10       # ...for a tenth of its value or less, shipping counted
LEDGER_KEY = "ledger_jackpots"   # the last ledger pass's live jackpots, for the dashboard
LEDGER_MIN_SALES = 2    # recorded sales of exactly this card, none of them stale
LEDGER_SEEN_DAYS = 3    # listings seen this recently and not closed
LEDGER_VERIFY = 8       # eBay reads a pass, at most
NEW_SELLER_UNDER = 5    # feedback under this on the live listing: a scam looks the same
EVENT = "snipe_hunt_jackpot"     # a Home Assistant event, for automations (flash the lights)


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


def level(pick: dict) -> str:
    """"mega" for the once-a-month kind, else "jackpot"."""
    value = float(pick.get("value") or 0)
    return "mega" if value >= MEGA_VALUE and pay(pick) <= value * MEGA_SHARE else "jackpot"


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
            p["jackpot_level"] = level(p)
            found.append(p)
        else:
            p.pop("jackpot", None)
            p.pop("jackpot_level", None)
    return sorted(found, key=lambda p: pay(p) / p["value"])


def message(pick: dict) -> tuple[str, str]:
    value, cost = float(pick["value"]), pay(pick)
    off = round(100 * (1 - cost / value))
    title = (f"🚨🚨 HOLY SH*T: ${value:.0f} card for ${cost:.2f}" if level(pick) == "mega"
             else f"🚨 JACKPOT: ${value:.0f} card for ${cost:.2f}")
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
    if pick.get("ledger_only"):
        lines.append("Found by the price ledger, not scored by Claude: check the photos and the seller.")
    if pick.get("owned"):
        lines.append("You already have this card: grab it to flip or trade.")
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
            notify.send(title, body, url=p.get("url"), image=p.get("image"), urgent=True,
                        critical=level(p) == "mega", actions=notify.pick_actions(p))
            notify.fire_event(EVENT, {"item_id": p["item_id"], "level": level(p), "title": title,
                                      "card": p.get("card") or p.get("title"),
                                      "value": p.get("value"), "pay": pay(p), "url": p.get("url")})
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


# ------------------------------------------------------------------ ledger jackpots
def _ledger_value(group: dict, now: float) -> tuple[float, int] | None:
    """The going rate of one ledger card (score.thin_comps), when it has enough fresh sales."""
    from . import score   # score imports a lot; kept local
    comps = [{"total": sales.delivered(s["price"], s["shipping"]), "sold_at": s["sold_at"]}
             for s in group["sales"]]
    thin = score.thin_comps(comps, now)
    if not thin or thin["n"] < LEDGER_MIN_SALES or thin["stale"]:
        return None
    return thin["value"], thin["n"]


def _ours(title: str) -> bool:
    """A sure match for the player (or the team): a jackpot has no room for "unclear"."""
    from . import ebay_search, profile
    who = profile.get()
    verdict = who.match(title)[0] if who.mode == "team" else ebay_search.classify(title)
    return verdict == "yes"


def scan(con, skip: set[str], now: datetime | None = None, gs: list[dict] | None = None,
         rows: list[dict] | None = None) -> list[dict]:
    """Live listings the ledger alone calls a jackpot: no Claude, no eBay call.
    `skip` are item ids already among the picks (those are judged as picks);
    `rows` the inventory, for marking a card already owned."""
    from . import inventory, ledger, snipecheck
    now = now or datetime.now(config.TZ)
    ts = now.timestamp()
    gs = gs if gs is not None else ledger.groups(con)
    if rows is None:
        try:
            rows = inventory.read()[1]
        except OSError:
            rows = []
    out = []
    for r in con.execute(
            "SELECT item_id, title, price, shipping, is_auction, end_ts, seller, bids FROM seen "
            "WHERE closed_at IS NULL AND price IS NOT NULL AND last_seen >= ? "
            "AND (end_ts IS NULL OR end_ts > ?)", (ts - LEDGER_SEEN_DAYS * 86400, ts)):
        title = r["title"] or ""
        if r["item_id"] in skip or ledger.LOT.search(title) or not _ours(title):
            continue
        if r["is_auction"] and not (r["end_ts"] and r["end_ts"] <= ts + AUCTION_HOURS * 3600):
            continue
        cost = sales.delivered(r["price"], r["shipping"])
        if cost <= 0:
            continue
        g = ledger.find(gs, title)
        got = _ledger_value(g, ts) if g else None
        if not got or got[0] < MIN_VALUE or cost > got[0] * SHARE_SALES:
            continue
        value, n = got
        end = (datetime.fromtimestamp(r["end_ts"], config.TZ).isoformat(timespec="seconds")
               if r["end_ts"] else None)
        out.append({
            "item_id": r["item_id"], "title": title, "card": title, "price": r["price"],
            "shipping": r["shipping"], "total": cost, "is_auction": bool(r["is_auction"]),
            "end_time": end, "seller": r["seller"], "bids": r["bids"],
            "bid_now": r["price"] if r["is_auction"] else None,
            "url": f"https://www.ebay.com/itm/{r['item_id']}", "image": g.get("image"),
            "value": round(value, 2), "max_basis": "sales", "tier": "ledger",
            "ledger_only": True, "comps": n,
            "owned": snipecheck.owned_copy(title, rows) is not None,
            "why": f"{n} recorded sales of this card put it at about ${value:.2f}.",
        })
    return sorted(out, key=lambda p: pay(p) / p["value"])


def verify(found: list[dict], client, limit: int = LEDGER_VERIFY) -> list[dict]:
    """Read each candidate on eBay (read-only, one item call each): drop the ones gone,
    sold or repriced, and take the live price, photo and seller. Cheapest share
    first, `limit` reads at most."""
    from . import ebay_search
    keep = []
    for p in found[:limit]:
        try:
            raw = client.item(p["item_id"], fresh=True)
        except Exception as exc:   # eBay trouble: try again next run
            print(f"jackpot: verify {p['item_id']}: {exc}", file=sys.stderr)
            continue
        if not raw:
            continue
        end = raw.get("itemEndDate")
        if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.now(config.TZ):
            continue
        if any(a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
               for a in raw.get("estimatedAvailabilities") or []):
            continue
        item = ebay_search.parse_item(raw)
        if not item:
            continue
        p.update(price=item.price, shipping=item.shipping, total=item.total, seller=item.seller,
                 image=item.image or p.get("image"), url=item.url or p["url"],
                 bin_price=item.bin_price, bids=item.bid_count,
                 bid_now=item.price if p["is_auction"] else None,
                 seller_feedback=item.seller_feedback_score, verified_at=int(time.time()))
        if item.seller_feedback_score is not None and item.seller_feedback_score < NEW_SELLER_UNDER:
            p["new_seller"] = True      # check() keeps a brand-new seller off: scams look like this
        keep.append(p)
    return keep


def ledger_run(con, client, skip: set[str], now: datetime | None = None) -> list[dict]:
    """The ledger pass: scan, read each candidate on eBay, keep the live ones for the
    dashboard (kv LEDGER_KEY). Returns the ones still jackpots."""
    now = now or datetime.now(config.TZ)
    live = mark(verify(scan(con, skip, now), client), now)
    db.kv_set(con, LEDGER_KEY, json.dumps(live))
    con.commit()
    return live


def stored(con, now: datetime | None = None) -> list[dict]:
    """The last ledger pass's jackpots that haven't ended, for the snapshot."""
    now = now or datetime.now(config.TZ)
    try:
        found = json.loads(db.kv_get(con, LEDGER_KEY, "[]") or "[]")
    except ValueError:
        return []
    return mark([p for p in found if not (
        p.get("end_time") and datetime.fromisoformat(p["end_time"]) <= now)], now)
