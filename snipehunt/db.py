"""SQLite state: which listings have been seen, scored and alerted."""
from __future__ import annotations

import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    item_id        TEXT PRIMARY KEY,
    title          TEXT,
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    scored_total   REAL,
    scored_at      INTEGER,
    tier           TEXT,
    alerted_total  REAL,
    alerted_at     INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sales (
    item_id   TEXT PRIMARY KEY,
    title     TEXT,
    price     REAL NOT NULL,
    shipping  REAL,
    bids      INTEGER,
    sold_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    item_id     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    end_time    TEXT,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_events (
    date       TEXT,
    type       TEXT,
    item_id    TEXT,
    title      TEXT,
    data       TEXT NOT NULL,
    logged_at  INTEGER NOT NULL,
    UNIQUE (date, type, item_id, title)
);
CREATE TABLE IF NOT EXISTS snipe_log (
    item_id    TEXT PRIMARY KEY,
    title      TEXT,
    my_max     REAL,
    bot_max    REAL,
    bot_value  REAL,
    logged_at  INTEGER NOT NULL
);
"""

PASSED_KEY = "passed_picks"   # what was hidden with the Skip button, to learn taste from
MAX_PASSED = 300
PRICE_DROP = 0.20  # re-score / re-alert only when the price falls this much
# Added to `seen` after the first release, so sales.py can look up closed auctions.
# `price` and `seller` came later still, for price drops and relists (watch.py).
# `checked_at` is when sales.py last asked eBay about a Buy It Now that was still for sale.
SEEN_ADDED = {"is_auction": "INTEGER", "end_ts": "INTEGER", "shipping": "REAL",
              "closed_at": "INTEGER", "price": "REAL", "seller": "TEXT",
              "checked_at": "INTEGER"}
# Added to `sales`: the photo, then whether it was an auction or a fixed-price sale.
SALES_ADDED = {"image": "TEXT", "kind": "TEXT DEFAULT 'auction'"}


def connect() -> sqlite3.Connection:
    config.ensure_state_dir()
    # The API reads while a job writes; wait for the other side instead of failing.
    con = sqlite3.connect(config.SEEN_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {row["name"] for row in con.execute("PRAGMA table_info(seen)")}
    for name, kind in SEEN_ADDED.items():
        if name not in have:
            con.execute(f"ALTER TABLE seen ADD COLUMN {name} {kind}")
    have = {row["name"] for row in con.execute("PRAGMA table_info(sales)")}
    for name, kind in SALES_ADDED.items():
        if name not in have:
            con.execute(f"ALTER TABLE sales ADD COLUMN {name} {kind}")
    return con


def _dropped(old: float | None, new: float) -> bool:
    return old is not None and old > 0 and new <= old * (1 - PRICE_DROP)


def touch(con, item_id: str, title: str, is_auction: bool | None = None,
          end_ts: float | None = None, shipping: float | None = None,
          price: float | None = None, seller: str | None = None) -> None:
    now = int(time.time())
    con.execute(
        "INSERT INTO seen (item_id, title, first_seen, last_seen, is_auction, end_ts, shipping,"
        " price, seller) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "is_auction=excluded.is_auction, end_ts=excluded.end_ts, shipping=excluded.shipping, "
        "price=excluded.price, seller=excluded.seller",
        (item_id, title, now, now, None if is_auction is None else int(is_auction),
         None if end_ts is None else int(end_ts), shipping, price, seller),
    )


def note_snipe(con, item_id: str, title: str, end_ts: float | None,
               seller: str | None = None) -> None:
    """Remember an auction from the Gixen list so its final price gets looked up.

    The search may never have returned it. Only gaps are filled: whatever a
    search already recorded about the listing (title, end, seller) is better
    than Gixen's copy and is left alone, and so is when it was last seen.
    """
    now = int(time.time())
    con.execute(
        "INSERT INTO seen (item_id, title, first_seen, last_seen, is_auction, end_ts, seller)"
        " VALUES (?,?,?,?,1,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET is_auction=1, "
        "title=COALESCE(NULLIF(seen.title, ''), excluded.title), "
        "end_ts=COALESCE(seen.end_ts, excluded.end_ts), "
        "seller=COALESCE(seen.seller, excluded.seller)",
        (item_id, title, now, now, None if end_ts is None else int(end_ts), seller or None),
    )


def needs_scoring(con, item_id: str, total: float) -> bool:
    row = con.execute(
        "SELECT scored_total FROM seen WHERE item_id=?", (item_id,)
    ).fetchone()
    if row is None or row["scored_total"] is None:
        return True
    return _dropped(row["scored_total"], total)


def mark_scored(con, item_id: str, total: float, tier: str | None) -> None:
    con.execute(
        "UPDATE seen SET scored_total=?, scored_at=?, tier=? WHERE item_id=?",
        (total, int(time.time()), tier, item_id),
    )


def may_alert(con, item_id: str, total: float) -> bool:
    row = con.execute(
        "SELECT alerted_total FROM seen WHERE item_id=?", (item_id,)
    ).fetchone()
    if row is None or row["alerted_total"] is None:
        return True
    return _dropped(row["alerted_total"], total)


def mark_alerted(con, item_id: str, total: float) -> None:
    con.execute(
        "UPDATE seen SET alerted_total=?, alerted_at=? WHERE item_id=?",
        (total, int(time.time()), item_id),
    )


def save_pick(con, pick: dict) -> None:
    """Keep the latest recommendation per listing for the weekly report."""
    con.execute(
        "INSERT INTO picks (item_id, data, end_time, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET data=excluded.data, "
        "end_time=excluded.end_time, created_at=excluded.created_at",
        (pick["item_id"], json.dumps(pick, ensure_ascii=False),
         pick.get("end_time"), int(time.time())),
    )


def recent_picks(con, since: int) -> list[dict]:
    rows = con.execute("SELECT data FROM picks WHERE created_at>=?", (since,))
    return [json.loads(r["data"]) for r in rows]


def log_mail_events(con, events: list[dict]) -> None:
    """Remember the player's email events so the weekly report can count wins and losses."""
    now = int(time.time())
    for ev in events:
        if not ev.get("is_player"):
            continue
        con.execute(
            "INSERT OR IGNORE INTO mail_events VALUES (?,?,?,?,?,?)",
            (ev.get("date", ""), ev.get("type", ""), str(ev.get("item_id") or ""),
             ev.get("title") or "", json.dumps(ev, ensure_ascii=False), now),
        )


def mail_events(con, since_date: str) -> list[dict]:
    rows = con.execute(
        "SELECT data FROM mail_events WHERE date>=? ORDER BY date", (since_date,))
    return [json.loads(r["data"]) for r in rows]


def note_passed(con, item_id: str) -> None:
    """Remember what a hidden pick was, not just its number, so scoring can learn from it."""
    row = con.execute("SELECT data FROM picks WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        return
    pick = json.loads(row["data"])
    kept = [p for p in passed(con) if p["item_id"] != item_id]
    kept.append({"item_id": item_id, "title": pick.get("title"), "card": pick.get("card"),
                 "tier": pick.get("tier"), "price": pick.get("price"), "at": int(time.time())})
    kv_set(con, PASSED_KEY, json.dumps(kept[-MAX_PASSED:], ensure_ascii=False))


def passed(con) -> list[dict]:
    """The picks hidden with the Skip button, oldest first."""
    return json.loads(kv_get(con, PASSED_KEY, "[]"))


def log_snipe(con, snipe: dict) -> None:
    """Keep a Gixen snipe's amount next to the max and going rate the bot gave that auction.

    The amount follows the snipe if it is changed; a snipe on an auction the
    bot never picked is kept too, with no bot numbers.
    """
    row = con.execute("SELECT data FROM picks WHERE item_id=?", (snipe["item_id"],)).fetchone()
    pick = json.loads(row["data"]) if row else {}
    con.execute(
        "INSERT INTO snipe_log (item_id, title, my_max, bot_max, bot_value, logged_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET my_max=excluded.my_max, "
        "bot_max=COALESCE(excluded.bot_max, snipe_log.bot_max), "
        "bot_value=COALESCE(excluded.bot_value, snipe_log.bot_value)",
        (snipe["item_id"], pick.get("card") or snipe.get("title"), snipe.get("max_bid"),
         pick.get("max"), pick.get("value"), int(time.time())))


def recent_snipes(con, limit: int) -> list[dict]:
    """The latest snipes logged, newest first."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM snipe_log ORDER BY logged_at DESC, rowid DESC LIMIT ?", (limit,))]


def kv_get(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO kv (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
