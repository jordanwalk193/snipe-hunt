"""Is the bot's Claude login about to run out?

The Claude CLI signs in as a person, and that login lapses after a few weeks.
When it does, every job fails until someone signs in again, and one failure
alert is easy to miss. So the daily job warns the phone once a day from
WARN_DAYS before the login ends, and the hourly job keeps the latest reading
for the dashboard. A long-lived token (`claude setup-token`, then
CLAUDE_CODE_OAUTH_TOKEN in the env file) never lapses, and an API key
(ANTHROPIC_API_KEY) is no login at all: with either set there is nothing to check.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

from . import config, db, notify, profile, score

WARN_DAYS = 5              # start warning this many days before the login ends
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CACHE_KEY = "claude_login"          # the last status read, for the dashboard
WARNED_KEY = "claude_login_warned"  # the day of the last warning: one a day
FIX = ("Sign in again on the bot's machine: `claude auth login`. To stop this "
       "happening: `claude setup-token`, then put CLAUDE_CODE_OAUTH_TOKEN=... "
       "in the env file.")


def status(now: datetime | None = None) -> dict:
    """The login as the CLI reports it: {"logged_in", "expires", "days_left", "token"},
    with "error" when the CLI couldn't be asked. Never raises."""
    now = now or datetime.now(config.TZ)
    checked = now.isoformat(timespec="seconds")
    if config.get(TOKEN_ENV) or config.get("ANTHROPIC_API_KEY"):
        return {"logged_in": True, "token": True, "expires": None, "days_left": None,
                "checked": checked}
    out = {"logged_in": None, "token": False, "expires": None, "days_left": None,
           "checked": checked}
    try:
        data = score.login_status()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        out["error"] = type(exc).__name__   # never the CLI's own words: they may name the account
        return out
    out["logged_in"] = bool(data.get("loggedIn"))
    expires = data.get("loginExpiresAt")
    if expires:
        try:
            ends = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            out["expires"] = ends.isoformat(timespec="seconds")
            out["days_left"] = round((ends - now).total_seconds() / 86400, 1)
        except ValueError:
            pass
    return out


def refresh(con, now: datetime | None = None) -> dict:
    """Read the login and keep it for the dashboard (snapshot.status). The caller commits."""
    out = status(now)
    db.kv_set(con, CACHE_KEY, json.dumps(out))
    return out


def cached(con) -> dict | None:
    """The last status read, or None before the first."""
    raw = db.kv_get(con, CACHE_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def describe(st: dict) -> str:
    if st.get("token"):
        return "long-lived token or API key"
    if st.get("logged_in") is None:
        return f"could not check: {st.get('error') or 'no answer'}"
    if not st["logged_in"]:
        return "not signed in"
    if st["days_left"] is None:
        return "signed in"
    return f"signed in, {st['days_left']:.0f} days left"


def check(con, now: datetime | None = None, dry_run: bool = False) -> str:
    """Daily: one phone alert a day while the login is missing or ends within
    WARN_DAYS, with what to do about it. Returns a line for the job summary."""
    now = now or datetime.now(config.TZ)
    st = refresh(con, now)
    worry = (st["logged_in"] is False
             or (st["days_left"] is not None and st["days_left"] <= WARN_DAYS))
    if (worry and not dry_run and notify.configured()
            and db.kv_get(con, WARNED_KEY) != now.date().isoformat()):
        app = profile.get().app_name
        title = (f"{app}: Claude login has lapsed, nothing is being scored"
                 if st["logged_in"] is False else
                 f"{app}: Claude login ends in {max(st['days_left'], 0):.0f} day(s)")
        try:
            notify.send(title, FIX)
            db.kv_set(con, WARNED_KEY, now.date().isoformat())
        except Exception as exc:   # best effort: the summary line still says so
            print(f"authcheck: could not send the warning: {exc}", file=sys.stderr)
    con.commit()
    return describe(st)
