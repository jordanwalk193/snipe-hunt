"""Read the scheduled snipes from Gixen. Read-only, always.

Gixen's API is switched off for this account, so this logs in to the normal
web page and reads the snipe list from it. It only ever logs in and loads that
page. It never adds, changes or removes a snipe: you place every snipe
himself.

Gixen allows one session per account. Logging in here signs your browser
out, and his next browser login signs the bot out. So the bot keeps its session
cookie between runs, and once it finds itself signed out it waits before
logging back in, so it doesn't kick him out while he is setting snipes.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timedelta

import requests

from . import config, db, settings

BASE = "https://www.gixen.com/main/"
SESSION_FILE = "gixen_session.json"
CACHE_KEY = "gixen_cache"
SIGNED_OUT_KEY = "gixen_signed_out_at"
CACHE_SECONDS = 15 * 60

ROW = re.compile(
    r"ViewItem&amp;item=(?P<item>\d{9,14})\">.*?"
    r"<td colspan=2>(?P<title>.*?)\s*<i>\(by <a[^>]*>(?P<seller>[^<]*)</a>.*?"
    r"Time to end:\s*(?P<left>[^<]*)<.*?"
    r"Max bid:\s*(?P<max>[\d.,]+).*?"
    r"Current bid:\s*(?P<bid>[\d.,]*).*?"
    r"Status \(main\):\s*</td>\s*<td>(?P<status>[^<]*)</td>", re.S)
UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class GixenError(RuntimeError):
    pass


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _end_time(left: str, now: datetime) -> str | None:
    """'1 d, 2 h, 4 m' counted from when the page was read; None once ended."""
    parts = re.findall(r"(\d+)\s*([dhms])", left)
    if not parts:
        return None
    seconds = sum(int(n) * UNITS[u] for n, u in parts)
    return (now + timedelta(seconds=seconds)).isoformat(timespec="minutes")


def parse(page: str, now: datetime | None = None) -> list[dict]:
    """Snipes from the logged-in page: scheduled ones and ended ones not yet purged."""
    now = now or datetime.now(config.TZ)
    # The page holds the list twice (phone and desktop layouts); read the first.
    page = page.split('class="main-table-desktop"')[0]
    snipes, seen = [], set()
    for m in ROW.finditer(page):
        if m["item"] in seen:
            continue
        seen.add(m["item"])
        snipes.append({
            "item_id": m["item"], "title": html.unescape(m["title"]).strip(),
            "seller": m["seller"].strip(), "end_time": _end_time(m["left"], now),
            "max_bid": _number(m["max"]), "current_bid": _number(m["bid"]),
            "status": m["status"].strip()})
    return snipes


def _signed_in(page: str) -> bool:
    return 'name="logout"' in page or "main-table-mobile" in page


def _session_path():
    return config.ensure_state_dir() / SESSION_FILE


def _load_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (snipe-hunt; read-only)"
    try:
        for name, value in json.loads(_session_path().read_text()).items():
            session.cookies.set(name, value, domain="www.gixen.com")
    except (OSError, ValueError):
        pass
    return session


def _save_session(session: requests.Session) -> None:
    path = _session_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({c.name: c.value for c in session.cookies}, fh)


def _page(session: requests.Session) -> str:
    resp = session.get(BASE + "home_2.php", timeout=30)
    return resp.text if resp.status_code == 200 else ""


def fetch(con, allow_login: bool = True) -> list[dict]:
    """Read the snipe list, reusing the saved session when it still works."""
    user, password = config.require("GIXEN_USER", "GIXEN_PASS")
    session = _load_session()
    page = _page(session) if session.cookies else ""
    if not _signed_in(page):
        now = time.time()
        signed_out_at = float(db.kv_get(con, SIGNED_OUT_KEY, "0") or 0)
        if session.cookies and not signed_out_at:
            db.bump(con, "gixen_signed_out")
            db.kv_set(con, SIGNED_OUT_KEY, str(now))
            con.commit()
            signed_out_at = now
        # after being signed out, leave your own session alone for a while
        wait = (signed_out_at + 60 * settings.load(con)["gixen_relogin_minutes"]
                - now)
        if not allow_login or wait > 0:
            raise GixenError("Signed out by another Gixen login; the bot signs back in "
                             f"in about {max(1, round(wait / 60))} min")
        session.cookies.clear()
        session.post(BASE + "home_1.php", timeout=30, data={
            "username": user, "password": password,
            "signin": "signin", "Submit": "Log in Now"})
        page = _page(session)
        if not _signed_in(page):
            raise GixenError("Gixen did not accept the login")
        _save_session(session)
    if db.kv_get(con, SIGNED_OUT_KEY, "0") != "0":
        db.kv_set(con, SIGNED_OUT_KEY, "0")
    return parse(page)


def snipes(con) -> tuple[list[dict], str | None, float]:
    """Return (snipes, problem, read at). Cached so the 5-minute push is gentle.

    When Gixen can't be read, the last good list is kept and the problem says why.
    """
    cache = json.loads(db.kv_get(con, CACHE_KEY, "{}"))
    if time.time() - cache.get("checked", 0) < CACHE_SECONDS:
        return cache.get("snipes", []), cache.get("error"), cache.get("at", 0)
    try:
        cache.update(snipes=fetch(con), error=None, at=time.time())
    except (GixenError, config.ConfigError) as exc:
        cache["error"] = str(exc)
    except requests.RequestException as exc:
        cache["error"] = f"Gixen unreachable ({type(exc).__name__})"
    cache["checked"] = time.time()
    db.kv_set(con, CACHE_KEY, json.dumps(cache))
    con.commit()
    return cache.get("snipes", []), cache.get("error"), cache.get("at", 0)


if __name__ == "__main__":
    for s in fetch(db.connect()):
        print(s)
