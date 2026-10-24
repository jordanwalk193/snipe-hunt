"""Lowball snipes: auctions of $15+ cards still near the opening bid.

Most cheap auctions of a good card end near its going rate; a few end far under
it, mostly ones few bidders see. Two ways to catch those:

- The shotgun list: every auction of a card worth MIN_VALUE or more that is still
  under half its going rate and ends within two days, with one low snipe each
  (a share of the going rate). Spread over many auctions, some win at a
  fraction of the price. The share is learned: it is set so about one in five
  of the auctions it was tried on would have been won.
- Last call: between hourly runs, the targets ending within LAST_CALL_HOURS are
  looked up again, and the ones still far under their going rate with no
  more than one bid go to the phone, so a snipe can still be set.

Auctions ending at dead hours (overnight, holidays, the Super Bowl) and ones most
buyers scroll past (high or unknown shipping, from abroad, no photo) are marked;
those are where cheap endings come from. Only recommends: you set every snipe.
The holidays are US ones and the hours are the bot's time zone (SNIPEHUNT_TZ).
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone

from . import config, db, pricing, rarity, sales, settings, snipecheck
from .ebay_search import Listing, _money
from .offers import rejected

KEY = "lowball"
LOG_KEY = "lowball_log"         # targets offered, to see later whether the snipe would have won
RECORD_KEY = "lowball_record"   # how those auctions ended
SENT_KEY = "last_call_sent"
MIN_VALUE = 15.00       # cards worth less aren't worth a lowball
UNDER = 0.50            # still under this share of the going rate, shipping counted
WITHIN_HOURS = 48       # targets end within this many hours
LEVEL = 0.35            # the snipe, as a share of the going rate, until there's a record
LEVEL_RANGE = (0.20, 0.60)
WIN_SHARE = 0.20        # the learned level would have won about this share
MIN_RECORD = 30         # fewer endings than this and LEVEL stands
CALL_MIN_VALUE = 20.00  # last call only for cards worth this much
CALL_UNDER = 0.30       # and still under this share of the going rate
CALL_MAX_BIDS = 1
LAST_CALL_HOURS = 3
PROMISING_UNDER = 8.00  # what an auction may be at now to be worth pricing
HIGH_SHIPPING = 5.00
LOG_DAYS = 120          # how long a target's going rate is kept for learning


# ------------------------------------------------------------ when it ends
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def dead_hour(end: datetime, learned: dict | None = None) -> str | None:
    """Why fewer bidders are around when an auction ends at `end` (bot's time zone), or None."""
    end = end.astimezone(config.TZ)
    day = end.date()
    holidays = {date(day.year, 1, 1): "New Year's Day", date(day.year, 7, 4): "July 4th",
                date(day.year, 12, 24): "Christmas Eve", date(day.year, 12, 25): "Christmas",
                date(day.year, 12, 31): "New Year's Eve",
                _nth_weekday(day.year, 11, 3, 4): "Thanksgiving"}
    if day in holidays:
        return f"ends on {holidays[day]}"
    if day == _nth_weekday(day.year, 2, 6, 2) and 18 <= end.hour < 23:
        return "ends during the Super Bowl"
    if 1 <= end.hour < 7:
        return "ends overnight"
    bucket = (learned or {}).get(str(end.hour // 3 * 3))
    if bucket and bucket["n"] >= MIN_RECORD and bucket["median"] <= 0.8:
        return f"ends {end.hour // 3 * 3}:00–{end.hour // 3 * 3 + 3}:00, when sales run cheap"
    return None


def overlooked(item: Listing) -> list[str]:
    """What makes most buyers scroll past a listing whose total may still be fine."""
    out = []
    if item.shipping is None:
        out.append("shipping not stated")
    elif item.shipping >= HIGH_SHIPPING and item.shipping >= item.price:
        out.append(f"${item.shipping:.2f} shipping")
    if item.country and item.country != "US":
        out.append(f"ships from {item.country}")
    if not item.image:
        out.append("no photo")
    return out


# ------------------------------------------------------------ what it learns
def outcomes(con) -> list[dict]:
    """Auctions of $15+ cards that have ended: the delivered price as a share of the
    going rate, and the local hour. From the bot's picks and the lowball log."""
    out, logged = [], json.loads(db.kv_get(con, LOG_KEY, "{}"))
    for row in sales.with_calls(con):
        value = (row.get("call") or {}).get("value") or (logged.get(row["item_id"]) or {}).get("value")
        if row["kind"] == "bin" or not value or value < MIN_VALUE:
            continue
        out.append({"item_id": row["item_id"], "share": row["total"] / value,
                    "hour": datetime.fromtimestamp(row["sold_at"], config.TZ).hour})
    seen = {o["item_id"] for o in out}
    for o in json.loads(db.kv_get(con, RECORD_KEY, "[]")):   # ended with no bids
        if o["item_id"] not in seen:
            out.append(o)
    return out


def _settle(con, now: float) -> None:
    """Logged targets whose auction closed with no sale: the opening bid would have won.
    A sold one stays logged, since outcomes() takes its going rate from the log."""
    logged = json.loads(db.kv_get(con, LOG_KEY, "{}"))
    record = json.loads(db.kv_get(con, RECORD_KEY, "[]"))
    for item_id, t in list(logged.items()):
        if t["end_ts"] > now - 3600:
            continue
        if t["end_ts"] < now - LOG_DAYS * 86400:
            logged.pop(item_id)
            continue
        if con.execute("SELECT 1 FROM sales WHERE item_id=?", (item_id,)).fetchone():
            continue
        row = con.execute("SELECT closed_at, price, shipping FROM seen WHERE item_id=?",
                          (item_id,)).fetchone()
        if row and row["closed_at"]:
            record.append({"item_id": item_id, "share": sales.delivered(
                row["price"] or 0, row["shipping"]) / t["value"],
                "hour": datetime.fromtimestamp(t["end_ts"], config.TZ).hour})
            logged.pop(item_id)
    db.kv_set(con, LOG_KEY, json.dumps(logged))
    db.kv_set(con, RECORD_KEY, json.dumps(record[-300:]))


def level(results: list[dict]) -> float:
    """The share of the going rate to snipe at: low enough to be a steal, high enough
    that about WIN_SHARE of these auctions would have been won."""
    if len(results) < MIN_RECORD:
        return LEVEL
    shares = sorted(r["share"] for r in results)
    at = shares[min(len(shares) - 1, int(len(shares) * WIN_SHARE))]
    return round(min(max(at, LEVEL_RANGE[0]), LEVEL_RANGE[1]), 2)


def hours(results: list[dict]) -> dict:
    """Per three-hour block of local ending time: how many, and the median share."""
    blocks: dict[str, list[float]] = {}
    for r in results:
        blocks.setdefault(str(r["hour"] // 3 * 3), []).append(r["share"])
    return {k: {"n": len(v), "median": round(statistics.median(v), 2)}
            for k, v in sorted(blocks.items(), key=lambda kv: int(kv[0]))}


# ------------------------------------------------------------ the list
def snipe_for(value: float, shipping: float | None, share: float) -> float | None:
    ship = sales.UNKNOWN_SHIPPING if shipping is None else shipping
    room = value * share - ship
    return pricing.odd_cents(room) if room >= 1 else None


def promising(item: Listing) -> bool:
    """Could this cheap auction be a $15+ card? Worth a Claude price if nothing has one."""
    run = rarity.print_run(item.title)
    return bool((run is not None and run <= 250) or rarity.AUTO.search(item.title)
                or rarity.GRADED.search(item.title)
                or any(w in item.title.lower() for w in
                       ("refractor", "1st bowman", "short print", " ssp", "superfractor",
                        "case hit", "patch", "lot")))


def _auctions(listings: list[Listing], now: datetime) -> list[Listing]:
    soon = now + timedelta(hours=WITHIN_HOURS)
    return [i for i in listings if i.is_auction and i.match != "no" and i.currency == "USD"
            and i.end_time and now < datetime.fromisoformat(i.end_time) <= soon]


def find(con, listings: list[Listing], inventory: list[dict], sniped: set[str] = frozenset(),
         now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows, cache, skip = sales.with_calls(con), snipecheck._cache(con), rejected(con)
    results = outcomes(con)
    share, blocks = level(results), hours(results)
    targets = []
    for item in _auctions(listings, now):
        if item.item_id in skip or snipecheck.owned_copy(item.title, inventory):
            continue
        rate = snipecheck.going_rate(con, {"item_id": item.item_id, "title": item.title},
                                     rows, cache)
        value = float(rate["value"]) if rate and rate.get("value") else None
        if not value or value < MIN_VALUE or item.total > value * UNDER:
            continue
        snipe = snipe_for(value, item.shipping, share)
        if snipe is None or snipe < item.price:
            continue
        end = datetime.fromisoformat(item.end_time)
        targets.append({
            "item_id": item.item_id, "title": item.title, "url": item.url, "image": item.image,
            "price": item.price, "shipping": item.shipping, "bids": item.bid_count or 0,
            "end_time": item.end_time, "value": round(value, 2), "basis": rate.get("basis"),
            "snipe": snipe, "share": round(item.total / value, 2),
            "dead": dead_hour(end, blocks), "overlooked": overlooked(item),
            "sniped": item.item_id in sniped, "seller": item.seller})
    # the deepest discounts first; a dead hour or a listing others skip breaks ties
    targets.sort(key=lambda t: (t["share"] - 0.1 * bool(t["dead"]) - 0.05 * bool(t["overlooked"]),
                                t["end_time"]))
    won = sum(r["share"] <= share for r in results)
    return {"targets": targets, "level": share, "learned": len(results) >= MIN_RECORD,
            "record": {"n": len(results), "won": won}, "hours": blocks}


def _appraise(con, listings: list[Listing], now: datetime | None = None) -> int:
    """Price the cheap auctions that could be good cards and that nothing has priced."""
    now = now or datetime.now(timezone.utc)
    todo = [i for i in _auctions(listings, now)
            if i.total <= PROMISING_UNDER and promising(i)]
    return snipecheck.appraise(   # it prices at most 20, the ones nothing has priced
        con, [{"item_id": i.item_id, "title": i.title, "current_bid": i.price,
               "end_time": i.end_time} for i in todo],
        doing="is looking for lowball snipes on")


def save(con, listings: list[Listing], inventory: list[dict], sniped: set[str] = frozenset(),
         now: datetime | None = None, appraise: bool = True) -> int:
    now = now or datetime.now(timezone.utc)
    _settle(con, now.timestamp())
    if appraise:
        _appraise(con, listings, now)
    found = find(con, listings, inventory, sniped, now)
    logged = json.loads(db.kv_get(con, LOG_KEY, "{}"))
    for t in found["targets"]:
        logged[t["item_id"]] = {"value": t["value"], "snipe": t["snipe"],
                                "end_ts": int(datetime.fromisoformat(t["end_time"]).timestamp())}
    db.kv_set(con, LOG_KEY, json.dumps(logged))
    found["targets"] = found["targets"][:60]
    db.kv_set(con, KEY, json.dumps(found, ensure_ascii=False))
    return len(found["targets"])


def load(con) -> dict:
    return json.loads(db.kv_get(con, KEY, "{}") or "{}")


# ------------------------------------------------------------ last call
def last_call(con, client, sniped: set[str] = frozenset(), now: datetime | None = None,
              send: bool = True) -> list[dict]:
    """Targets ending soon that are still far under their going rate: one phone alert."""
    from . import notify   # notify imports score; kept local
    now = now or datetime.now(config.TZ)
    soon = now + timedelta(hours=LAST_CALL_HOURS)
    sent = json.loads(db.kv_get(con, SENT_KEY, "[]"))
    due = [t for t in load(con).get("targets", [])
           if t["value"] >= CALL_MIN_VALUE and t["item_id"] not in sent
           and t["item_id"] not in sniped
           and now + timedelta(minutes=5) < datetime.fromisoformat(t["end_time"]) <= soon]
    if notify.is_quiet(now):   # in quiet hours only what ends within the urgent window
        urgent = now + timedelta(hours=settings.load(con)["urgent_hours"])
        due = [t for t in due if datetime.fromisoformat(t["end_time"]) <= urgent]
    from . import usage
    hits = []
    for t in due:
        if not usage.ebay_allowed("lowball", con):
            break
        raw = client.item(t["item_id"], lane="lowball", fresh=True)   # this minute's bids
        if not raw:
            continue
        price, _ = _money(raw.get("currentBidPrice") or raw.get("price"))
        bids = raw.get("bidCount") or 0
        if price is None or bids > CALL_MAX_BIDS:
            continue
        total = sales.delivered(price, t["shipping"])
        if total > t["value"] * CALL_UNDER or t["snipe"] < price:
            continue
        hits.append(dict(t, price=price, bids=bids))
    if hits and send and notify.configured() and settings.load(con)["alerts_enabled"]:
        blocks = []
        for h in hits:
            ends = datetime.fromisoformat(h["end_time"]).astimezone(config.TZ)
            blocks.append(f"{h['title'][:80]}\nNow ${h['price']:.2f} + "
                          + ("ship ?" if h["shipping"] is None else f"${h['shipping']:.2f} ship")
                          + f" · {h['bids']} bid{'s' if h['bids'] != 1 else ''} · ends "
                          f"{ends.strftime('%I:%M %p').lstrip('0')}\nGoes for ~${h['value']:.2f} · Snipe: {h['snipe']:.2f}"
                          + (f"\n{h['dead']}" if h["dead"] else "") + f"\n{h['item_id']}")
        notify.send(f"🎯 Last call: {len(hits)} cheap auction{'s' if len(hits) != 1 else ''}",
                    "\n\n".join(blocks), url=hits[0]["url"], image=hits[0].get("image"))
    if hits and send:
        db.kv_set(con, SENT_KEY, json.dumps((sent + [h["item_id"] for h in hits])[-300:]))
        con.commit()
    return hits
