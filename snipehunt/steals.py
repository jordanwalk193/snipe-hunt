"""Steals mode: only cheap cards far under their value, and the "don't think, just
buy" side of the dashboard.

A profile with a [steals] table turns it on (see examples/profile.example.toml):

- max_share: an auction's max is cut so winning costs at most this share of the
  going rate with shipping, and a Buy It Now over it is no pick (fit).
- the cheap filter: listings over a delivered ceiling, and the high-end noise
  (1/1s, superfractors, printing plates), are dropped before scoring (keep).
- bid-now and price-drop pushes (bidnow.py, drop_alert).

Read-only views that every bot gets, steals mode or not:

- radar: the listings closest to a jackpot or a bid-now, soonest ending first,
  so nothing slips through the cracks.
- bundles: picks from one seller added up, shipping paid once where the seller
  combines it, with the Gixen lines to copy for a group snipe.
- savings: what the bought picks were worth against what was paid.

Nothing here asks Claude; only drop_alert sends anything. Recommends only.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timedelta

from . import bidnow, config, db, jackpot, pricing, profile, sales, settings

# [steals] in the profile, with these where it says nothing
DEFAULTS = {
    "max_share": 0.50,          # "50% of value at most", shipping counted
    "bid_now_alerts": True,
    "drop_alerts": True,
    "max_landed": 25.0,         # a card costing more than this delivered is skipped
    "max_landed_auto": 75.0,    # the same for an autograph
    # high-end noise a frugal collector never wins cheap
    "skip_words": ["1/1", "1 of 1", "one of one", "superfractor", "printing plate"],
}
SETTING_KEYS = ("max_share", "bid_now_alerts", "drop_alerts")   # Home Assistant may override
AUTO_WORDS = re.compile(r"\b(auto|autograph|autographed|signed|signature)\b", re.I)

RADAR_SIZE = 10
DROP_HOURS = 24         # a price cut this recent still counts as "just dropped"
DROP_SENT_KEY = "drops_sent"
KEEP_SENT = 300


# ------------------------------------------------------------ steals mode
def profile_config() -> dict | None:
    """[steals] with defaults, or None when the profile has none (steals mode off)."""
    who = profile.get()
    raw = getattr(who, "steals", None)
    if raw is None or raw.get("enabled") is False:
        return None
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    cfg["max_share"] = min(1.0, max(0.3, float(cfg["max_share"])))
    return cfg


def on() -> bool:
    try:
        return profile_config() is not None
    except (profile.ProfileError, OSError, ValueError):
        return False


def settings_defaults() -> dict:
    """The settings steals mode starts from: settings.defaults() puts these on top."""
    cfg = profile_config()
    return {} if cfg is None else {k: cfg[k] for k in SETTING_KEYS}


def fit(pick: dict) -> dict | None:
    """The pick held to max_share, or None when it can't be a steal: an auction's max is
    cut so winning at it costs at most that share of the going rate with shipping, and
    a Buy It Now over it is no pick. Want-list cards keep their own ceilings; a pick
    with no value is left alone. With max_share at 1.0 (player mode) nothing changes."""
    share = settings.load()["max_share"]
    value = pick.get("value")
    if share >= 1.0 or not value or pick.get("want"):
        return pick
    ship = 0.0 if pick.get("combined_ship") else (
        pick["shipping"] if pick.get("shipping") is not None else sales.UNKNOWN_SHIPPING)
    if not pick.get("is_auction"):
        return None if pick["price"] + ship > share * value else pick
    steal = pricing.odd_cents(share * value - ship) if share * value - ship > 0 else None
    if steal is None or steal < pick["price"]:
        return None
    if pick.get("max") is not None and steal < pick["max"]:
        pick = dict(pick, max=steal, max_claude=pick.get("max_claude", pick["max"]))
    return pick


def too_dear(item, cfg: dict) -> str | None:
    """Why a listing is no steals-mode find, or None when it is worth scoring."""
    title = (item.title or "").lower()
    if any(w.lower() in title for w in cfg["skip_words"]):
        return "high-end"
    ship = item.shipping if item.shipping is not None else sales.UNKNOWN_SHIPPING
    ceiling = cfg["max_landed_auto"] if AUTO_WORDS.search(title) else cfg["max_landed"]
    return "over the ceiling" if (item.price or 0) + ship > ceiling else None


def keep(listings: list) -> tuple[list, int]:
    """Steals mode's cheap filter, before anything is scored: (kept, dropped count).
    Everything is kept when steals mode is off."""
    cfg = profile_config() if on() else None
    if cfg is None:
        return list(listings), 0
    kept = [i for i in listings if too_dear(i, cfg) is None]
    return kept, len(listings) - len(kept)


def prompt_rule() -> str:
    """One line for the scoring prompt when steals mode is on, else nothing."""
    if not on():
        return ""
    pct = round(100 * settings.load()["max_share"])
    return (f"- Steals only: pick a card only if it can be had for {pct}% of its going "
            f"rate or less, shipping included; set an auction's max to fit that.\n")


# ------------------------------------------------------------ dashboard
def _line(p: dict) -> tuple[float | None, str | None]:
    """The share of value at which this listing becomes a no-think buy, and which kind."""
    lines = []
    if float(p["value"]) >= jackpot.MIN_VALUE:
        share = jackpot.SHARE_SALES if p.get("max_basis") == "sales" else jackpot.SHARE_GUESS
        lines.append((share, "jackpot"))
    if p.get("is_auction"):
        lines.append((bidnow.SHARE, "bid_now"))
    return max(lines) if lines else (None, None)


def _opens(p: dict, kind: str) -> str | None:
    """When an auction enters its alert window: jackpot's last hours, or bid-now's hour."""
    if not p.get("is_auction") or not p.get("end_time"):
        return None
    before = (timedelta(hours=jackpot.AUCTION_HOURS) if kind == "jackpot"
              else timedelta(minutes=bidnow.WINDOW_MINUTES))
    return (datetime.fromisoformat(p["end_time"]) - before).isoformat(timespec="minutes")


def ending_order(row: dict) -> tuple:
    """Soonest-ending auction first, so nothing slips through; Buy It Nows (which don't
    end) after them, closest to the line first."""
    return (not row["is_auction"], row.get("end_time") or "9",
            row["share_pct"] - row["line_pct"])


def radar(con, now: datetime | None = None, picks: list[dict] | None = None) -> list[dict]:
    """The listings nearest to a jackpot or a bid-now, soonest ending first."""
    from . import snapshot   # snapshot imports this module; kept local
    now = now or datetime.now(config.TZ)
    live = picks if picks is not None else snapshot.live_picks(con, now)
    pool = {p["item_id"]: p for p in bidnow.candidates(con, now)}
    pool.update({p["item_id"]: p for p in live if not p.get("is_auction")})
    out = []
    for p in pool.values():
        if not p.get("value") or not bidnow._trusted(p):
            continue
        if p.get("is_auction") and (not p.get("end_time")
                                    or datetime.fromisoformat(p["end_time"]) <= now):
            continue
        line, kind = _line(p)
        if line is None:
            continue
        pay, value = jackpot.pay(p), float(p["value"])
        hot = ("jackpot" if jackpot.check(p, now) else
               "bid_now" if bidnow.qualifies(p, now) else None)
        out.append({
            "item_id": p["item_id"], "card": str(p.get("card") or p.get("title"))[:90],
            "url": p.get("url"), "image": p.get("image"), "pay": pay, "value": value,
            "share_pct": round(100 * pay / value), "line_pct": round(100 * line),
            "kind": hot or kind, "hot": bool(hot),
            "gap": max(0.0, round(pay - line * value, 2)),   # dollars still above the line
            # under the line already, waiting only for the auction's last hours
            "in_range": not hot and pay <= line * value,
            "opens_at": _opens(p, kind) if not hot else None,
            "is_auction": bool(p.get("is_auction")), "end_time": p.get("end_time"),
            "max": p.get("max"), "max_basis": p.get("max_basis"),
            "best_offer": bool(p.get("best_offer"))})
    # the closest RADAR_SIZE to the line, then shown in ending order
    out.sort(key=lambda r: (not r["hot"], r["share_pct"] - r["line_pct"]))
    return sorted(out[:RADAR_SIZE], key=ending_order)


def bundles(picks: list[dict], combined_sellers: list[str]) -> list[dict]:
    """Two or more picks from one seller: the whole lot delivered, shipping paid once
    where the seller combines it. The most saved first."""
    combined = {s.lower() for s in combined_sellers}
    by_seller: dict[str, list[dict]] = {}
    for p in picks:
        if p.get("seller") and p.get("value"):
            by_seller.setdefault(p["seller"], []).append(p)
    out = []
    for seller, cards in by_seller.items():
        if len(cards) < 2:
            continue
        cards.sort(key=lambda p: p.get("end_time") or "9")
        cost = sum(float(p["max"] if p.get("is_auction") and p.get("max") is not None
                         else p["price"]) for p in cards)
        ships = [0.0 if p.get("shipping") is None else float(p["shipping"]) for p in cards]
        joins = seller.lower() in combined
        if any(p.get("combined_ship") for p in cards):
            ship = 0.0                     # an open order already paid the shipping
        else:
            ship = max(ships) if joins else sum(ships)
        value = sum(float(p["value"]) for p in cards)
        total = round(cost + ship, 2)
        out.append({
            "seller": seller, "combined": joins, "n": len(cards), "cost": round(cost, 2),
            "shipping": round(ship, 2), "total": total, "value": round(value, 2),
            "share_pct": round(100 * total / value) if value else None,
            "saved": round(value - total, 2), "per_card": round(total / len(cards), 2),
            "shipping_saved": round(sum(ships) - ship, 2),
            "first_end": next((p["end_time"] for p in cards if p.get("end_time")), None),
            # one line per auction, for a Gixen group snipe: item number and max
            "gixen": "\n".join(f"{p['item_id']} {float(p['max']):.2f}" for p in cards
                               if p.get("is_auction") and p.get("max") is not None),
            "cards": [{"item_id": p["item_id"], "card": str(p.get("card"))[:70],
                       "url": p.get("url"), "image": p.get("image"),
                       "is_auction": bool(p.get("is_auction")), "price": p.get("price"),
                       "max": p.get("max"), "value": p.get("value"),
                       "end_time": p.get("end_time")} for p in cards]})
    out.sort(key=lambda b: -b["saved"])
    return out


def bought(con) -> list[dict]:
    """Bought picks with what was paid and what each was worth, newest first.
    Joined on the eBay item number in inventory.csv; nothing else is guessed."""
    try:
        with open(config.INVENTORY, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []
    owned = {r["ebay_item_id"].strip(): r for r in rows if (r.get("ebay_item_id") or "").strip()}
    out = []
    for item_id, data in con.execute("SELECT item_id, data FROM picks"):
        row = owned.get(item_id)
        if not row:
            continue
        pick = json.loads(data)
        try:
            paid, value = float(row.get("price_paid") or ""), float(pick.get("value") or 0)
        except ValueError:
            continue
        if value <= 0 or paid <= 0:
            continue
        out.append({"item_id": item_id, "card": str(pick.get("card"))[:90], "paid": paid,
                    "value": value, "saved": round(value - paid, 2),
                    "pct": round(100 * paid / value), "image": pick.get("image"),
                    "date": (row.get("order_date") or "").strip()})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def savings(con, now: datetime | None = None) -> dict:
    """Bought picks: what they were worth against what was paid, this month and in all."""
    now = now or datetime.now(config.TZ)
    month = now.strftime("%Y-%m")
    tally = {"month": {"n": 0, "paid": 0.0, "value": 0.0},
             "all": {"n": 0, "paid": 0.0, "value": 0.0}}
    best = None
    for r in bought(con):
        for k in ["all"] + (["month"] if r["date"].startswith(month) else []):
            tally[k]["n"] += 1
            tally[k]["paid"] += r["paid"]
            tally[k]["value"] += r["value"]
        if best is None or r["saved"] > best["saved"]:
            best = {k: r[k] for k in ("card", "paid", "value", "saved", "pct", "image", "date")}
    out = {}
    for k, t in tally.items():
        out[k] = {"n": t["n"], "paid": round(t["paid"], 2), "value": round(t["value"], 2),
                  "saved": round(t["value"] - t["paid"], 2),
                  "pct": round(100 * t["paid"] / t["value"]) if t["value"] else None}
    out["best"] = best
    return out


def dropped_into_range(con, picks: list[dict], now: datetime | None = None) -> list[dict]:
    """Buy It Now picks whose price was cut within DROP_HOURS and now cost at most
    max_share of their value delivered (half, when max_share is left at 1.0)."""
    now = now or datetime.now(config.TZ)
    since = now.timestamp() - DROP_HOURS * 3600
    moves = {m["item_id"]: m for m in json.loads(db.kv_get(con, "moves", "[]") or "[]")
             if m.get("at", 0) >= since and m.get("old")}
    share = settings.load(con)["max_share"]
    share = DEFAULTS["max_share"] if share >= 1.0 else share
    found = []
    for p in picks:
        m = moves.get(p["item_id"])
        if (m and not p.get("is_auction") and p.get("value") and bidnow._trusted(p)
                and jackpot.pay(p) <= share * float(p["value"])):
            found.append(dict(p, was=m["old"], cut_pct=m.get("cut_pct")))
    return found


def drop_alert(con, picks: list[dict], now: datetime | None = None) -> list[dict]:
    """One push per Buy It Now that a seller just cut into steal range."""
    from . import notify   # notify imports score; kept local
    now = now or datetime.now(config.TZ)
    if not settings.load(con)["drop_alerts"] or not notify.configured():
        return []
    sent = json.loads(db.kv_get(con, DROP_SENT_KEY, "[]") or "[]")
    skip = set(sent) | set(json.loads(db.kv_get(con, jackpot.SENT_KEY, "[]") or "[]"))
    done = []
    for p in dropped_into_range(con, picks, now):
        if p["item_id"] in skip:
            continue
        pay, value = jackpot.pay(p), float(p["value"])
        title = f"📉 JUST DROPPED: ${value:.0f} card now ${pay:.2f} delivered"
        body = "\n".join([
            str(p.get("card"))[:90],
            f"Was ${float(p['was']):.2f}, now ${float(p['price']):.2f}"
            + (f" ({p['cut_pct']}% cut)" if p.get("cut_pct") else "")
            + f" · {round(100 * (1 - pay / value))}% under its value.",
            ("Worth about ${:.2f} going by recorded sales.".format(value)
             if p.get("max_basis") == "sales" else
             "Worth about ${:.2f} by the bot's estimate.".format(value)),
            "Buy it now, think later.", f"Item {p['item_id']}"])
        try:
            notify.send(title, body, url=p.get("url"), image=p.get("image"), urgent=True)
        except Exception as exc:   # the next run tries again
            print(f"drops: {exc}", file=sys.stderr)
            break
        sent.append(p["item_id"])
        done.append(p)
    if done:
        db.kv_set(con, DROP_SENT_KEY, json.dumps(sent[-KEEP_SENT:]))
        db.bump(con, "drops_sent", len(done))
        con.commit()
        print(f"drops: {len(done)} sent", file=sys.stderr)
    return done
