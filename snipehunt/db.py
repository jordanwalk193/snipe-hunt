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
"""

PRICE_DROP = 0.20  # re-score / re-alert only when the price falls this much


def connect() -> sqlite3.Connection:
    config.ensure_state_dir()
    # The API reads while a job writes; wait for the other side instead of failing.
    con = sqlite3.connect(config.SEEN_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _dropped(old: float | None, new: float) -> bool:
    return old is not None and old > 0 and new <= old * (1 - PRICE_DROP)


def touch(con, item_id: str, title: str) -> None:
    now = int(time.time())
    con.execute(
        "INSERT INTO seen (item_id, title, first_seen, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET last_seen=excluded.last_seen",
        (item_id, title, now, now),
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


def kv_get(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO kv (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
