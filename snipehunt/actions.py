"""What the buttons in Home Assistant can ask the bot to do.

Everything here stays inside the bot: hide a pick, mark cards as filed in the
binder, re-read Gixen, start a search, send a test alert. Nothing bids, buys
or touches a snipe.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime

from . import config, db, gitsync, gixen, inventory, notify, report, scheduler, snapshot

MAX_SKIPPED = 2000
REASONS = db.REASONS    # "bad pick" reasons, then "good_pick"
MAX_NOTE = 200


def _item_id(data: dict) -> str:
    item = str(data.get("item_id", ""))
    if not re.fullmatch(r"\d{9,14}", item):
        raise ValueError("not an eBay item number")
    return item


def clean_note(text) -> str:
    """A feedback note as it may be shown to Claude: one line, no control or
    invisible formatting characters, at most MAX_NOTE characters."""
    text = "".join(c if unicodedata.category(c)[0] != "C" else " " for c in str(text or ""))
    return " ".join(text.split())[:MAX_NOTE].strip()


def skip(con, data: dict) -> str:
    item = _item_id(data)
    ids = [i for i in snapshot.skipped_ids(con) if i != item] + [item]
    db.kv_set(con, snapshot.SKIPPED_KEY, json.dumps(ids[-MAX_SKIPPED:]))
    db.note_passed(con, item)   # which card it was, so scoring picks fewer like it
    con.commit()
    return "Hidden. It won't come back unless you restore skipped picks."


def feedback(con, data: dict) -> str:
    """"Bad pick" with a reason, which also hides it, or "good call", which doesn't."""
    item = _item_id(data)
    reason = str(data.get("reason", ""))
    if reason not in REASONS:
        raise ValueError(f"reason must be one of {', '.join(REASONS)}")
    db.add_feedback(con, item, reason, clean_note(data.get("note")))
    con.commit()
    if reason == "good_pick":
        return "Thanks: it will look for more like this one."
    skip(con, {"item_id": item})
    return "Got it: hidden, and scoring will learn from it."


def unskip_all(con, data: dict) -> str:
    count = len(snapshot.skipped_ids(con))
    db.kv_set(con, snapshot.SKIPPED_KEY, "[]")
    db.kv_set(con, db.PASSED_KEY, "[]")   # restored picks are no longer "passed on"
    con.commit()
    return f"Restored {count} skipped pick{'s' if count != 1 else ''}."


def _push_inventory(message: str) -> None:
    try:
        gitsync.commit_and_push(["inventory.csv"], message)
    except Exception as exc:   # the next sync commits and pushes it instead
        print(f"actions: git push failed: {exc}", file=sys.stderr)


def filed(con, data: dict) -> str:
    """Delivered cards that are now in the binder."""
    want = {str(i) for i in data.get("ids", [])}
    if not want or len(want) > 200:
        raise ValueError("no cards given")
    gitsync.pull()
    fields, rows = inventory.read()
    done = [r for r in rows if r.get("id") in want and r.get("status") == "Delivered"
            and inventory.advance(r, "In binder")]
    if not done:
        return "Nothing to change: those cards aren't waiting to be filed."
    inventory.write(fields, rows)
    # A git remote can take a few seconds; the button shouldn't wait for it.
    threading.Thread(target=_push_inventory, daemon=True,
                     args=(f"inventory: {len(done)} filed in binder",)).start()
    if len(done) == 1:
        return f"Filed: {report.card_name(done[0])}."
    return f"Filed {len(done)} cards in the binder."


def refresh(con, data: dict) -> str:
    return "Refreshed."


def search(con, data: dict) -> str:
    if not scheduler.trigger("hourly"):
        return "A search is already running."
    time.sleep(1)   # so the snapshot sent back already says "searching"
    return "Search started. New picks show up in a minute or two."


def gixen_refresh(con, data: dict) -> str:
    """Re-read Gixen now. `login` signs back in even if your browser took over."""
    if data.get("login"):
        db.kv_set(con, gixen.SIGNED_OUT_KEY, "1")   # long ago: no waiting period
    cache = json.loads(db.kv_get(con, gixen.CACHE_KEY, "{}"))
    cache["checked"] = 0
    db.kv_set(con, gixen.CACHE_KEY, json.dumps(cache))
    con.commit()
    _, problem, _ = gixen.snipes(con)
    return f"Gixen: {problem}." if problem else "Gixen snipe list re-read."


def test_alert(con, data: dict) -> str:
    notify.send_test("Home Assistant")
    return "Test alert sent to your phone."


ACTIONS = {"skip": skip, "feedback": feedback, "unskip_all": unskip_all, "filed": filed, "refresh": refresh,
           "search": search, "gixen_refresh": gixen_refresh, "test_alert": test_alert}


def handle(con, data: dict) -> tuple[bool, str]:
    """Run one action and remember how it went for the dashboard."""
    name = data.get("action")
    if name not in ACTIONS:
        ok, message = False, f"Unknown action {name!r}"
    else:
        try:
            ok, message = True, ACTIONS[name](con, data)
        except Exception as exc:   # one bad tap must not take the API down
            ok, message = False, f"{name} failed: {type(exc).__name__}: {exc}"[:250]
    db.kv_set(con, snapshot.LAST_ACTION_KEY, json.dumps({
        "ok": ok, "action": name if name in ACTIONS else None, "message": message,
        "at": datetime.now(config.TZ).isoformat(timespec="seconds")}))
    con.commit()
    return ok, message
