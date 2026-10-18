"""What to tell you when a card is won: where it goes in the binder, and when to wait
for one combined invoice.

Wins come from the order emails the daily inventory sync reads (mail_events).
Nothing here pays, messages a seller or changes the inventory.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import config, db, inventory, known_cards, notify, profile, score

ALERTED_KEY = "wins_alerted"
ALERTED_KEEP = 300
REMIND_KEY = "ship_reminders"
WIN_TYPES = ("won", "offer_accepted")


def binder_line(title: str, known: known_cards.KnownCards) -> str:
    """Binder: 2023 → pp8–11, with the year from known_cards.toml when the title's is wrong."""
    who = profile.get()
    year, source = known.year_for(title)
    verdict = known.check(title)
    page = who.binder_page(str(year) if year else None)
    if year is None:
        return f"Binder: year unknown → {page} (check the back of the card)"
    line = f"Binder: {year} → {page}"
    if source == "known_cards" and verdict.title_year and verdict.title_year != year:
        line += f" (title says {verdict.title_year}; {verdict.number} is {year})"
    elif source == "title":
        line += " (year from the title: check the back of the card)"
    return line


def win_message(ev: dict, known: known_cards.KnownCards) -> str:
    title = " ".join(str(ev.get("title") or "a card").split())
    price = ev.get("price")
    money = f"${float(price):.2f}" if price is not None else "$?"
    if ev.get("shipping") is not None:
        money += f" + ${float(ev['shipping']):.2f} ship"
    lines = [f"🏆 Won: {title}",
             money + (f" · {ev['seller']}" if ev.get("seller") else "")
             + (" · Gixen" if ev.get("source") == "gixen" else ""),
             binder_line(title, known)]
    if ev.get("item_id"):
        lines.append(str(ev["item_id"]))
    return "\n".join(lines)


def new_wins(con, events: list[dict]) -> list[dict]:
    """Wins on the player's cards not yet told about, one per item."""
    told = set(json.loads(db.kv_get(con, ALERTED_KEY, "[]")))
    out, ids = [], set()
    for ev in events:
        key = str(ev.get("item_id") or ev.get("title") or "")
        if ev.get("type") in WIN_TYPES and ev.get("is_player") and key and key not in told \
                and key not in ids:
            ids.add(key)
            out.append(ev)
    return out


def announce(con, events: list[dict], dry_run: bool = False) -> int:
    """One message with every new win and its binder page. Returns wins sent."""
    wins = new_wins(con, events)
    if not wins or not (notify.configured() or dry_run):
        return 0
    known = known_cards.load()
    title = f"{profile.get().app_name}: {len(wins)} win{'s' if len(wins) != 1 else ''}"
    body = "\n\n".join(win_message(ev, known) for ev in wins)
    if dry_run:
        print(f"--- would send ---\n{title}\n{body}\n")
        return len(wins)
    notify.send(title, body)
    told = json.loads(db.kv_get(con, ALERTED_KEY, "[]"))
    told += [str(ev.get("item_id") or ev.get("title")) for ev in wins]
    db.kv_set(con, ALERTED_KEY, json.dumps(told[-ALERTED_KEEP:]))
    con.commit()
    return len(wins)


def due_reminders(con, now: datetime | None = None, record: bool = True) -> list[str]:
    """Sellers with two or more open orders, not yet reminded today: their lines.

    The record of reminders is trimmed to sellers still open, so a seller whose
    orders are all paid drops off and is never reminded again for those."""
    now = now or datetime.now(config.TZ)
    since = (now - timedelta(days=score.OPEN_ORDER_DAYS)).strftime("%Y-%m-%d")
    orders = inventory.open_orders(inventory.read()[1], db.mail_events(con, since))
    today = now.date().isoformat()
    sent = json.loads(db.kv_get(con, REMIND_KEY, "{}"))
    sent = {s: d for s, d in sent.items() if len(orders.get(s, [])) >= 2}
    lines = []
    for seller, items in sorted(orders.items()):
        if len(items) >= 2 and sent.get(seller) != today:
            lines.append(f"{seller}: {len(items)} wins, wait for one combined invoice "
                         "before paying.")
            sent[seller] = today
    if record:
        db.kv_set(con, REMIND_KEY, json.dumps(sent))
        con.commit()
    return lines


def remind(con, now: datetime | None = None, dry_run: bool = False) -> int:
    """Send the combined-shipping reminders that are due. Returns how many sellers."""
    if not (notify.configured() or dry_run):
        return 0
    lines = due_reminders(con, now, record=not dry_run)
    if lines and not dry_run:
        notify.send(f"{profile.get().app_name}: combine shipping", "\n".join(lines))
    elif lines:
        print("--- would send ---\n" + "\n".join(lines))
    return len(lines)
