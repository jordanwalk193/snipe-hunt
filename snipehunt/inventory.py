"""Read and update inventory.csv without disturbing existing rows."""
from __future__ import annotations

import csv
import io
import re
from datetime import date

from . import config

# Statuses only ever move forward along this line.
ORDER = ["Won", "Awaiting payment", "In transit", "Delivered", "In binder"]
FROZEN = {"In binder", "Sold-Traded"}
UNPAID = ("Won", "Awaiting payment")


def read() -> tuple[list[str], list[dict]]:
    with config.INVENTORY.open(newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def dumps(fields: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write(fields: list[str], rows: list[dict]) -> None:
    config.INVENTORY.write_text(dumps(fields, rows))


def next_id(rows: list[dict]) -> int:
    return max((int(r["id"]) for r in rows if r.get("id", "").isdigit()), default=0) + 1


def find(rows: list[dict], item_id: str | None, row_id: str | None) -> dict | None:
    if item_id:
        for r in rows:
            if r.get("ebay_item_id") == item_id:
                return r
    if row_id:
        for r in rows:
            if r.get("id") == str(row_id):
                return r
    return None


def advance(row: dict, status: str) -> bool:
    """Move a row's status forward. Returns True if it changed."""
    current = row.get("status", "")
    if current in FROZEN or status not in ORDER:
        return False
    if current in ORDER and ORDER.index(status) <= ORDER.index(current):
        return False
    row["status"] = status
    return True


def open_sellers(rows: list[dict], events: list[dict]) -> set[str]:
    """Sellers with an order still open, in lower case: one more card can ride along.

    That is a card won or awaiting payment in the inventory, or a recent "won"
    email with no payment, shipping or delivery email after it.
    """
    return set(open_orders(rows, events))


def open_orders(rows: list[dict], events: list[dict]) -> dict[str, list[str]]:
    """The open orders (open_sellers) by seller in lower case: item numbers, or row ids."""
    out: dict[str, list[str]] = {}
    settled = {str(e.get("item_id")) for e in events
               if e.get("type") in ("paid", "shipped", "delivered")}

    def note(seller, key):
        seller = (seller or "").strip().lower()
        if seller and key not in out.setdefault(seller, []):
            out[seller].append(key)
    for r in rows:
        if r.get("status") in UNPAID:
            note(r.get("seller"), r.get("ebay_item_id") or f"row {r.get('id')}")
    for e in events:
        if e.get("type") in ("won", "offer_accepted") and str(e.get("item_id")) not in settled:
            note(e.get("seller"), str(e.get("item_id") or e.get("title")))
    return out


# Words that say a card is a parallel, not the base card.
PARALLEL_WORDS = re.compile(
    r"\b(refractor|xfractor|prizm|parallel|gold|silver|holo|foil|wave|shimmer|mojo|sapphire|"
    r"orange|purple|green|blue|red|black|pink|aqua|camo|lava|atomic|speckle|sp|ssp|variation)\b",
    re.I)
YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def _number(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def owns(rows: list[dict], title: str, player: str | None = None) -> bool:
    """Is the card in this title already in the collection? Only when it is clearly
    the same card: same year and card number, same player when both say, same
    print run, autograph and grade, and the row's parallel named in the title.
    Anything less sure is left for Claude, who sees the inventory too."""
    from . import rarity, sales   # both import listing code; kept local to avoid a cycle
    year = YEAR.search(title)
    number = sales.card_number(title)
    if not year or not number:
        return False
    run = rarity.print_run(title)
    auto = bool(rarity.AUTO.search(title))
    graded = bool(rarity.GRADED.search(title))
    low = title.lower()
    for row in rows:
        if row.get("status") == "Sold-Traded" or row.get("year") != year.group(1):
            continue
        if _number(row.get("card_no")) != _number(number):
            continue
        if player and row.get("player") and row["player"].strip().lower() != player.lower():
            continue
        serial = row.get("serial") or ""
        found = re.search(r"/\s?(\d+)", serial)
        if (1 if "1/1" in serial else int(found.group(1)) if found else None) != run:
            continue
        if bool((row.get("auto") or "").strip()) != auto:
            continue
        if bool((row.get("graded") or "").strip()) != graded:
            continue
        parallel = (row.get("parallel") or "").strip().lower()
        if parallel in ("", "base"):
            if PARALLEL_WORDS.search(title):
                continue
        elif not all(w in low for w in re.findall(r"[a-z0-9]+", parallel)):
            continue
        return True
    return False


# The inventory check: problems worth a look, listed and never fixed. The CSV
# is yours; nothing here writes it.
STALE_DELIVERED_DAYS = 14   # a delivered card not in the binder after this many days
FRONT_PAGE = 9              # a binder's front page holds nine cards
SAME_AS = re.compile(r"\bsame item as\b", re.I)
# "due 2026-10-20", "arrives 10/20", "est. delivery 10/20/2026" in the notes
DUE = re.compile(r"\b(?:due|arriv\w*|deliver\w*|est\w*)\D{0,14}"
                 r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.I)
DUPE_KEY = ("year", "set", "card", "parallel", "card_no", "serial", "seller", "order_date")


def _date(text: str, today: date) -> date | None:
    try:
        if "-" in text:
            return date.fromisoformat(text)
        parts = [int(p) for p in text.split("/")]
        year = parts[2] if len(parts) == 3 else today.year
        return date(year + 2000 if year < 100 else year, parts[0], parts[1])
    except ValueError:
        return None


def _label(row: dict) -> str:
    parts = [row.get(k) for k in ("year", "set", "card", "parallel") if row.get(k)]
    if row.get("card_no"):
        parts.append(f"#{row['card_no']}")
    if row.get("serial"):
        parts.append(row["serial"])
    return f"#{row.get('id') or '?'} " + " ".join(parts)


def check(rows: list[dict], known=None, today=None) -> dict:
    """Problems in the inventory, by kind. `known` is a known_cards.KnownCards table."""
    today = today or date.today()
    live = [r for r in rows if r.get("status") != "Sold-Traded"]
    out: dict[str, list[str]] = {k: [] for k in (
        "duplicates", "stale", "year_mismatch", "missing_card_no", "missing_item_id",
        "trade_pile", "front_page")}
    groups: dict[tuple, list[dict]] = {}
    for r in live:
        key = tuple((r.get(k) or "").strip().lower() for k in DUPE_KEY)
        if any(key):
            groups.setdefault(key, []).append(r)
    for same in groups.values():
        if len(same) > 1:
            out["duplicates"].append(" = ".join(_label(r) for r in same))
    for r in live:
        if SAME_AS.search(r.get("notes") or ""):
            out["duplicates"].append(f"{_label(r)}: notes say \"{r['notes'].strip()[:60]}\"")
        status = r.get("status", "")
        ordered = _date(r.get("order_date") or "", today)
        if status == "Delivered" and ordered and (today - ordered).days > STALE_DELIVERED_DAYS:
            out["stale"].append(f"{_label(r)}: Delivered, ordered {ordered}, not in the binder")
        due = DUE.search(r.get("notes") or "")
        due_on = _date(due.group(1), today) if due else None
        if status == "In transit" and due_on and due_on < today:
            out["stale"].append(f"{_label(r)}: In transit, was due {due_on}")
        if known is not None and r.get("card_no") and str(r.get("year", "")).isdigit():
            title = " ".join(str(r.get(k) or "") for k in ("year", "set", "card", "card_no"))
            cards = known.resolve(r["card_no"], title)
            years = sorted({c.year for c in cards})
            if years and int(r["year"]) not in years:
                drift = {y for c in cards for y in c.drift_years}
                out["year_mismatch"].append(
                    f"{_label(r)}: known_cards says {'/'.join(map(str, years))} "
                    f"{cards[0].product}" + (" (a common mix-up)" if int(r["year"]) in drift
                                             else ""))
        if not (r.get("card_no") or "").strip():
            out["missing_card_no"].append(_label(r))
        if "ebay_item_id" in r and not (r.get("ebay_item_id") or "").strip():
            out["missing_item_id"].append(_label(r))
        if (r.get("dupe") or "").strip().upper() == "Y":
            out["trade_pile"].append(_label(r))
    front = sum((r.get("front_page") or "").strip().upper() == "Y" for r in live)
    if front != FRONT_PAGE:
        out["front_page"].append(f"{front} card{'s' if front != 1 else ''} marked front_page=Y; "
                                 f"the page holds {FRONT_PAGE}")
    return out


CHECK_TITLES = {"duplicates": "Possible duplicates", "stale": "Stale statuses",
                "year_mismatch": "Year mismatches", "missing_card_no": "No card number",
                "missing_item_id": "No eBay item id", "trade_pile": "Trade pile (dupe=Y)",
                "front_page": "Front page"}


def check_lines(found: dict, limit: int = 10) -> list[str]:
    """The check as Markdown lines, for the weekly report and the command line."""
    lines = []
    for key, title in CHECK_TITLES.items():
        rows = found.get(key) or []
        if not rows:
            continue
        lines.append(f"- **{title}** ({len(rows)})")
        lines += [f"  - {r}" for r in rows[:limit]]
        if len(rows) > limit:
            lines.append(f"  - and {len(rows) - limit} more")
    return lines or ["- Nothing to fix."]


SHORT = {"duplicates": "possible duplicates", "stale": "stale statuses",
         "year_mismatch": "year mismatches", "missing_card_no": "without a card number",
         "missing_item_id": "without an eBay item id", "trade_pile": "in the trade pile"}


def check_summary(found: dict) -> str:
    """One short line for a phone alert."""
    parts = [f"{len(found[k])} {label}" for k, label in SHORT.items() if found.get(k)]
    parts += found.get("front_page") or []
    return "Inventory check: " + ("; ".join(parts) if parts else "nothing to fix")
