"""Read and update inventory.csv without disturbing existing rows."""
from __future__ import annotations

import csv
import io

from . import config

# Statuses only ever move forward along this line.
ORDER = ["Won", "Awaiting payment", "In transit", "Delivered", "In binder"]
FROZEN = {"In binder", "Sold-Traded"}


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
