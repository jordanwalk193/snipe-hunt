"""Case hits and other priority cards: the ones you want first (data/casehits.md).

Every row of casehits.md is a card with an eBay search, a kind (Case hit, SSP,
Variation, Auto, Grail, Event or Hype) and a ceiling. This module:

  - searches a few rows each hourly run, round-robin, plus "case hit" and "ssp"
    every run, so a new listing of any row is found within a few hours;
  - gives a matching listing the row's ceiling instead of steals mode's
    (steals.too_dear asks `ceiling_for`), and the PRIORITY label on its picks;
  - sends one alert the first time a copy is listed at or under its ceiling;
  - keeps every live copy of every row, cheapest first, with what the row has
    sold for, for the dashboard's Case hits panel.

A Hype row is a card sellers *call* a case hit or SSP that sells like a common
(the 2026 Topps Holiday "SSP" variations go for $2–3). Hype rows never alert and
never lift the ceiling; the dashboard shows them as a warning. Only recommends:
nothing here bids or buys.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import datetime

from . import config, db, musthave, rarity, settings, usage

def file():
    return config.DATA_DIR / "casehits.md"


LANE = "priority"                 # its eBay calls, against the daily cap (usage.py)
KINDS = ("Case hit", "SSP", "Variation", "Auto", "Grail", "Event", "Hype")
HYPE = "Hype"
KEY = "priority"                  # the live copies of every row, saved hourly
CURSOR_KEY = "priority_cursor"    # which row the next run searches from
SENT_KEY = "priority_sent"        # item ids already alerted
SEEDED_KEY = "priority_seeded"    # rows searched at least once: their old listings are known
DUE_KEY = "priority_last_due"     # rows this run searched
KEEP_SENT = 500
PER_RUN = 3                       # rows searched each hourly run (eBay calls are shared)
PAGE = 50
ALERT_MAX = 6                     # alerts per run at most; the rest wait an hour
LIVE_MAX = 8                      # live copies kept per row
# Searched every run: titles that claim a case hit or SSP, which the rows may not cover.
FIXED_WORDS = ("case hit", "ssp")
# Not one card: lots, customs, reprints, digital cards and sealed product.
NOT_A_CARD = re.compile(r"\b(lot|lots|you pick|u pick|pick your|complete set|reprint|custom|"
                        r"digital|bunt|break|sealed)\b", re.I)

_cache: tuple[float, list[dict]] | None = None


def _money(text: str) -> float | None:
    found = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(found.group()) if found else None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def read(path=None) -> list[dict]:
    """The rows of casehits.md in file order (read again whenever it changes).
    Optional: without the file the list is empty and nothing here runs."""
    global _cache
    path = path or file()
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if path == file() and _cache and _cache[0] == mtime:
        return _cache[1]
    rows = []
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[1] not in KINDS or not cells[4]:
            continue
        hype = cells[1] == HYPE
        rows.append({"card": cells[0], "kind": cells[1], "rare": cells[2], "typical": cells[3],
                     "search": cells[4], "ceiling": None if hype else _money(cells[5]),
                     "hype": hype, "key": slug(cells[0])})
    if path == file():
        _cache = (mtime, rows)
    return rows


RUN_TERM = re.compile(r"^/(\d{1,3})$")   # "/10" in a search: numbered to 10 or fewer


def print_run(title: str) -> int | None:
    """The smallest print run a title states ("07/10", "/10", "1/1", Superfractor)."""
    if rarity.ONE_OF_ONE.search(title):
        return 1
    runs = [int(run) for copy, run in rarity.SERIAL.findall(title)
            if 1 <= int(run) <= rarity.MAX_RUN and (not copy or 1 <= int(copy) <= int(run))]
    return min(runs) if runs else None


def matches(search: str, title: str) -> bool:
    """musthave.matches, plus "/N": the title must say it is numbered to N or fewer."""
    words, runs = [], []
    for term in search.split():
        found = RUN_TERM.match(term)
        (runs.append(int(found.group(1))) if found else words.append(term))
    if runs:
        run = print_run(title)
        if run is None or run > min(runs):
            return False
    return musthave.matches(" ".join(words), title)


def ebay_query(search: str) -> str:
    """The search as eBay gets it: "/N" terms are for matching only."""
    return " ".join(t for t in search.split() if not RUN_TERM.match(t))


def entry_for(title: str, rows: list[dict] | None = None) -> dict | None:
    """The first row a listing title matches, or None. Lots and customs never match."""
    title = title or ""
    if not title or NOT_A_CARD.search(title):
        return None
    for row in rows if rows is not None else read():
        if matches(row["search"], title):
            return row
    return None


def ceiling_for(title: str) -> float | None:
    """The row's ceiling when the title is a priority card (not hype), else None."""
    row = entry_for(title)
    return row["ceiling"] if row and not row["hype"] and row["ceiling"] else None


def prompt_rule() -> str:
    """One line for the scoring prompt: these cards may cost up to their own ceiling."""
    rows = [r for r in read() if not r["hype"] and r["ceiling"]]
    if not rows:
        return ""
    listed = "; ".join(f"{r['card']} up to ${r['ceiling']:.0f}" for r in rows)
    return ("- Priority cards (case hits, SSPs, grails the collector wants first) have their "
            f"own ceilings: {listed}. Still set the max under the card's "
            "going rate. Sellers often call plain cards \"case hit\" or \"SSP\": trust the "
            "card, not the title.\n")


# --- searching ---------------------------------------------------------------------

def due(con, rows: list[dict] | None = None, per_run: int = PER_RUN) -> list[tuple[str, str]]:
    """This run's searches: the fixed two plus the next `per_run` rows, round-robin.
    Hype rows aren't searched (the broad searches find plenty of them)."""
    from . import profile   # profile imports nothing from here; kept local
    rows = [r for r in (rows if rows is not None else read()) if not r["hype"]]
    if not rows:
        return []
    name = profile.get().search_name
    out = [("priority-" + slug(w), f"{name} {w}") for w in FIXED_WORDS]
    start = int(db.kv_get(con, CURSOR_KEY, "0") or 0) % len(rows)
    for i in range(min(per_run, len(rows))):
        row = rows[(start + i) % len(rows)]
        out.append(("priority-" + row["key"], ebay_query(row["search"])))
    db.kv_set(con, CURSOR_KEY, str((start + per_run) % len(rows)))
    db.kv_set(con, DUE_KEY, json.dumps([lbl[len("priority-"):] for lbl, _ in out[len(FIXED_WORDS):]]))
    return out


def search(con, known: set[str], client=None, verbose: bool = False) -> list:
    """Listings this run's priority searches found that the other searches didn't."""
    from . import deep, ebay_search   # ebay_search imports settings; kept local
    todo = due(con) if read() else []
    if not todo or not usage.ebay_allowed(LANE, con):
        return []
    client = client or ebay_search.EbayClient()
    found: dict = {}
    for label, q in todo:
        if not usage.ebay_allowed(LANE, con):
            break
        client.lane = label
        try:
            got = client.search(q, ebay_search.ALL_FORMATS, "newlyListed", limit=PAGE)
        except ebay_search.RateLimited:
            break
        except Exception as exc:   # one bad search shouldn't stop the hunt
            print(f"priority: {label}: {exc}", file=sys.stderr)
            continue
        deep._parse(got, known, label, found)
    if verbose:
        print(f"  priority: {len(todo)} searches, {len(found)} listings new to this run",
              file=sys.stderr)
    return list(found.values())


# --- what is for sale, owned and sold ----------------------------------------------

def _listing(item, row: dict) -> dict:
    return {"item_id": item.item_id, "title": item.title, "price": item.price,
            "shipping": item.shipping, "total": round(item.price + (5.0 if item.shipping is None
                                                                     else item.shipping), 2),
            "is_auction": item.is_auction, "bids": item.bid_count,
            "best_offer": "BEST_OFFER" in item.buying_options, "end_time": item.end_time,
            "seller": item.seller, "url": item.url, "image": item.image, "row": row["key"]}


def save(con, listings: list) -> int:
    """Keep every live copy of every row (cheapest first) for the dashboard."""
    rows = read()
    by_row: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for item in listings:
        if item.match != "yes" or item.currency != "USD" or item.item_id in seen:
            continue
        row = entry_for(item.title, rows)
        if row:
            seen.add(item.item_id)
            by_row.setdefault(row["key"], []).append(_listing(item, row))
    for key in by_row:
        by_row[key].sort(key=lambda r: r["total"])
        by_row[key] = by_row[key][:LIVE_MAX]
    db.kv_set(con, KEY, json.dumps(by_row, ensure_ascii=False))
    return sum(len(v) for v in by_row.values())


def owned(row: dict, inventory_rows: list[dict]) -> bool:
    """Does an inventory row (not sold or traded) fit this row's search?"""
    for card in inventory_rows:
        if card.get("status") == "Sold-Traded":
            continue
        text = " ".join(str(card.get(k) or "") for k in
                        ("year", "set", "card", "parallel", "card_no", "serial", "notes"))
        if matches(row["search"], text):
            return True
    return False


def sold(con, rows: list[dict]) -> dict[str, dict]:
    """What each row has sold for, delivered, from the ledger."""
    out: dict[str, list[tuple[int, float]]] = {}
    for s in con.execute("SELECT title, price, shipping, sold_at FROM sales WHERE price > 0"):
        row = entry_for(s["title"], rows)
        if row:
            total = float(s["price"]) + (5.0 if s["shipping"] is None else float(s["shipping"]))
            out.setdefault(row["key"], []).append((int(s["sold_at"] or 0), round(total, 2)))
    stats = {}
    for key, xs in out.items():
        xs.sort()
        prices = [p for _, p in xs]
        stats[key] = {"n": len(xs), "median": round(statistics.median(prices), 2),
                      "low": min(prices), "high": max(prices),
                      "last": {"at": xs[-1][0], "total": xs[-1][1]}}
    return stats


def view(con, inventory_rows: list[dict], now: datetime) -> dict:
    """The Case hits panel: every row with what's for sale, owned and sold."""
    rows = read()
    live = json.loads(db.kv_get(con, KEY, "{}") or "{}")
    now_iso = now.isoformat(timespec="minutes")
    stats = sold(con, rows)
    out = []
    for row in rows:
        copies = [r for r in live.get(row["key"], [])
                  if not (r["is_auction"] and r["end_time"] and r["end_time"] <= now_iso)]
        under = [r for r in copies if row["ceiling"] and r["total"] <= row["ceiling"]]
        out.append({**row, "owned": owned(row, inventory_rows), "live": copies,
                    "under": len(under), "sold": stats.get(row["key"]),
                    "ebay": musthave.ebay_url(ebay_query(row["search"]))})
    real = [r for r in out if not r["hype"]]
    return {"rows": out, "live_n": sum(len(r["live"]) for r in real),
            "under_n": sum(r["under"] for r in real),
            "owned_n": sum(r["owned"] for r in real), "n": len(real)}


# --- alerts ------------------------------------------------------------------------

def message(item, row: dict) -> tuple[str, str]:
    total = item.price + (5.0 if item.shipping is None else item.shipping)
    title = f"🎯 {row['kind']} listed: ${total:.2f}"
    how = ("auction, " + (f"{item.bid_count} bids" if item.bid_count else "no bids yet")
           if item.is_auction else "Buy It Now" + (" or Best Offer"
                                                   if "BEST_OFFER" in item.buying_options else ""))
    lines = [row["card"], item.title,
             f"${item.price:.2f} + " + ("ship ?" if item.shipping is None else f"${item.shipping:.2f} ship")
             + f" · {how}",
             f"Typical: {row['typical']} · your ceiling ${row['ceiling']:.0f}"]
    if item.is_auction and item.end_time:
        lines.append("Ends " + datetime.fromisoformat(item.end_time).strftime("%a %I:%M %p").replace(" 0", " "))
    lines.append(f"Item {item.item_id}")
    return title, "\n".join(lines)


def to_alert(con, listings: list, now: datetime | None = None) -> list[tuple]:
    """(listing, row) for each new priority listing at or under its row's ceiling."""
    rows = read()
    sent = set(json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]"))
    now_iso = (now or datetime.now(config.TZ)).isoformat(timespec="minutes")
    out, seen = [], set()
    for item in listings:
        if (item.match != "yes" or item.currency != "USD" or item.item_id in sent
                or item.item_id in seen):
            continue
        if item.is_auction and item.end_time and item.end_time <= now_iso:
            continue
        row = entry_for(item.title, rows)
        if not row or row["hype"] or not row["ceiling"]:
            continue
        if item.price + (5.0 if item.shipping is None else item.shipping) <= row["ceiling"]:
            seen.add(item.item_id)
            out.append((item, row))
    return out


def seed(con, found: list[tuple]) -> list[tuple]:
    """The first search of a row turns up listings that may have been up for weeks:
    those are marked known without an alert (the dashboard shows them), so an alert
    always means a copy that is new. Returns the ones still to alert."""
    seeded = set(json.loads(db.kv_get(con, SEEDED_KEY, "[]") or "[]"))
    quiet = [(i, r) for i, r in found if r["key"] not in seeded]
    if quiet:
        sent = json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]")
        sent += [i.item_id for i, _ in quiet]
        db.kv_set(con, SENT_KEY, json.dumps(sent[-KEEP_SENT:]))
    seeded |= set(json.loads(db.kv_get(con, DUE_KEY, "[]") or "[]"))
    db.kv_set(con, SEEDED_KEY, json.dumps(sorted(seeded)))
    return [(i, r) for i, r in found if r["key"] in seeded and (i, r) not in quiet]


def alert(con, listings: list, now: datetime | None = None, send: bool = True) -> list[tuple]:
    """One push per new priority listing at or under its ceiling. Quiet hours hold
    them for the morning run (they are not marked sent until they go out)."""
    from . import notify   # notify imports score; kept local
    now = now or datetime.now(config.TZ)
    cfg = settings.load(con)
    if not cfg.get("priority_alerts", True) or not cfg["alerts_enabled"]:
        return []
    fresh = to_alert(con, listings, now)
    if send:
        fresh = seed(con, fresh)
        con.commit()
    fresh = fresh[:ALERT_MAX]
    if not fresh or not send or notify.is_quiet(now) or not notify.configured():
        return fresh if not send else []
    sent = json.loads(db.kv_get(con, SENT_KEY, "[]") or "[]")
    done = []
    for item, row in fresh:
        title, body = message(item, row)
        try:
            notify.send(title, body, url=item.url, image=item.image,
                        actions=notify.pick_actions({"item_id": item.item_id,
                                                     "is_auction": item.is_auction,
                                                     "url": item.url}))
        except Exception as exc:   # the next run tries again
            print(f"priority: {exc}", file=sys.stderr)
            break
        sent.append(item.item_id)
        done.append((item, row))
    db.kv_set(con, SENT_KEY, json.dumps(sent[-KEEP_SENT:]))
    db.bump(con, "priority_alerts", len(done))
    con.commit()
    if done:
        print(f"priority: {len(done)} alerts sent", file=sys.stderr)
    return done


def main() -> None:
    """`python -m snipehunt.priority`: the rows and how each matches what's on record."""
    con = db.connect()
    from . import inventory
    v = view(con, inventory.read()[1], datetime.now(config.TZ))
    for r in v["rows"]:
        s = r["sold"]
        print(f"{r['kind']:9} {'✓' if r['owned'] else ' '} live {len(r['live']):2} "
              f"under {r['under']:2} sold {s['n'] if s else 0:3} "
              f"med {('$%.2f' % s['median']) if s else '-':>8}  {r['card']}")
    print(f"{v['n']} rows, {v['owned_n']} owned, {v['live_n']} live copies, "
          f"{v['under_n']} at or under ceiling")


if __name__ == "__main__":
    main()
