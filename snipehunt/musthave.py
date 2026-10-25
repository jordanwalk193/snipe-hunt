"""The must-have list: the defining cards of each year (data/musthave.md).

For each card it says whether the collector owns it (from inventory.csv, by the
list's "Owned when" rule), how many copies are for sale right now and the
cheapest, and whether one of them is a live pick. Nothing here asks Claude.
Optional: without the file the list is empty.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from . import config, profile

FILE = config.DATA_DIR / "musthave.md"
# the best status first, when more than one row matches
STATUS_RANK = {"In binder": 0, "Delivered": 1, "In transit": 2, "Awaiting payment": 3,
               "Won": 4}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def read(path=None) -> list[dict]:
    """The list's rows, in file order."""
    path = path or FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and re.fullmatch(r"(19|20)\d\d", cells[0]):
            out.append({"year": cells[0], "card": cells[1], "why": cells[2], "rate": cells[3],
                        "search": cells[4], "owned_when": cells[5]})
    return out


def _has(term: str, tokens: set[str], squashed: str) -> bool:
    """Is one search word in a title? Plain words and numbers must be whole words
    ("18" is not in "2018"); a mixed one like "bd-57" may be written "BD57"."""
    if re.fullmatch(r"[a-z]+|\d+", term):
        return term in tokens
    return _squash(term) in squashed


def _name_words() -> set[str]:
    """The player's (or team's) name: titles misspell it, and the search already
    found it, so a title needn't repeat it word for word."""
    prof = profile.get()
    return {*_tokens(prof.search_name), *getattr(prof, "first_names", ()),
            *getattr(prof, "last_names", ())}


def matches(search: str, title: str) -> bool:
    """Does a listing title fit the list's eBay search (`-word` leaves it out)?"""
    tokens, squashed, name = set(_tokens(title)), _squash(title), _name_words()
    for term in search.lower().split():
        if term.startswith("-"):
            if _has(term[1:], tokens, squashed):
                return False
        elif term not in name and not _has(term, tokens, squashed):
            return False
    return True


def owners(card: dict, rows: list[dict]) -> list[dict]:
    """The inventory rows that count as owning this card, best status first."""
    parts = (card["owned_when"].split(";") + ["", "", ""])[:3]
    set_words, number, parallel = (p.strip() for p in parts)
    want = [w.lower() for w in set_words.split() if not w.startswith("-")]
    avoid = [w[1:].lower() for w in set_words.split() if w.startswith("-")]
    found = []
    for r in rows:
        if r.get("status") == "Sold-Traded" or r.get("year") != card["year"]:
            continue
        name = set(_tokens(f"{r.get('set')} {r.get('card')}"))
        if any(w not in name for w in want) or any(w in name for w in avoid):
            continue
        if number and _squash(r.get("card_no")) != _squash(number):
            continue
        if not _parallel_fits(parallel, r):
            continue
        found.append(r)
    return sorted(found, key=lambda r: STATUS_RANK.get(r.get("status"), 9))


def _parallel_fits(parallel: str, row: dict) -> bool:
    words = set(_tokens(f"{row.get('parallel')} {row.get('card')}"))
    for word in parallel.split():
        word = word.lower()
        if word.startswith("-"):
            if word[1:] in words:
                return False
        elif word == "base":
            if (row.get("parallel") or "Base").lower() != "base":
                return False
        elif word == "graded":
            if not row.get("graded"):
                return False
        elif word == "auto":
            if row.get("auto") != "Y" and "auto" not in words:
                return False
        elif word not in words:
            return False
    return True


def ebay_url(search: str) -> str:
    """An eBay search link, in the category the bot searches (none for "")."""
    chosen = profile.get().category
    category = config.get("EBAY_CATEGORY") if chosen is None else chosen
    return ("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(search)
            + (f"&_sacat={category}" if category else ""))


def view(rows: list[dict], listings: list[dict], picks: list[dict]) -> dict:
    """Every year's list, for the dashboard. `listings` are live listings
    ({"title", "total"}); `picks` are live picks, best first, with their titles."""
    years: dict[str, list] = {}
    for card in read():
        mine = owners(card, rows)
        selling = [x["total"] for x in listings if x.get("title")
                   and matches(card["search"], x["title"])]
        pick = next((p for p in picks if p.get("title")
                     and matches(card["search"], p["title"])), None)
        years.setdefault(card["year"], []).append({
            "card": card["card"], "why": card["why"], "rate": card["rate"],
            "url": ebay_url(card["search"]),
            "owned": {"id": mine[0]["id"], "status": mine[0].get("status")} if mine else None,
            "for_sale": len(selling),
            "cheapest": round(min(selling), 2) if selling else None,
            "pick": {"item_id": pick["item_id"], "tier": pick.get("tier"),
                     "deal": pick.get("deal"), "total": pick.get("total"),
                     "url": pick.get("url")} if pick else None,
        })
    out = [{"year": y, "cards": cards, "owned": sum(bool(c["owned"]) for c in cards)}
           for y, cards in sorted(years.items())]
    return {"years": out, "owned": sum(y["owned"] for y in out),
            "total": sum(len(y["cards"]) for y in out)}
