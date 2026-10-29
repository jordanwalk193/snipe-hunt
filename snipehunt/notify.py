"""Phone alerts through Home Assistant's notify service, or an ntfy topic."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import closes, config, db, filters, jackpot, pricing, profile, sales, score, settings

TIER_ICON = {"fire": "🔥", "green": "🟢", "yellow": "🟡", "drop": "⏰"}
TIER_ORDER = {"drop": 0, "fire": 1, "green": 2, "yellow": 3}
# Quiet hours, the urgent window and which tiers alert are in settings.py.
PER_MESSAGE = 5                  # picks per notification, so a phone shows them all
GROUP_TIP = "Tip: use a Gixen group bid."   # one snipe across several auctions of a card
MAX_MESSAGES = 3                 # per run; the rest go out on the next run
PENDING_KEY = "pending_picks"
BEDTIME_KEY = "bedtime_bundle_night"
BEDTIME_AFTER = 2                # hours past the end of quiet hours the bedtime bundle covers
BIN_GONE = 2 * 3600              # a Buy It Now out of the results this long has likely sold
# eBay shows many buyers Pacific time, so every end time is given in both.
ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")


def configured() -> bool:
    """Is there anywhere to send an alert?"""
    ha = all(config.get(k) for k in ("HA_URL", "HA_TOKEN", "HA_NOTIFY_SERVICE"))
    return ha or bool(config.get("NTFY_URL"))


def send(title: str, message: str, url: str | None = None,
         image: str | None = None, urgent: bool = False) -> None:
    """Send to Home Assistant if it is set up, and to ntfy if `NTFY_URL` is set.

    `urgent` (jackpots only) asks the phone to break through Focus / Do Not
    Disturb: time-sensitive on iPhone, high priority on Android, urgent on ntfy."""
    ha_url, token, service = (config.get(k) for k in ("HA_URL", "HA_TOKEN", "HA_NOTIFY_SERVICE"))
    ntfy = config.get("NTFY_URL")
    if not (ha_url and token and service) and not ntfy:
        raise config.ConfigError(
            "No way to send alerts: set HA_URL, HA_TOKEN and HA_NOTIFY_SERVICE, or NTFY_URL")
    if ha_url and token and service:
        data: dict = {}
        if url:
            data["url"] = url           # iOS
            data["clickAction"] = url   # Android
        if image:
            data["image"] = image
        if urgent:
            data["push"] = {"interruption-level": "time-sensitive"}   # iOS
            data.update(priority="high", ttl=0)                      # Android
        payload = {"title": title, "message": message}
        if data:
            payload["data"] = data
        resp = requests.post(
            f"{ha_url.rstrip('/')}/api/services/notify/{service}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"Home Assistant notify {resp.status_code}: {resp.text[:200]}")
    if ntfy:
        headers = {"Title": title.encode("utf-8")}
        if url:
            headers["Click"] = url
        if image:
            headers["Attach"] = image
        if urgent:
            headers["Priority"] = "urgent"
        resp = requests.post(ntfy, data=message.encode("utf-8"), headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"ntfy {resp.status_code}: {resp.text[:200]}")


def is_quiet(now: datetime | None = None) -> bool:
    cfg = settings.load()
    if not cfg["quiet_hours"]:
        return False
    start = settings.parse_time(cfg["quiet_start"])
    end = settings.parse_time(cfg["quiet_end"])
    at = (now or datetime.now(config.TZ)).time()
    if start <= end:
        return start <= at < end
    return at >= start or at < end   # the usual case: quiet hours cross midnight


def _ends(pick: dict) -> datetime | None:
    return datetime.fromisoformat(pick["end_time"]) if pick.get("end_time") else None


def is_urgent(pick: dict, now: datetime) -> bool:
    if pick.get("tier") == "drop":
        return True
    cfg = settings.load()
    if cfg["bedtime_bundle"] and is_quiet(now):
        return False   # the snipe was entered before bed; nothing to wake up for
    end = _ends(pick)
    window = timedelta(hours=cfg["urgent_hours"])
    return bool(pick.get("is_auction") and end and end - now <= window)


def quiet_night(now: datetime) -> tuple[str, datetime] | None:
    """During quiet hours: (the date the night began, when it ends). None by day.

    The date names the night, so 11:30pm and 2am of the same night give the
    same one and the bedtime bundle goes out once.
    """
    if not is_quiet(now):
        return None
    cfg = settings.load()
    start = settings.parse_time(cfg["quiet_start"])
    end = settings.parse_time(cfg["quiet_end"])
    began = now.date()
    if start > end and now.time() < end:   # past midnight: the night began yesterday
        began -= timedelta(days=1)
    ends = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if ends <= now:
        ends += timedelta(days=1)
    return began.isoformat(), ends


def bedtime_due(con, now: datetime) -> bool:
    """Is this the first run of tonight's quiet hours, with the bundle switched on?"""
    night = quiet_night(now)
    return bool(night and settings.load(con)["bedtime_bundle"]
                and db.kv_get(con, BEDTIME_KEY) != night[0])


def split_for_bedtime(held: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    """Return (snipes to enter before bed, still held until morning).

    Before bed means every auction ending during quiet hours, and those ending
    in the first couple of hours after, before the morning alert is likely read.
    """
    night = quiet_night(now)
    if night is None:
        return [], held
    until = night[1] + timedelta(hours=BEDTIME_AFTER)
    tonight = [p for p in held if p.get("is_auction") and _ends(p) and _ends(p) <= until]
    return (sorted(tonight, key=lambda p: _ends(p)),
            [p for p in held if p not in tonight])


def format_bedtime(picks: list[dict]) -> tuple[str, str, str | None, str | None]:
    """One alert with every snipe to enter tonight, soonest first."""
    title = f"{profile.get().app_name}: enter before bed ({len(flat(picks))})"
    return (title, "\n\n".join(format_pick(p) for p in picks),
            picks[0].get("url"), picks[0].get("image"))


def worth_alerting(pick: dict) -> bool:
    cfg = settings.load()
    tier = pick.get("tier")
    if pick.get("new_seller") and pick.get("capped_from"):
        tier = pick["capped_from"]   # shown as yellow, alerted with a new-seller warning
    if tier == "drop":
        return True
    if pick.get("unverified"):
        return False   # a dear Buy It Now priced from a guess: the dashboard shows it
    if pick.get("scam_risk") and not (pick.get("comps_n") and not pick.get("new_seller")):
        return False   # too good from a new seller: only real sales and a small total may alert
    if pick.get("deal") is not None and pick["deal"] < cfg["alert_min_stars"]:
        return False   # not a good enough price; picks with no rating still pass
    if tier in ("fire", "green"):
        return cfg[f"alert_{tier}"]
    # Unknown shipping can't count as "$2 or less".
    return (tier == "yellow" and cfg["alert_cheap_yellow"]
            and pick.get("shipping") is not None
            and pick.get("total", 99) <= cfg["yellow_max_total"])


def split_for_quiet_hours(picks: list[dict], now: datetime | None = None
                          ) -> tuple[list[dict], list[dict]]:
    """Return (send now, hold until morning). Expired auctions are dropped."""
    now = now or datetime.now(config.TZ)
    live = [p for p in picks
            if not (p.get("is_auction") and _ends(p) and _ends(p) <= now)]
    if not is_quiet(now):
        return live, []
    return ([p for p in live if is_urgent(p, now)],
            [p for p in live if not is_urgent(p, now)])


# Why a pick is more than a fair price (score.EDGES), as the phone shows it.
EDGE_LABEL = {"mispriced": "💎 PRICE MISTAKE", "hidden": "🕵️ SLIPPED THROUGH",
              "sleeper": "😴 SLEEPER AUCTION", "mislabeled": "🏷️ MISLABELED"}


GUESS = " (guess, no sales found)"


def _guess(pick: dict) -> str:
    return GUESS if pick.get("max_basis", "estimate") == "estimate" else ""


def deal_line(pick: dict) -> str:
    """★★★★☆ 42% under $25 value. For an auction that is at the max, not today's bid."""
    stars = "★" * pick["deal"] + "☆" * (5 - pick["deal"])
    worth = f"${pick['value']:.0f}" if pick["value"] >= 10 else f"${pick['value']:.2f}"
    if not pick.get("deal_pct"):
        return f"Deal: {stars} about its {worth} value" + _guess(pick)
    return (f"Deal: {stars} {pick['deal_pct']}% under {worth} value"
            + (" at max" if pick.get("is_auction") else "") + _guess(pick))


def seller_line(pick: dict) -> str:
    """Seller: cardshop · 1,204 feedback, 99.8%. Flags a new seller on a dear card."""
    if not pick.get("seller"):
        return ""
    line = f"Seller: {pick['seller']}"
    if pick.get("seller_feedback") is not None:
        line += f" · {pick['seller_feedback']:,} feedback"
        if pick.get("seller_pct") is not None:
            line += f", {pick['seller_pct']:g}%"
    if pick.get("new_seller"):
        line = f"⚠️ new seller ({pick['seller_feedback']} feedback) · " + line
    return line


def _price_line(pick: dict) -> str:
    """$12.50 + $1.25 ship · ends Mon 9:14 PM EDT"""
    ship = pick.get("shipping")
    ship_txt = "ship ?" if ship is None else ("free ship" if ship == 0 else f"${ship:.2f} ship")
    line = f"${pick['price']:.2f} + {ship_txt}"
    if pick.get("combined_ship"):
        line += " (combined ship w/ open order)"
    end = _ends(pick)
    if end and pick.get("is_auction"):
        line += f" · ends {ends_text(end)}"
    elif not pick.get("is_auction"):
        line += " · Buy It Now" + ("/Offer" if pick.get("best_offer") else "")
    return line


def ends_text(end: datetime, zones: bool = True) -> str:
    """Mon 9:14 PM EDT / 6:14 PM PDT, or with zones=False, Mon 9:14 PM ET / 6:14 PM PT."""
    et, pt = end.astimezone(ET), end.astimezone(PT)
    if zones:
        return f"{et:%a %-I:%M %p %Z} / {pt:%-I:%M %p %Z}"
    return f"{et:%a %-I:%M %p} ET / {pt:%-I:%M %p} PT"


def bin_line(pick: dict) -> str:
    """BIN ok ≤ $12.12 · offer idea: $10.10. The offer is a number to type yourself."""
    most = pick.get("max")
    if pick.get("is_auction") and pick.get("bin_price") and most is not None:
        ok = float(pick["bin_price"]) <= most * 1.1
        return (f"Or Buy It Now ${float(pick['bin_price']):.2f}"
                + (" (about the max: buy it, the first bid removes that option)" if ok
                   else " (over the max; the first bid removes it)"))
    if pick.get("is_auction") or most is None or pick.get("tier") == "drop":
        return ""
    line = f"BIN ok ≤ ${most:.2f}"
    if pick.get("best_offer"):
        line += f" · offer idea: ${pricing.offer_idea(most):.2f} (you'd send it, not the bot)"
    return line


# Warning flags as the phone shows them, worst first.
WATCH = (("fake_auto", filters.FAKE_AUTO_WHY), ("scam_risk", "too good for a new seller"),
         ("new_seller", "new seller"), ("thin_listing", "thin listing"),
         ("unverified", "price is a guess"))


def watch_outs(pick: dict) -> str:
    """Watch-outs: printed signature, not an auto · new seller (12 feedback). Or "none"."""
    found = []
    flags = set(pick.get("flags") or [])
    for key, text in WATCH:
        if pick.get(key) or key in flags:
            if key == "new_seller" and pick.get("seller_feedback") is not None:
                text += f" ({pick['seller_feedback']} feedback)"
            found.append(text)
    for s in (pick.get("midtier") or {}).get("signals", []):
        if not s.get("positive") and s.get("name") not in ("facsimile_guard",
                                                           "too_good_new_seller"):
            found.append(f"{s['name']} ({s['why']})")
    if any(s.get("name") == "facsimile_guard" and s.get("why") and "fake_auto" not in flags
           for s in (pick.get("midtier") or {}).get("signals", [])):
        found.append(next(s["why"] for s in pick["midtier"]["signals"]
                          if s.get("name") == "facsimile_guard"))
    return "Watch-outs: " + (", ".join(dict.fromkeys(found)) or "none")


def members(entry: dict) -> list[dict]:
    """The picks in one alert entry: the pick itself, then other listings of the same card."""
    return [{k: v for k, v in entry.items() if k != "others"}] + entry.get("others", [])


def flat(entries: list[dict]) -> list[dict]:
    return [pick for entry in entries for pick in members(entry)]


def group_same(picks: list[dict]) -> list[dict]:
    """Put listings of the same card from several sellers into one entry.

    The best-ranked listing leads and carries the rest under "others", so a
    group takes one place in a message. Only picks that kept their listing
    title can be compared; a limited drop is never grouped.
    """
    entries: list[dict] = []
    for pick in sorted(picks, key=_rank):
        title = pick.get("title") if pick.get("tier") != "drop" else None
        lead = next((e for e in entries if title and e.get("title") and e.get("tier") != "drop"
                     and sales._same(title, e["title"])), None)
        if lead is None:
            entries.append(dict(pick))
        else:
            lead.setdefault("others", []).append(pick)
    return entries


def format_group(entry: dict) -> str:
    """One card listed by several sellers: the card, then a line for each listing."""
    listings = sorted(members(entry), key=lambda p: p.get("end_time") or "9")
    icon = TIER_ICON.get(entry.get("tier"), "•")
    lines = [f"{icon} {_who(entry)}{entry['card']} ({len(listings)} listings)"]
    if entry.get("edge") in EDGE_LABEL:
        lines.insert(0, EDGE_LABEL[entry["edge"]])
    for p in listings:
        lines.append(_price_line(p) + (f" · max {p['max']:.2f}" if p.get("max") is not None
                                       else "")
                     + (f" · ⚠️ new seller ({p['seller_feedback']} feedback)"
                        if p.get("new_seller") else "") + f" · {p['item_id']}")
    if entry.get("deal"):
        lines.append(deal_line(entry))
    if entry.get("why"):
        lines.append(entry["why"])
    if entry.get("tier") != "drop":
        lines.append(watch_outs({**entry, **{k: any(p.get(k) for p in listings)
                                            for k, _ in WATCH}}))
    if sum(bool(p.get("is_auction")) for p in listings) > 1:
        lines.append(GROUP_TIP)
    return "\n".join(lines)


def format_pick(pick: dict) -> str:
    """A few short lines, with the item number alone on the last one for copying."""
    if pick.get("others"):
        return format_group(pick)
    icon = TIER_ICON.get(pick.get("tier"), "•")
    lines = [f"{icon} {_who(pick)}{pick['card']}", _price_line(pick)]
    if pick.get("edge") in EDGE_LABEL:
        lines.insert(0, EDGE_LABEL[pick["edge"]])
    if pick.get("max") is not None:
        basis = pick.get("max_basis", "estimate")
        lines.append(f"Max: {pick['max']:.2f}" + (_guess(pick) or f" ({basis})"))
    if bin_line(pick):
        lines.append(bin_line(pick))
    if pick.get("deal"):
        lines.append(deal_line(pick))
    if pick.get("why"):
        lines.append(pick["why"])
    if seller_line(pick):
        lines.append(seller_line(pick))
    if closes.line(pick):
        lines.append(closes.line(pick))
    if pick.get("tier") != "drop":
        lines.append(watch_outs(pick))
    lines.append(str(pick["item_id"]))
    return "\n".join(lines)


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_midtier(pick: dict) -> str:
    """The mid-tier lane's block: the item number alone on line 2, for pasting into Gixen."""
    lane = pick.get("midtier") or {}
    stars = "★" * (pick.get("deal") or 0)
    n = lane.get("comps_n") or 0
    basis = (f"from {n} sold comp{'s' if n != 1 else ''}" if n
             else "estimate, no sold comps")
    value = f"value ~${lane.get('value') or pick.get('value') or 0:.0f} {basis}; " \
            f"ship ${lane.get('ship', 0):.2f}"
    if pick.get("is_auction"):
        top = f"Max: {pick['max']:.2f}  ({value})"
        end = _ends(pick)
        when = f"Ends: {ends_text(end, zones=False)}" if end else "Ends: ?"
    else:
        top = f"BIN ok ≤ ${lane.get('bin_ok', pick.get('max') or 0):.2f}  ({value})"
        ship = pick.get("shipping")
        when = (f"BIN ${pick['price']:.2f} + "
                + ("ship ?" if ship is None else f"${ship:.2f} ship"))
        if lane.get("offer_idea"):
            when += f"  · offer idea: ${lane['offer_idea']:.2f} (text only; nothing is sent)"
    why = [f"{s['name']}({s['why']})" for s in lane.get("signals", []) if s.get("positive")]
    return "\n".join([
        f"🎯 MIDTIER {stars} {_short(pick.get('card') or pick.get('title'))}",
        str(pick["item_id"]), top, when,
        "Why: " + (", ".join(why) or "-"),
        watch_outs(pick),
        f"https://www.ebay.com/itm/{pick['item_id']}"])


def _who(pick: dict) -> str:
    """Team mode: the player, when the card name doesn't already say."""
    name = pick.get("player")
    if not name or name == "unknown" or name.lower() in str(pick.get("card", "")).lower():
        return ""
    return f"{name} · "


def _rank(pick: dict) -> tuple:
    """Cards that slipped through the cracks lead a bundle, then tier, then what ends first.
    A team bot leads with the deepest discount instead: steals, then the rest."""
    if profile.get().mode == "team":
        return (pick.get("edge") not in EDGE_LABEL, -(pick.get("deal_pct") or 0),
                TIER_ORDER.get(pick.get("tier"), 9), pick.get("end_time") or "9")
    return (pick.get("edge") not in EDGE_LABEL, TIER_ORDER.get(pick.get("tier"), 9),
            pick.get("end_time") or "9")


def format_bundle(picks: list[dict], part: str = ""
                  ) -> tuple[str, str, str | None, str | None]:
    picks = sorted(picks, key=_rank)
    n = len(picks)
    who = profile.get()
    kind = f"{who.last} steal" if who.mode == "team" else "steal"   # "3 Cardinals steals"
    title = f"{who.app_name}: {n} {kind}{'s' if n != 1 else ''}{part}"
    body = "\n\n".join(format_pick(p) for p in picks)
    top = picks[0]
    return title, body, top.get("url"), top.get("image")


def plan_messages(picks: list[dict], now: datetime | None = None
                  ) -> tuple[list[list[dict]], list[dict]]:
    """Split picks into phone-sized messages. Returns (messages, overflow).

    Anything urgent goes first, then by tier, so what is left for the next
    run is the least time-sensitive. A group from group_same() counts as one.
    """
    now = now or datetime.now(config.TZ)
    ordered = sorted(picks, key=lambda p: (not is_urgent(p, now), *_rank(p)))
    cut = PER_MESSAGE * MAX_MESSAGES
    first = ordered[:cut]
    return ([first[i:i + PER_MESSAGE] for i in range(0, len(first), PER_MESSAGE)],
            ordered[cut:])


def refresh(con, pick: dict, current: dict, now: datetime) -> dict | None:
    """A pick with today's price, or None when it is no longer worth sending.

    A pick held overnight, or left over from a full run, still carries the
    price it was scored at. `current` is this run's search results by item
    number. An auction bid past its max is dropped, and so is a Buy It Now
    that has been out of the results long enough to have sold.
    """
    if pick.get("tier") == "drop":
        return pick   # a print-to-order drop is not an eBay listing
    item = current.get(pick["item_id"])
    if item is None:
        if pick.get("is_auction"):
            return pick   # not in this search; its end time still rules it out when over
        row = con.execute("SELECT last_seen FROM seen WHERE item_id=?",
                          (pick["item_id"],)).fetchone()
        gone = row is not None and row["last_seen"] < now.timestamp() - BIN_GONE
        return None if gone else pick
    if pick.get("max") is not None and item.price > pick["max"]:
        return None
    return score.rerate(dict(pick, price=item.price, shipping=item.shipping,
                             total=item.total, bids=item.bid_count))


def deliver(con, new_picks: list[dict], dry_run: bool = False,
            now: datetime | None = None, current: dict | None = None) -> int:
    """Bundle new picks plus anything held overnight into one notification.

    With `current` (this run's listings by item number), every candidate is
    brought up to today's price first; see refresh().
    """
    if not settings.load(con)["alerts_enabled"] or not (configured() or dry_run):
        return 0   # paused, or no phone set up; the picks still reach the dashboard
    now = now or datetime.now(config.TZ)
    held = json.loads(db.kv_get(con, PENDING_KEY, "[]"))
    known = {p["item_id"] for p in new_picks}
    candidates = new_picks + [p for p in held if p["item_id"] not in known]
    stale = len(candidates)
    if current is not None:
        candidates = [p for p in (refresh(con, p, current, now) for p in candidates) if p]
    stale -= len(candidates)   # dropped because the price moved on or the listing is gone
    # a card at a sliver of its value skips every alert setting and quiet hours
    jackpots = {p["item_id"] for p in jackpot.alert(con, candidates, now, send=not dry_run)}
    candidates = [p for p in candidates if p["item_id"] not in jackpots and worth_alerting(p)]
    candidates = [p for p in candidates if db.may_alert(con, p["item_id"], p["total"])]
    send_now, hold = split_for_quiet_hours(candidates, now)
    # Once a night, as quiet hours begin: the snipes to enter before going to bed.
    bedtime_night = quiet_night(now)[0] if bedtime_due(con, now) else None
    bedtime, hold = split_for_bedtime(hold, now) if bedtime_night else ([], hold)
    bedtime = sorted(group_same(bedtime), key=lambda p: p.get("end_time") or "9")
    # the same card from several sellers goes out as one entry
    messages, overflow = plan_messages(group_same(send_now), now)
    hold = hold + flat(overflow)
    outbox = [(*format_bedtime(bedtime), bedtime)] if bedtime else []
    for n, chunk in enumerate(messages, 1):
        part = f" ({n}/{len(messages)})" if len(messages) > 1 else ""
        outbox.append((*format_bundle(chunk, part), chunk))
    sent = done = 0
    try:
        for title, body, url, image, chunk in outbox:
            if dry_run:
                print(f"--- would send ---\n{title}\n{body}\nurl={url}\n", file=sys.stderr)
            else:
                send(title, body, url, image)
                for p in flat(chunk):
                    db.mark_alerted(con, p["item_id"], p["total"])
            done += 1
            sent += len(flat(chunk))
    except Exception:
        # Keep what didn't go out so the next run tries again.
        hold = hold + [p for *_, chunk in outbox[done:] for p in flat(chunk)]
        raise
    finally:
        if not dry_run:
            db.kv_set(con, PENDING_KEY, json.dumps(hold))
            if bedtime_night and (done or not bedtime):   # tonight's bundle is dealt with
                db.kv_set(con, BEDTIME_KEY, bedtime_night)
            if bedtime and done:
                db.bump(con, "bedtime_bundles")
                db.bump(con, "bedtime_picks", len(flat(bedtime)))
            # counts for the weekly self-review
            db.bump(con, "alerts_sent", sent)
            db.bump(con, "alerts_held", len(hold))
            db.bump(con, "alerts_stale", stale)
            con.commit()
    return sent


def send_test(origin: str = "the command line") -> None:
    who = profile.get()
    stamp = datetime.now(config.TZ).strftime("%a %-I:%M %p %Z")
    send(f"{who.app_name}: test",
         f"Test alert sent from {origin} at {stamp}. If you can read this, alerts "
         "are wired up.\n\nMax: 3.03 (sample)\n123456789012",
         url="https://www.ebay.com/sch/i.html?_nkw=" + who.search_name.replace(" ", "+"))


def main() -> None:
    argparse.ArgumentParser(description="Send a test notification").parse_args()
    send_test()
    print("sent")


if __name__ == "__main__":
    main()
