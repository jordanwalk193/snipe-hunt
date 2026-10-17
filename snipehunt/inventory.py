"""Read and update inventory.csv without disturbing existing rows."""
from __future__ import annotations

import csv
import io
import re

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
    sellers = {r.get("seller") for r in rows if r.get("status") in UNPAID}
    settled = {str(e.get("item_id")) for e in events
               if e.get("type") in ("paid", "shipped", "delivered")}
    sellers |= {e.get("seller") for e in events if e.get("type") in ("won", "offer_accepted")
                and str(e.get("item_id")) not in settled}
    return {s.strip().lower() for s in sellers if s and s.strip()}


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
