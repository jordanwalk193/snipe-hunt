"""Update inventory.csv from eBay and Gixen emails.

Gmail is opened read-only over IMAP: nothing is sent, deleted or marked read.
"""
from __future__ import annotations

import argparse
import email
import html
import imaplib
import json
import re
import sys
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from . import config, db, gitsync, inventory, profile
from .score import ask_claude, extract_json

IMAP_HOST = "imap.gmail.com"
MAILBOX = '"[Gmail]/All Mail"'
SUBJECT_RE = re.compile(
    r"won|order confirmed|order update|order delivered|package|deliver|shipped|"
    r"on its way|offer|outbid|auction end|payment|pay now|paid|invoice|refund|cancel",
    re.I)
ITEM_RE = re.compile(r"(?:itm/(?:[^/\s\"?]*/)?|item(?:\s*(?:id|number|no\.?|#))?\s*[:#]?\s*)(\d{11,13})", re.I)
BATCH = 30
BODY_CHARS = 1600

EVENT_STATUS = {
    "won": "Won", "offer_accepted": "Awaiting payment",
    "paid": "In transit", "shipped": "In transit", "delivered": "Delivered",
}

PROMPT = """These are {collector}'s recent eBay and Gixen emails (already \
filtered to order-related ones). {collector} collects {player}. Turn them into \
inventory events.

For each email decide one event type:
  won            auction won (eBay "You won", or Gixen end notice saying HIGH BIDDER)
  offer_accepted seller accepted the offer; payment still due
  paid           order confirmed / payment made
  shipped        package handed to the carrier or in transit
  delivered      delivered
  lost           Gixen end notice saying NOT high bidder, or an outbid notice
  offer_countered / offer_declined
  cancelled      order cancelled or refunded
  other          anything else (marketing, watch reminders, etc.)

Rules:
- is_player is true only when the item is a {name} card.
- item_id is the eBay item number if the email shows one (candidates are \
listed under "item ids found"), else null.
- price is what the card itself costs in dollars, shipping separate. Use \
null when the email doesn't say; never guess.
- match_row: if this item is already a row in the inventory below (same eBay \
item id, or same card from the same seller at about the same price and date), \
give that row's id, else null.
- For won / offer_accepted / paid events on {name} cards that are NOT already \
in the inventory, fill "row" from the title using the inventory's own style: \
year, set, card, parallel, card_no, serial (e.g. 029/175), rc, auto, relic \
(Y or empty), graded (grade text or empty), notes (short). Otherwise row is null.
- One email can describe several items; emit one event per item.

Reply with ONLY one JSON object, no prose:
{{"events": [{{"msg": 1, "type": "won", "is_player": true, "item_id": "…", \
"title": "…", "price": 6.06, "shipping": 1.25, "seller": "…", "date": \
"YYYY-MM-DD", "match_row": null, "row": {{"year": "", "set": "", "card": "", \
"parallel": "", "card_no": "", "serial": "", "rc": "", "auto": "", "relic": \
"", "graded": "", "notes": ""}}}}]}}

=== inventory.csv ===
{inventory}

=== emails ===
{emails}
"""


def configured() -> bool:
    """The email sync is optional: it runs only when a Gmail login is set."""
    return bool(config.get("GMAIL_USER") and config.get("GMAIL_APP_PASSWORD"))


def _text(msg: email.message.Message) -> str:
    plain, rich = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            plain += text
        else:
            rich += text
    if len(plain.strip()) < 200 and rich:
        rich = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", rich)
        plain = html.unescape(re.sub(r"(?s)<[^>]+>", " ", rich))
    return re.sub(r"\s+", " ", plain).strip(), rich


def fetch(days: int, skip_ids: set[str]) -> list[dict]:
    user, password = config.require("GMAIL_USER", "GMAIL_APP_PASSWORD")
    box = imaplib.IMAP4_SSL(IMAP_HOST)
    box.login(user, password.replace(" ", ""))
    out: list[dict] = []
    try:
        box.select(MAILBOX, readonly=True)
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = box.search(None, f'(SINCE {since} OR FROM "ebay.com" FROM "gixen.com")')
        for num in data[0].split():
            _, head = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
            hdr = email.message_from_bytes(head[0][1])
            subject = str(make_header(decode_header(hdr.get("Subject", ""))))
            sender = hdr.get("From", "").lower()
            msg_id = hdr.get("Message-ID", "").strip() or f"{num.decode()}:{subject}"
            is_gixen = "gixen" in sender
            if msg_id in skip_ids or not (is_gixen or SUBJECT_RE.search(subject)):
                continue
            _, full = box.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1])
            text, rich = _text(msg)
            try:
                when = parsedate_to_datetime(hdr.get("Date")).astimezone(config.TZ)
            except (TypeError, ValueError):
                when = datetime.now(config.TZ)
            ids = list(dict.fromkeys(ITEM_RE.findall(rich + " " + text)))[:6]
            out.append({"msg_id": msg_id, "source": "gixen" if is_gixen else "ebay",
                        "subject": subject, "date": when.strftime("%Y-%m-%d"),
                        "item_ids": ids, "body": text[:BODY_CHARS]})
    finally:
        box.logout()
    out.sort(key=lambda m: m["date"])
    return out


def classify(emails: list[dict], verbose: bool = False) -> list[dict]:
    events: list[dict] = []
    inv = config.INVENTORY.read_text()
    for start in range(0, len(emails), BATCH):
        batch = emails[start:start + BATCH]
        blocks = [
            f"--- msg {n} | {m['source']} | {m['date']} ---\nsubject: {m['subject']}\n"
            f"item ids found: {', '.join(m['item_ids']) or 'none'}\n{m['body']}"
            for n, m in enumerate(batch, 1)]
        reply = extract_json(ask_claude(
            PROMPT.format(collector=profile.get().collector, player=profile.get().described,
                        name=profile.get().player, inventory=inv,
                        emails="\n\n".join(blocks)), tools="", timeout=900))
        for ev in reply.get("events", []) or []:
            n = ev.get("msg")
            if isinstance(n, int) and 1 <= n <= len(batch):
                ev["source"] = batch[n - 1]["source"]
                ev.setdefault("date", batch[n - 1]["date"])
                events.append(ev)
        if verbose:
            print(f"sync: classified {min(start + BATCH, len(emails))}/{len(emails)} emails",
                  file=sys.stderr)
    return events


def apply(events: list[dict]) -> tuple[list[str], list[dict], list[str], dict]:
    """Apply events to the inventory in memory. Returns fields, rows, change log, counts."""
    fields, rows = inventory.read()
    changes: list[str] = []
    counts = {"added": 0, "updated": 0, "gixen_wins": 0, "lost": 0}
    order = {"won": 0, "offer_accepted": 1, "paid": 2, "shipped": 3, "delivered": 4}
    for ev in sorted(events, key=lambda e: (e.get("date", ""), order.get(e.get("type"), 9))):
        kind = ev.get("type")
        if kind == "lost" and ev.get("is_player"):
            counts["lost"] += 1
        status = EVENT_STATUS.get(kind)
        if not status or not ev.get("is_player"):
            continue
        item_id = str(ev["item_id"]) if ev.get("item_id") else ""
        row = inventory.find(rows, item_id, ev.get("match_row"))
        if row is None:
            if kind not in ("won", "offer_accepted", "paid") or not ev.get("row"):
                continue  # a delivery for something we can't place: leave it alone
            spec = ev["row"]
            row = {f: "" for f in fields}
            row.update({k: str(spec.get(k) or "") for k in
                        ("year", "set", "card", "parallel", "card_no", "serial",
                         "rc", "auto", "relic", "graded", "notes")})
            row.update({
                "id": str(inventory.next_id(rows)), "status": status,
                "price_paid": f"{float(ev['price']):.2f}" if ev.get("price") is not None else "",
                "order_date": ev.get("date", ""), "seller": ev.get("seller") or "",
                "ebay_item_id": item_id})
            rows.append(row)
            counts["added"] += 1
            if ev.get("source") == "gixen":
                counts["gixen_wins"] += 1
            changes.append(f"+ #{row['id']} {row['year']} {row['set']} {row['card']} "
                           f"{row['parallel']} | {status} | ${row['price_paid'] or '?'} "
                           f"| {row['seller']} | item {item_id or '?'}")
            continue
        before = row.get("status", "")
        touched = inventory.advance(row, status)
        if item_id and not row.get("ebay_item_id"):
            row["ebay_item_id"] = item_id
            touched = True
        priced = False
        if ev.get("price") is not None and not row.get("price_paid"):
            row["price_paid"] = f"{float(ev['price']):.2f}"
            touched = priced = True
        if touched:
            counts["updated"] += 1
            what = (f"{before} -> {row['status']}" if before != row["status"]
                    else f"price ${row['price_paid']} filled in" if priced
                    else f"item id {item_id} filled in")
            changes.append(f"~ #{row['id']} {row['year']} {row['set']} {row['card']} "
                           f"{row['parallel']} | {what}")
    return fields, rows, changes, counts


def commit_message(counts: dict) -> str:
    parts = []
    if counts["added"]:
        src = " (Gixen wins)" if counts["gixen_wins"] == counts["added"] else ""
        parts.append(f"+{counts['added']}{src}")
    if counts["updated"]:
        parts.append(f"{counts['updated']} status update{'s' if counts['updated'] != 1 else ''}")
    return "inventory: " + ", ".join(parts)


def run(days: int | None = None, dry_run: bool = False, verbose: bool = True) -> dict:
    con = db.connect()
    con.execute("CREATE TABLE IF NOT EXISTS mail_seen (msg_id TEXT PRIMARY KEY, seen_at INTEGER)")
    done = set() if dry_run else {r[0] for r in con.execute("SELECT msg_id FROM mail_seen")}
    if days is None:
        last = db.kv_get(con, "mail_last_run")
        days = max(2, (date.today() - date.fromisoformat(last)).days + 2) if last else 14
    if not dry_run:
        gitsync.pull()
    emails = fetch(days, done)
    if verbose:
        print(f"sync: {len(emails)} order-related emails in the last {days} days "
              f"({sum(m['source'] == 'gixen' for m in emails)} from Gixen)", file=sys.stderr)
    events = classify(emails, verbose) if emails else []
    fields, rows, changes, counts = apply(events)
    kinds: dict[str, int] = {}
    for ev in events:
        if ev.get("is_player"):
            kinds[ev.get("type", "?")] = kinds.get(ev.get("type", "?"), 0) + 1
    result = {"emails": len(emails), "player_events": kinds, "changes": changes,
              "counts": counts, "events": events}
    if dry_run:
        return result
    if changes:
        inventory.write(fields, rows)
        gitsync.commit_and_push(["inventory.csv"], commit_message(counts))
    db.log_mail_events(con, events)
    con.executemany("INSERT OR IGNORE INTO mail_seen VALUES (?, strftime('%s','now'))",
                    [(m["msg_id"],) for m in emails])
    db.kv_set(con, "mail_last_run", date.today().isoformat())
    con.commit()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync inventory.csv from eBay/Gixen emails")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change; write, commit and remember nothing")
    args = ap.parse_args()
    result = run(args.days, args.dry_run)
    print(f"Card events: {result['player_events'] or 'none'}")
    print(f"Inventory changes ({'dry run, nothing written' if args.dry_run else 'applied'}):")
    for line in result["changes"] or ["  (none)"]:
        print("  " + line)


if __name__ == "__main__":
    main()
