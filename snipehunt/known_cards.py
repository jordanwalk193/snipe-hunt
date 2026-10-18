"""Which year and product a card number belongs to, and the usual mix-ups.

Read from data/known_cards.toml (see examples/known_cards.example.toml), plus
the card numbers and years in the inventory, plus (for the searches) the
numbers hidden.py has learned from titles. Nothing about a card is written in
Python: the file is the one place to correct it.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

from . import config

YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# BA-JW1, BHPC-52, FMF-10: letters, a dash, then the number or initials.
CODE = re.compile(r"(?<![A-Z0-9-])([A-Z0-9]{1,5}-[A-Z0-9]{1,5})(?![A-Z0-9-])")


FILE = "known_cards.toml"   # in the data folder, next to the profile; optional


def path():
    return config.DATA_DIR / FILE


def norm(number: str) -> str:
    """A card number without dashes, spaces or "#", in capitals: BAJW1."""
    return re.sub(r"[^A-Z0-9]", "", str(number or "").upper())


@dataclass
class Card:
    number: str
    year: int
    product: str = ""
    keywords: list[str] = field(default_factory=list)
    drift_years: list[int] = field(default_factory=list)
    parallels: list[str] = field(default_factory=list)
    autos: list[str] = field(default_factory=list)
    source: str = "file"     # file | inventory | learned


@dataclass
class Verdict:
    """What the table says about a title's year."""
    kind: str                # "match", "mismatch", "drift" or "unknown"
    number: str = ""
    title_year: int | None = None
    years: tuple[int, ...] = ()
    product: str = ""

    @property
    def year(self) -> int | None:
        """The card's real year when the table pins down just one."""
        return self.years[0] if len(self.years) == 1 else None


class KnownCards:
    def __init__(self, cards: list[Card] | None = None) -> None:
        self.by_number: dict[str, list[Card]] = {}
        for card in cards or []:
            self.add(card)

    def add(self, card: Card) -> None:
        mine = self.by_number.setdefault(norm(card.number), [])
        # the file outranks the inventory, which outranks what was learned from titles
        if card.source != "file" and any(c.source == "file" or c.year == card.year
                                         for c in mine):
            return
        mine.append(card)

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_number.values())

    def numbers_in(self, title: str) -> list[str]:
        """The card numbers in a title that the table knows, as printed."""
        return [c for c in dict.fromkeys(CODE.findall(title.upper()))
                if norm(c) in self.by_number]

    def resolve(self, number: str, title: str) -> list[Card]:
        """The products this number can be in this title.

        One product (or one year): that one. Several: only those whose keyword
        is in the title, and of those the longest keyword wins ("all-american"
        over "metal"). No keyword: nothing, the year is left undecided."""
        cards = self.by_number.get(norm(number), [])
        if len({c.year for c in cards}) <= 1:
            return cards
        low = title.lower()
        hits = [(max(len(k) for k in c.keywords if k.lower() in low), c)
                for c in cards if any(k.lower() in low for k in c.keywords)]
        if not hits:
            return []
        best = max(n for n, _ in hits)
        return [c for n, c in hits if n == best]

    def check(self, title: str) -> Verdict:
        """Does the title's year fit the card number in it?"""
        found = YEAR.search(title)
        title_year = int(found.group(1)) if found else None
        for number in self.numbers_in(title):
            cards = self.resolve(number, title)
            if not cards:
                continue
            years = tuple(sorted({c.year for c in cards}))
            product = " / ".join(dict.fromkeys(c.product for c in cards if c.product))
            if title_year is None or title_year in years:
                return Verdict("match", number, title_year, years, product)
            drift = {y for c in cards for y in c.drift_years}
            return Verdict("drift" if title_year in drift else "mismatch", number,
                           title_year, years, product)
        return Verdict("unknown", title_year=title_year)

    def year_for(self, title: str) -> tuple[int | None, str]:
        """(the card's year, where it came from: "known_cards" or "title")."""
        verdict = self.check(title)
        if verdict.year is not None:
            return verdict.year, "known_cards"
        return verdict.title_year, "title"


def _cards_from_file(text: str) -> list[Card]:
    raw = tomllib.loads(text)
    out = []
    for entry in raw.get("card", []) or []:
        numbers = entry.get("numbers") or [entry.get("number")]
        for number in numbers:
            if not number or not str(entry.get("year", "")).isdigit():
                continue
            out.append(Card(
                number=str(number), year=int(entry["year"]),
                product=str(entry.get("product", "")),
                keywords=[str(k) for k in entry.get("keywords", [])],
                drift_years=[int(y) for y in entry.get("drift_years", [])],
                parallels=[str(p) for p in entry.get("parallels", [])],
                autos=[str(a) for a in entry.get("autos", [])]))
    return out


def _cards_from_inventory() -> list[Card]:
    from . import inventory   # inventory reads listing code; kept local
    try:
        _, rows = inventory.read()
    except OSError:
        return []
    return [Card(number=r["card_no"].strip(), year=int(r["year"]), product=r.get("set", ""),
                 source="inventory")
            for r in rows if (r.get("card_no") or "").strip()
            and str(r.get("year", "")).strip().isdigit()
            and any(ch.isalpha() for ch in r["card_no"])]   # "209" alone names no product


def load(learned: list[tuple[str, str]] = (), with_inventory: bool = True) -> KnownCards:
    """The table: the file, then the inventory, then (year, number) pairs learned elsewhere.

    A number the file lists is never added to from the inventory or titles. The
    inventory check uses the file alone (`with_inventory=False`), or it would
    be checking the inventory against itself."""
    cards: list[Card] = []
    if path().exists():
        cards += _cards_from_file(path().read_text())
    table = KnownCards(cards)
    for card in _cards_from_inventory() if with_inventory else []:
        table.add(card)
    for year, number in learned:
        if str(year).isdigit():
            table.add(Card(number=number, year=int(year), source="learned"))
    return table
