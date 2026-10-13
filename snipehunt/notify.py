"""Phone alerts through Home Assistant's notify service, or an ntfy topic."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

import requests

from . import config, db, profile, settings

TIER_ICON = {"fire": "🔥", "green": "🟢", "yellow": "🟡", "drop": "⏰"}
TIER_ORDER = {"drop": 0, "fire": 1, "green": 2, "yellow": 3}
# Quiet hours, the urgent window and which tiers alert are in settings.py.
PER_MESSAGE = 5                  # picks per notification, so a phone shows them all
MAX_MESSAGES = 3                 # per run; the rest go out on the next run
PENDING_KEY = "pending_picks"


def configured() -> bool:
    """Is there anywhere to send an alert?"""
    ha = all(config.get(k) for k in ("HA_URL", "HA_TOKEN", "HA_NOTIFY_SERVICE"))
    return ha or bool(config.get("NTFY_URL"))


def send(title: str, message: str, url: str | None = None,
         image: str | None = None) -> None:
    """Send to Home Assistant if it is set up, and to ntfy if `NTFY_URL` is set."""
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
    end = _ends(pick)
    window = timedelta(hours=settings.load()["urgent_hours"])
    return bool(pick.get("is_auction") and end and end - now <= window)


def worth_alerting(pick: dict) -> bool:
    cfg = settings.load()
    tier = pick.get("tier")
    if tier == "drop":
        return True
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
              "sleeper": "😴 SLEEPER AUCTION"}


def deal_line(pick: dict) -> str:
    """★★★★☆ 42% under $25 value. For an auction that is at the max, not today's bid."""
    stars = "★" * pick["deal"] + "☆" * (5 - pick["deal"])
    worth = f"${pick['value']:.0f}" if pick["value"] >= 10 else f"${pick['value']:.2f}"
    if not pick.get("deal_pct"):
        return f"Deal: {stars} about its {worth} value"
    return (f"Deal: {stars} {pick['deal_pct']}% under {worth} value"
            + (" at max" if pick.get("is_auction") else ""))


def format_pick(pick: dict) -> str:
    """Four short lines, with the item number alone on the last one for copying."""
    icon = TIER_ICON.get(pick.get("tier"), "•")
    ship = pick.get("shipping")
    ship_txt = "ship ?" if ship is None else ("free ship" if ship == 0 else f"${ship:.2f} ship")
    line2 = f"${pick['price']:.2f} + {ship_txt}"
    end = _ends(pick)
    if end and pick.get("is_auction"):
        line2 += f" · ends {config.local(end).strftime('%a %-I:%M %p %Z')}"
    elif not pick.get("is_auction"):
        line2 += " · Buy It Now" + ("/Offer" if pick.get("best_offer") else "")
    lines = [f"{icon} {pick['card']}", line2]
    if pick.get("edge") in EDGE_LABEL:
        lines.insert(0, EDGE_LABEL[pick["edge"]])
    if pick.get("max") is not None:
        lines.append(f"Max: {pick['max']:.2f} ({pick.get('max_basis', 'estimate')})")
    if pick.get("deal"):
        lines.append(deal_line(pick))
    if pick.get("why"):
        lines.append(pick["why"])
    lines.append(str(pick["item_id"]))
    return "\n".join(lines)


def _rank(pick: dict) -> tuple:
    """Cards that slipped through the cracks lead a bundle, then tier, then what ends first."""
    return (pick.get("edge") not in EDGE_LABEL, TIER_ORDER.get(pick.get("tier"), 9),
            pick.get("end_time") or "9")


def format_bundle(picks: list[dict], part: str = ""
                  ) -> tuple[str, str, str | None, str | None]:
    picks = sorted(picks, key=_rank)
    n = len(picks)
    title = f"{profile.get().app_name}: {n} steal{'s' if n != 1 else ''}{part}"
    body = "\n\n".join(format_pick(p) for p in picks)
    top = picks[0]
    return title, body, top.get("url"), top.get("image")


def plan_messages(picks: list[dict], now: datetime | None = None
                  ) -> tuple[list[list[dict]], list[dict]]:
    """Split picks into phone-sized messages. Returns (messages, overflow).

    Anything urgent goes first, then by tier, so what is left for the next
    run is the least time-sensitive.
    """
    now = now or datetime.now(config.TZ)
    ordered = sorted(picks, key=lambda p: (not is_urgent(p, now), *_rank(p)))
    cut = PER_MESSAGE * MAX_MESSAGES
    first = ordered[:cut]
    return ([first[i:i + PER_MESSAGE] for i in range(0, len(first), PER_MESSAGE)],
            ordered[cut:])


def deliver(con, new_picks: list[dict], dry_run: bool = False,
            now: datetime | None = None) -> int:
    """Bundle new picks plus anything held overnight into one notification."""
    if not settings.load(con)["alerts_enabled"] or not (configured() or dry_run):
        return 0   # paused, or no phone set up; the picks still reach the dashboard
    held = json.loads(db.kv_get(con, PENDING_KEY, "[]"))
    known = {p["item_id"] for p in new_picks}
    candidates = [p for p in new_picks if worth_alerting(p)]
    candidates += [p for p in held if p["item_id"] not in known]
    candidates = [p for p in candidates if db.may_alert(con, p["item_id"], p["total"])]
    send_now, hold = split_for_quiet_hours(candidates, now)
    messages, overflow = plan_messages(send_now, now)
    hold = hold + overflow
    sent = 0
    try:
        for n, chunk in enumerate(messages, 1):
            part = f" ({n}/{len(messages)})" if len(messages) > 1 else ""
            title, body, url, image = format_bundle(chunk, part)
            if dry_run:
                print(f"--- would send ---\n{title}\n{body}\nurl={url}\n", file=sys.stderr)
            else:
                send(title, body, url, image)
                for p in chunk:
                    db.mark_alerted(con, p["item_id"], p["total"])
            sent += len(chunk)
    except Exception:
        # Keep what didn't go out so the next run tries again.
        hold = hold + [p for chunk in messages for p in chunk][sent:]
        raise
    finally:
        if not dry_run:
            db.kv_set(con, PENDING_KEY, json.dumps(hold))
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
