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

from . import config, db

KEY = "settings"
MAX_LIST = 12   # each extra search costs eBay API calls every hour

DEFAULTS: dict = {
    "alerts_enabled": True,          # master switch for phone alerts
    "alert_fire": True,
    "alert_green": True,
    "alert_cheap_yellow": True,
    "yellow_max_total": 2.00,        # a yellow pick alerts at this total or less
    "alert_min_stars": 1,            # rated picks alert only at this many deal stars or more
    "quiet_hours": True,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "urgent_hours": 2.0,             # auctions ending this soon ignore quiet hours
    "need_snipe_hours": 24,          # "needs a snipe" looks this far ahead
    "monthly_budget": 0.0,
    "gixen_enabled": True,
    "gixen_relogin_minutes": 120,    # after being signed out, leave your own session alone
    "sellers": [],                   # eBay sellers whose listings get their own search
    "extra_searches": [],
}

RANGES = {
    "yellow_max_total": (0, 25),
    "urgent_hours": (0, 24),
    "alert_min_stars": (1, 5),
    "need_snipe_hours": (1, 168),
    "monthly_budget": (0, 5000),
    "gixen_relogin_minutes": (0, 1440),
}
ITEM = {
    "sellers": re.compile(r"[A-Za-z0-9._*\-]{1,64}"),
    "extra_searches": re.compile(r"[A-Za-z0-9 ./#'&+\-]{2,80}"),
}


class SettingsError(ValueError):
    pass


def _clean(key: str, value):
    """Return `value` in the stored form for `key`, or say what is wrong with it."""
    if key not in DEFAULTS:
        raise SettingsError(f"unknown setting {key!r}")
    default = DEFAULTS[key]
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
    return [k for k in DEFAULTS if not only or k in only]


def parse_time(text: str) -> clock:
    hour, minute = text.split(":")[:2]
    return clock(int(hour), int(minute))


def load(con=None) -> dict:
    """Every setting, with defaults for anything unset or no longer valid."""
    values = dict(DEFAULTS)
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


def update(con, changes: dict) -> dict:
    """Validate every change, then save them all. Nothing is saved if one is bad."""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("no settings given")
    hidden = [key for key in changes if key in DEFAULTS and key not in visible()]
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
