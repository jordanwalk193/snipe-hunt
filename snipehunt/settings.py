"""Settings you can change from Home Assistant.

The Home Assistant integration shows each of these as a switch, number, time or
text box and sends changes to api.py. They are kept in the state database
next to everything else the bot remembers, so the hourly job, the alerts and
the dashboard all read the same values. Secrets are not settings: those stay
in the environment or the env file.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import time as clock

from . import config, db, profile

KEY = "settings"
MAX_LIST = 12   # each extra search costs eBay API calls every hour

DEFAULTS: dict = {
    "alerts_enabled": True,          # master switch for phone alerts
    # a $20+ card at a sliver of its value (jackpot.py): its own loud alert, even at night
    "jackpot_alerts": True,
    "alert_fire": True,
    "alert_green": True,
    "alert_cheap_yellow": True,
    "yellow_max_total": 2.00,        # a yellow pick alerts at this total or less
    "alert_min_stars": 1,            # rated picks alert only at this many deal stars or more
    "price_lookups": 8,              # sold-price web lookups per scoring call; 0 = none
    # a Buy It Now over this total whose value is only a guess is shown, never alerted
    "estimate_bin_cap": 25.0,
    # a seller under this many feedback can't make a pick over SELLER_TRUST_OVER better than yellow
    "min_seller_feedback": 100,
    "quiet_hours": True,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "urgent_hours": 2.0,             # auctions ending this soon ignore quiet hours
    "bedtime_bundle": True,          # one "enter before bed" alert, no overnight wake-ups
    "need_snipe_hours": 24,          # "needs a snipe" looks this far ahead
    "monthly_budget": 0.0,
    "gixen_enabled": True,
    "gixen_relogin_minutes": 120,    # after being signed out, leave your own session alone
    "sellers": [],                   # eBay sellers whose listings get their own search
    "extra_searches": [],
    # sellers who combine shipping: with an order already open, another card ships for $0
    "combined_ship_sellers": [],
    # The four below are tuning knobs more than preferences; see TUNABLES.
    "edge_min_pct": 25,              # an edge has to be this far under the going rate
    "min_overlap": 0.5,              # share of title words a sold price must have in common
    "track_off": 10,                 # percent the going rates must be off before scoring is told
    "fast_min_price": 15.0,          # under this, a plain Buy It Now waits for the hourly run
    # the market sweep (deep.py): pages of standing Buy It Nows read each hourly run,
    # on top of every live auction; 0 turns the sweep off
    "deep_pages": 6,
    # offers and lowball snipes may ask Claude to price up to 20 listings nothing has
    # priced yet, at most twice an hour; each answer is kept 30 days
    "deal_appraisals": True,
    "calibrate": True,               # correct guessed going rates from how past guesses sold
    "self_tuning": True,             # the weekly self-review may adjust one tunable a week
    # eBay Browse calls a day for the whole app (eBay's default allowance); past 80%
    # the least useful lanes stop first (usage.CUT_ORDER)
    "ebay_daily_cap": 5000,
    "aspect_refresh": True,          # one eBay call a week for eBay's item-specifics names
    # Steals mode (steals.py). Off here so player mode is unchanged; a profile with
    # [steals] turns them on (steals.settings_defaults) and Home Assistant may override.
    "max_share": 1.0,                # an auction max or Buy It Now keeps to this share of value
    "bid_now_alerts": False,         # auctions ending within the hour at 10% of value or less
    "drop_alerts": False,            # a Buy It Now just cut into steal range
}

# What the weekly self-review may adjust on its own, and how far (tune.py).
# Each has hard bounds, the most it may move in one week, and the metric from
# reflect.py the change is judged by two weeks later ("better" says which way
# is good). The value itself is an ordinary setting, so nothing here ever
# touches code. Never tunable: anything about the read-only rules, quiet
# hours, which alerts are on, credentials or how the bot signs in to Gixen.
TUNABLES: dict = {
    "edge_min_pct": {"min": 15, "max": 40, "step": 5,
                     "metric": "sniped_pct", "better": "higher"},
    "min_overlap": {"min": 0.4, "max": 0.7, "step": 0.05,
                    "metric": "value_gap_pct", "better": "lower"},
    "track_off": {"min": 5, "max": 20, "step": 5,
                  "metric": "value_gap_pct", "better": "lower"},
    "price_lookups": {"min": 0, "max": 12, "step": 2,
                      "metric": "value_gap_pct", "better": "lower"},
    "fast_min_price": {"min": 5, "max": 30, "step": 5,
                       "metric": "cost_per_pick", "better": "lower"},
    "yellow_max_total": {"min": 1, "max": 4, "step": 0.5,
                         "metric": "skipped_pct", "better": "lower"},
}
NEVER_TUNABLE = ("quiet", "alert", "gixen", "urgent", "bedtime", "self_tuning")

# Team mode only: a team lists far more cards an hour than a player, so what
# reaches Claude is filtered and ranked for free first (shortlist.py). These
# exist only on a team bot; a player bot never sees them.
TEAM_DEFAULTS: dict = {
    "team_claude_per_run": 30,       # listings sent to Claude per hourly run, best-ranked first
    "team_daily_budget": 5.0,        # dollars of Claude a day; at it, scoring waits for tomorrow
    "team_min_price": 3.0,           # a plain Buy It Now under this isn't worth a look
    "team_max_price": 250.0,         # nothing over this, auction or not
    "team_max_run": 99,              # a print run this low or lower is worth a look
    "team_lots": False,              # lots and team sets are skipped unless this is on
}
# Defaults that differ on a team bot: frugal by design, only real steals alert.
TEAM_OVERRIDES: dict = {"price_lookups": 4, "alert_min_stars": 4}
# The mid-tier lane's switches, on a bot whose profile has [search.midtier]. Their
# defaults come from the profile (midtier.py); Home Assistant may override them.
MIDTIER_KEYS = {"midtier_enabled": "enabled", "midtier_deal_ratio": "deal_ratio",
                "midtier_max_pay": "max_pay"}

RANGES = {
    "yellow_max_total": (0, 25),
    "urgent_hours": (0, 24),
    "alert_min_stars": (1, 5),
    "price_lookups": (0, 12),
    "deep_pages": (0, 20),
    "estimate_bin_cap": (0, 1000),
    "min_seller_feedback": (0, 100000),
    "edge_min_pct": (15, 40),
    "min_overlap": (0.4, 0.7),
    "track_off": (5, 20),
    "fast_min_price": (5, 30),
    "need_snipe_hours": (1, 168),
    "monthly_budget": (0, 5000),
    "gixen_relogin_minutes": (0, 1440),
    "team_claude_per_run": (0, 80),
    "team_daily_budget": (0, 500),
    "team_min_price": (0, 100),
    "team_max_price": (1, 10000),
    "team_max_run": (1, 999),
    "ebay_daily_cap": (100, 1000000),
    "midtier_deal_ratio": (0.2, 0.95),
    "midtier_max_pay": (20, 1000),
    "max_share": (0.3, 1.0),
}
ITEM = {
    "sellers": re.compile(r"[A-Za-z0-9._*\-]{1,64}"),
    "combined_ship_sellers": re.compile(r"[A-Za-z0-9._*\-]{1,64}"),
    "extra_searches": re.compile(r"[A-Za-z0-9 ./#'&+\-]{2,80}"),
}


class SettingsError(ValueError):
    pass


def team_mode() -> bool:
    """Is this a team bot? False when there is no profile yet."""
    try:
        return profile.get().mode == "team"
    except (profile.ProfileError, OSError, ValueError):
        return False


def defaults() -> dict:
    """The defaults for this bot: player mode's, or a team's with its own extras."""
    if not team_mode():
        return {**DEFAULTS, **midtier_defaults(), **steals_defaults()}
    return {**DEFAULTS, **TEAM_OVERRIDES, **TEAM_DEFAULTS, **steals_defaults()}


def steals_defaults() -> dict:
    """Steals mode's switches as the profile's [steals] sets them; none without it."""
    try:
        from . import steals   # it reads settings; kept local
        return steals.settings_defaults()
    except (profile.ProfileError, OSError, ValueError):
        return {}


def midtier_defaults() -> dict:
    """The mid-tier switches as the profile sets them; none without [search.midtier]."""
    try:
        from . import midtier   # it reads settings; kept local
        cfg = midtier.profile_config()
    except (profile.ProfileError, OSError, ValueError):
        return {}
    if cfg is None:
        return {}
    return {key: (bool(cfg[name]) if name == "enabled" else float(cfg[name]))
            for key, name in MIDTIER_KEYS.items()}


def _clean(key: str, value):
    """Return `value` in the stored form for `key`, or say what is wrong with it."""
    known = defaults()
    if key not in known:
        raise SettingsError(f"unknown setting {key!r}")
    default = known[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false")
        return value
    if key in RANGES:
        low, high = RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{key} must be a number")
        if not low <= value <= high:
            raise SettingsError(f"{key} must be between {low} and {high}")
        return int(value) if isinstance(default, int) else round(float(value), 2)
    if isinstance(default, list):
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            raise SettingsError(f"{key} must be a list")
        items = list(dict.fromkeys(" ".join(str(v).split()) for v in value))
        items = [i for i in items if i]
        if len(items) > MAX_LIST:
            raise SettingsError(f"{key} takes at most {MAX_LIST} entries")
        bad = [i for i in items if not ITEM[key].fullmatch(i)]
        if bad:
            raise SettingsError(f"{key}: {bad[0]!r} isn't allowed")
        return items
    try:   # what is left are the two clock times
        return parse_time(str(value)).strftime("%H:%M")
    except ValueError:
        raise SettingsError(f"{key} must be a time like 23:00") from None


def visible() -> list[str]:
    """Settings the integration may show and change.

    `API_SETTINGS` (comma-separated) limits them to the ones the running bot
    really obeys; unset means all of them.
    """
    only = [k.strip() for k in (config.get("API_SETTINGS") or "").split(",") if k.strip()]
    return [k for k in defaults() if not only or k in only]


def parse_time(text: str) -> clock:
    hour, minute = text.split(":")[:2]
    return clock(int(hour), int(minute))


def load(con=None) -> dict:
    """Every setting, with defaults for anything unset or no longer valid."""
    values = defaults()
    try:
        stored = json.loads(db.kv_get(con or db.connect(), KEY, "{}"))
    except (sqlite3.Error, OSError, ValueError):
        return values   # a test or a first run with no database yet
    for key, value in stored.items():
        try:
            values[key] = _clean(key, value)
        except SettingsError:
            pass
    return values


def set_value(con, key: str, value) -> None:
    """Store one setting, validated like any other. The caller commits."""
    stored = json.loads(db.kv_get(con, KEY, "{}"))
    stored[key] = _clean(key, value)
    db.kv_set(con, KEY, json.dumps(stored))


def update(con, changes: dict) -> dict:
    """Validate every change, then save them all. Nothing is saved if one is bad."""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("no settings given")
    hidden = [key for key in changes if key in defaults() and key not in visible()]
    if hidden:
        raise SettingsError(f"{hidden[0]} can't be changed from Home Assistant here")
    cleaned = {key: _clean(key, value) for key, value in changes.items()}
    stored = json.loads(db.kv_get(con, KEY, "{}"))
    stored.update(cleaned)
    db.kv_set(con, KEY, json.dumps(stored))
    con.commit()
    return load(con)


if __name__ == "__main__":
    print(json.dumps(load(), indent=1))
