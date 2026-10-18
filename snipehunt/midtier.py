"""The mid-tier lane: $20-$80 cards whose listings get them wrong.

A card listed with the name misspelled, the autograph or parallel only in the
item specifics, the wrong year, or in the wrong category is one most buyers
never see. This lane searches for the player's cards in that price band
(misspellings first), reads the item specifics of the likely ones within a
daily budget of single-listing reads, runs the mislabel detectors and sends
only the listings they flag to Claude. A pick alerts only when recorded sales
or Claude's value put it at `deal_ratio` of its going rate or better, with
the max worked out here as an odd-cent cheapskate snipe.

Recommend only, like everything else: searches and single-listing reads, never
a bid, an offer or a purchase. Turned on by [search.midtier] in the profile.
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import (aspects, config, db, ebay_search, filters, hidden, known_cards, mislabel,
               notify, pricing, profile, sales, score, settings, usage)
from .ebay_search import EbayClient, Listing, price_filter

# Everything [search.midtier] can set, and what it is without a value there.
DEFAULTS = {
    "enabled": True,
    "min_pay": 20.0,               # the band is on what you'd pay (max + ship), not the bid
    "max_pay": 80.0,
    "category_id": mislabel.SINGLES,
    "auction_window_hours": 36,
    "deal_ratio": 0.60,            # alert only at this share of the going rate or less
    "min_comps_for_5_star": 3,
    "getitem_daily_budget": 120,
    "search_daily_budget": 400,
    "odd_hours_et": [1, 6],        # both hours included: 1:00 to 6:59 ET
    "odd_weekdays": ["Mon", "Tue", "Wed", "Thu"],
    "thin_listing_max_title_tokens": 7,
    "new_seller_feedback_below": 50,
    "allow_lots": False,
}
LABEL = "midtier"
PENDING_KEY = "midtier_pending"
HOURS = 24                       # runs a day: a day's searches are spread over them


def profile_config() -> dict | None:
    """[search.midtier] with defaults, or None when the profile has none (lane off)."""
    who = profile.get()
    if getattr(who, "midtier", None) is None:
        return None
    return {**DEFAULTS, **{k: v for k, v in who.midtier.items() if k in DEFAULTS}}


def config_now(con=None) -> dict | None:
    """The profile's settings, with Home Assistant's switches on top."""
    cfg = profile_config()
    if cfg is None:
        return None
    live = settings.load(con)
    for key, name in settings.MIDTIER_KEYS.items():
        if key in live:
            cfg[name] = live[key]
    return cfg


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def auction_filter(cfg: dict, now: datetime) -> str:
    """Auctions ending in the window with a current price under the band's top.

    The bottom is left open: a $9 bid today can still be a $30 card."""
    until = now + timedelta(hours=cfg["auction_window_hours"])
    return (price_filter(high=cfg["max_pay"]) + ",buyingOptions:{AUCTION}"
            f",itemEndDate:[{_iso(now)}..{_iso(until)}]")


def bin_filter(cfg: dict) -> str:
    return (price_filter(cfg["min_pay"], cfg["max_pay"])
            + ",buyingOptions:{FIXED_PRICE|BEST_OFFER}")


def words() -> list[tuple[str, str, str]]:
    """(kind, label part, q) to search for, most valuable first: misspellings, then
    the name ("name" kind, searched every run), then keyword searches in turn."""
    who = profile.get()
    name = who.search_name
    slug = lambda text: re.sub(r"\W+", "-", text.lower()).strip("-")   # noqa: E731
    out = [("name", slug(q), q) for q in dict.fromkeys([*(who.typo_searches or []), name])]
    out += [("keyword", slug(f"{name} {k}") if slug(k) else slug(name) + "-numbered",
             f"{name} {k}") for k in who.keyword_searches]
    return out


def queries(cfg: dict, now: datetime | None = None) -> list[tuple[str, str, str, str]]:
    """(label, q, filter, sort) for every mid-tier search, two per search word."""
    return [q for _, q in _queries(cfg, now)]


def _queries(cfg: dict, now: datetime | None) -> list[tuple[str, tuple]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for kind, part, q in words():
        out.append((kind, (f"{LABEL}-auction-{part}", q, auction_filter(cfg, now),
                           "endingSoonest")))
        out.append((kind, (f"{LABEL}-bin-{part}", q, bin_filter(cfg), "newlyListed")))
    return out


def this_run(cfg: dict, now: datetime) -> list:
    """The searches for this hour: misspellings and the name every time, keywords in turn.

    A day's `search_daily_budget` is spread over the hours, so it isn't spent by noon."""
    per_run = max(2, math.ceil(cfg["search_daily_budget"] / HOURS))
    todo = _queries(cfg, now)
    names = [q for kind, q in todo if kind == "name"]
    rest = [q for kind, q in todo if kind != "name"]
    if rest:
        turn = int(now.timestamp() // 3600) % len(rest)
        rest = rest[turn:] + rest[:turn]
    return (names + rest)[:per_run]


def pay_now(item: Listing) -> float:
    """The least you could pay right now, shipping included: the price, or the next bid."""
    ship = item.shipping if item.shipping is not None else sales.UNKNOWN_SHIPPING
    price = pricing.next_bid(item.price, item.bid_count) if item.is_auction else item.price
    return round(price + ship, 2)


def in_band(item: Listing, cfg: dict) -> bool:
    """eBay's price filter isn't always exact, so the band is checked here as well."""
    if item.is_auction:
        return pay_now(item) <= cfg["max_pay"]
    return cfg["min_pay"] <= pay_now(item) <= cfg["max_pay"]


def _lane_calls_today(lane: str) -> int:
    return usage.ebay_used(lane)


def hunt(con, client: EbayClient | None = None, now: datetime | None = None,
         skip: set[str] = frozenset(), verbose: bool = False) -> tuple[list[Listing], dict]:
    """Search, filter, read item specifics and detect. Returns (listings for Claude, counts).

    `skip` are item numbers to leave out (already being scored this run)."""
    cfg = config_now(con)
    stats = {"searches": 0, "found": 0, "in_band": 0, "junk": 0, "enriched": 0,
             "flagged": 0, "skipped_budget": 0}
    if not cfg or not cfg["enabled"] or profile.get().mode == "team":
        return [], stats
    now = now or datetime.now(timezone.utc)
    client = client or EbayClient(con)
    client.attempts = 1   # the lowest lane: no waiting on a slow eBay, try next hour
    who = profile.get()
    known = known_cards.load(hidden.card_numbers(con))
    found: dict[str, Listing] = {}
    looked: set[str] = set()   # every listing counted once, however many searches return it
    for label, q, flt, sort in this_run(cfg, now):
        if (_lane_calls_today("midtier") >= cfg["search_daily_budget"]
                or not usage.ebay_allowed("midtier", con)):
            stats["skipped_budget"] += 1
            continue
        client.lane = label
        try:
            raw_items = client.search(q, flt, sort, limit=200, category=str(cfg["category_id"]))
        except ebay_search.RateLimited as exc:
            print(f"midtier: rate limited, the lane waits for the next run: {exc}",
                  file=sys.stderr)
            break
        except Exception as exc:   # eBay is down or refusing: the lane waits for the next run
            print(f"midtier: {label}: {exc}; the lane stops for this run", file=sys.stderr)
            break
        stats["searches"] += 1
        for raw in raw_items:
            item = ebay_search.parse_item(raw)
            if item is None or item.currency != "USD" or item.item_id in skip:
                continue
            if item.item_id in found:   # found by an earlier search this run
                found[item.item_id].queries.append(label)
                continue
            if item.item_id in looked:
                continue
            looked.add(item.item_id)
            stats["found"] += 1
            item.match = who.classify(item.title)
            if item.match == "no" or not in_band(item, cfg):
                continue
            if filters.junk(item.title, lots=bool(cfg["allow_lots"])):
                stats["junk"] += 1
                continue
            stats["in_band"] += 1
            item.lane = LABEL
            item.queries.append(label)
            found[item.item_id] = item
    out = []
    names = None
    for item in found.values():
        if not db.needs_scoring(con, item.item_id, item.total):
            continue
        signals = mislabel.title_signals(item, known, cfg)
        comps = sales.ledger_comps(con, item)
        rated = score.thin_comps(comps, full=cfg["min_comps_for_5_star"])
        value = rated["value"] if rated else None
        reach = bool(value) and pay_now(item) <= cfg["deal_ratio"] * value
        if not (reach or any(s.positive and s.name not in mislabel.TIMING for s in signals)):
            continue
        if _lane_calls_today("midtier-item") < cfg["getitem_daily_budget"] and \
                usage.ebay_allowed("midtier", con):
            try:
                raw = client.item(item.item_id, lane="midtier-item")
            except Exception as exc:   # still judged on its title alone
                print(f"midtier: {item.item_id}: {exc}", file=sys.stderr)
                raw = None
            if raw:
                ebay_search.enrich(item, raw)
                stats["enriched"] += 1
        names = names or aspects.names(con)
        filters.flag(item, names)
        signals = mislabel.detect(item, known, cfg, value, names)
        item.signals = [s.to_dict() for s in signals]
        forgive = bool(rated) and "fake_auto" in item.flags
        if mislabel.qualifies(signals, forgive_facsimile=forgive):
            stats["flagged"] += 1
            out.append(item)
    for key in ("searches", "enriched", "flagged"):
        db.bump(con, f"midtier_{key}", stats[key])
    db.bump(con, "midtier_searched", stats["found"])
    con.commit()
    if verbose:
        print(f"  midtier: {stats['searches']} searches, {stats['in_band']} in band, "
              f"{stats['enriched']} read in full, {stats['flagged']} flagged", file=sys.stderr)
    return out, stats


def decide(pick: dict, item: Listing, cfg: dict, comps: list[dict] | None = None,
           now: float | None = None) -> dict:
    """The lane's own max, stars and alert decision for one of Claude's picks.

    rec_max = the lower of deal_ratio x value and max_pay, less shipping. It
    alerts only when rec_max plus shipping reaches min_pay, the price you could
    pay now is within deal_ratio of the value, Claude was sure which card it
    is, and the signals still say mislabeled."""
    rated = score.thin_comps(comps or [], now, full=cfg["min_comps_for_5_star"])
    value = rated["value"] if rated else pick.get("value")
    ship = item.shipping if item.shipping is not None else sales.UNKNOWN_SHIPPING
    signals = mislabel.as_signals(item.signals)
    lane = {"signals": item.signals, "alert": False, "value": value, "ship": ship,
            "comps_n": rated["n"] if rated else 0, "reason": ""}
    pick.update(lane="midtier", midtier=lane)
    if not value:
        lane["reason"] = "no value"
        return pick
    rec_max = round(min(cfg["deal_ratio"] * value - ship, cfg["max_pay"] - ship), 2)
    lane["rec_max"] = rec_max
    ok = cfg["deal_ratio"] * value
    if rec_max + ship < cfg["min_pay"]:
        lane["reason"] = "under the band"
    elif pay_now(item) > ok + 1e-9:
        lane["reason"] = "price already past the deal line"
    elif pick.get("identity") == "unsure":
        lane["reason"] = "card not pinned down"
    elif not mislabel.qualifies(signals, forgive_facsimile=bool(rated)):
        lane["reason"] = "signals not enough"
    elif any(s.name == "facsimile_guard" for s in signals) and not rated:
        lane["reason"] = "printed signature with no sales of the unsigned card"
    else:
        lane["alert"] = True
    pick["value"] = value
    if item.is_auction:
        pick["max"] = pricing.odd_cents(rec_max)
        pay = pick["max"] + ship
    else:
        lane["bin_ok"] = rec_max
        if "BEST_OFFER" in item.buying_options:
            lane["offer_idea"] = pricing.offer_idea(rec_max)
        pay = item.price + ship
    pick["deal"], pick["deal_pct"] = score.rate(pay, value)
    if rated:
        pick.update(max_basis="sales", comps_n=rated["n"], comps_newest=rated["newest"],
                    comps_cap=rated["cap"])
    if lane["alert"] and mislabel.qualifies(signals, forgive_facsimile=bool(rated)):
        pick["edge"] = "mislabeled"
    return score.apply_caps(pick)


def score_lane(con, items: list[Listing], on_batch=None) -> list[dict]:
    """Claude's picks among the flagged listings, each decided by the lane's rules."""
    if not items:
        return []
    cfg = config_now(con) or DEFAULTS
    picks, _ = score.score(items, verbose=True, on_batch=on_batch, con=con)
    by_id = {i.item_id: i for i in items}
    return [decide(p, by_id[p["item_id"]], cfg, sales.ledger_comps(con, by_id[p["item_id"]]))
            for p in picks if p["item_id"] in by_id]


def deliver(con, picks: list[dict], now: datetime | None = None, dry_run: bool = False) -> int:
    """Send the lane's alerts, in its own format; quiet hours hold them like any other."""
    if not settings.load(con)["alerts_enabled"] or not (notify.configured() or dry_run):
        return 0
    now = now or datetime.now(config.TZ)
    held = json.loads(db.kv_get(con, PENDING_KEY, "[]"))
    fresh = {p["item_id"] for p in picks}
    ready = [p for p in picks + [h for h in held if h["item_id"] not in fresh]
             if (p.get("midtier") or {}).get("alert")
             and db.may_alert(con, p["item_id"], p["total"])]
    send_now, hold = notify.split_for_quiet_hours(ready, now)
    sent = 0
    if send_now:
        title = (f"{profile.get().app_name}: {len(send_now)} midtier "
                 f"steal{'s' if len(send_now) != 1 else ''}")
        body = "\n\n".join(notify.format_midtier(p) for p in send_now)
        if dry_run:
            print(f"--- would send ---\n{title}\n{body}\n", file=sys.stderr)
        else:
            notify.send(title, body, send_now[0].get("url"), send_now[0].get("image"))
            for p in send_now:
                db.mark_alerted(con, p["item_id"], p["total"])
        sent = len(send_now)
    if not dry_run:
        db.kv_set(con, PENDING_KEY, json.dumps(hold))
        db.bump(con, "midtier_alerted", sent)
        con.commit()
    return sent


def summary(con, day: str | None = None) -> str:
    """One line on what the lane did on a day (yesterday's, for the daily job)."""
    day = day or (datetime.now(config.TZ).date() - timedelta(days=1)).isoformat()
    stats = db.stat_totals(con, day, day)
    n = lambda key: int(stats.get(key, 0))   # noqa: E731
    return (f"Midtier {day}: {n('midtier_searched')} listings searched, "
            f"{n('midtier_enriched')} read in full, {n('midtier_flagged')} flagged, "
            f"{n('midtier_alerted')} alerted · eBay calls {n('ebay_lane:midtier')} searches "
            f"+ {n('ebay_lane:midtier-item')} item reads")


def run(con, skip: set[str], save=None, dry_run: bool = False, verbose: bool = True
        ) -> tuple[list[dict], int]:
    """The lane, start to finish, for the hourly job. Returns (picks, alerts sent)."""
    items, _ = hunt(con, skip=skip, verbose=verbose)
    for item in items:   # seen, so sales.py records what they go for
        db.touch(con, item.item_id, item.title, item.is_auction,
                 datetime.fromisoformat(item.end_time).timestamp() if item.end_time else None,
                 item.shipping, item.price, item.seller)
    con.commit()
    picks = score_lane(con, items, on_batch=save)
    if not dry_run:
        for p in picks:
            db.save_pick(con, p)
        con.commit()
    return picks, deliver(con, picks, dry_run=dry_run)


if __name__ == "__main__":
    found, counts = hunt(db.connect(), verbose=True)
    for listing in found:
        print(listing.item_id, listing.title, [s["name"] for s in listing.signals])
    print(counts, time.strftime("%H:%M"))
