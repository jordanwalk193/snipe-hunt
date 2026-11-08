"""Vague titles, identified from eBay's item specifics.

"Jordan Walker PRIZM Rookie Draft Picks Power Surge RC" names no year and no set,
so nobody searching for the card finds it, Claude can't price it, and the ledger
has no comps for it: on one collector's record, 94% of such auctions closed with
one bid or none. eBay's item specifics usually say exactly what it is (Set, Card
Number, Year, Parallel/Variety, Features), one read-only item call away.

For each vague, affordable listing this appends the identity to the title in
square brackets ("... [2020 Panini Prizm Draft Picks #PDP169, Rookie]"), so the
title Claude reads, the sales ledger, the inventory match and the dashboard's
labels all see the card. The listing's search labels get "specifics", which
gives its picks the VAGUE label. Each item is looked up once (table
`specifics`), at most PER_RUN a run, counted in eBay's "item" lane.
"""
from __future__ import annotations

import re
import sys
import time

from . import rarity, usage

PER_RUN = 12            # item calls per hourly run; the rest wait for the next run
KEEP_DAYS = 120         # a looked-up identity (or the lack of one) is kept this long
LANE = "item"           # the eBay lane these calls count in (usage.py)
YEAR = re.compile(r"\b(19|20)\d\d\b")
# A title with one of these names its set well enough for buyers to find it.
SET = re.compile(r"\b(topps|bowman|panini|leaf|donruss|prizm|select|chrome|heritage|archives|"
                 r"stadium club|finest|optic|now|sapphire|inception|pristine|tier one|dynasty|"
                 r"immaculate|flawless|contenders|elite|perfect game|cosmic|gypsy|ginter|"
                 r"big league|holiday|japan|update|series|sterling|platinum|trinity|metal|"
                 r"bowman's best|bowmans best|mosaic|absolute|chronicles|diamond kings|"
                 r"triple threads|five star|museum|tribute|transcendent|definitive|living|"
                 r"upper deck|fleer|score|hoops|skybox|pokemon|lorcana|magic)\b", re.I)
# Values eBay sellers leave as "Does not apply", "N/A" or "No" say nothing.
NOTHING = re.compile(r"^(n/?a|none|no|not applicable|does not apply|unknown|-)$", re.I)
BASE = re.compile(r"^(base|base set|base card|standard|regular)$", re.I)   # not an identity
LOT = re.compile(r"\(\d+\)|\b\d+\s?x\b|\bx\s?\d+\b|\b\d+\s?cards?\b", re.I)  # "(10)", "4x", "900+ card"


def vague(title: str) -> bool:
    """Does this title leave out the year or the set? Lots and customs don't count:
    item specifics describe one card."""
    title = title or ""
    if rarity.NOT_A_CARD.search(title) or LOT.search(title):
        return False
    return not YEAR.search(title) or not SET.search(title)


def identity(raw: dict | None) -> str | None:
    """The card as eBay's item specifics describe it, or None when they say nothing."""
    if not raw:
        return None
    asp = {}
    for a in raw.get("localizedAspects") or []:
        name = str(a.get("name") or "")
        value = str(a.get("value") or "").strip().strip("[]").strip()   # "[Base]" is Base
        if value and not NOTHING.match(value) and not BASE.match(value):
            asp.setdefault(name, value)
    year = asp.get("Year Manufactured") or asp.get("Season") or ""
    year = year if YEAR.fullmatch(year.strip()) else ""
    card_set = asp.get("Set") or ""
    maker = asp.get("Manufacturer") or ""
    if maker and card_set and maker.lower() not in card_set.lower():
        card_set = f"{maker} {card_set}"
    if year and card_set and not card_set.startswith(year):
        card_set = f"{year} {card_set}"
    number = asp.get("Card Number") or ""
    if not card_set and not number:
        return None
    parts = [card_set or year]
    if number:
        parts.append(f"#{number.lstrip('#')}")
    extras = []
    parallel = asp.get("Parallel/Variety") or ""
    if parallel:
        extras.append(parallel)
    features = asp.get("Features") or ""
    for f in re.split(r",\s*", features):
        f = f.strip().strip("[]")
        if f and f.lower() != "parallel/variety" and not BASE.match(f) and f not in extras:
            extras.append(f)
    if (asp.get("Autographed") or "").lower() == "yes" and not any("auto" in e.lower() for e in extras):
        extras.append("Autograph")
    grader = asp.get("Professional Grader") or asp.get("Grader") or ""
    grade = asp.get("Grade") or ""
    if grader and grade:
        extras.append(f"{grader} {grade}")
    text = " ".join(p for p in parts if p)
    return text + (", " + ", ".join(extras) if extras else "")


def _known(con, item_id: str, now: float) -> tuple[bool, str | None]:
    row = con.execute("SELECT identity, checked_at FROM specifics WHERE item_id=?",
                      (item_id,)).fetchone()
    if row is None or row["checked_at"] < now - KEEP_DAYS * 86400:
        return False, None
    return True, row["identity"]


def apply(item, ident: str | None) -> bool:
    """Put the identity on the listing's title; False when there is nothing to add."""
    if not ident or "[" in item.title or ident.lower() in item.title.lower():
        return False
    item.title = f"{item.title} [{ident}]"
    if "specifics" not in item.queries:
        item.queries.append("specifics")
    return True


def _affordable(item) -> bool:
    """Steals mode's cheap filter, when it is on: no point identifying a $200 card."""
    from . import steals   # steals imports the pricing side; kept local
    cfg = steals.profile_config() if steals.on() else None
    return cfg is None or steals.too_dear(item, cfg) is None


def enrich(con, listings: list, client=None, now: float | None = None,
           limit: int = PER_RUN) -> int:
    """Identify the vague, affordable listings among these; returns how many got an
    identity. Looks up at most `limit` new items with eBay this run."""
    from .ebay_search import EbayClient   # ebay_search imports settings; kept local
    now = now or time.time()
    done, calls = 0, 0
    for item in listings:
        if item.match == "no" or item.currency != "USD" or not vague(item.title):
            continue
        if not _affordable(item):
            continue
        known, ident = _known(con, item.item_id, now)
        if not known:
            if calls >= limit or not usage.ebay_allowed(LANE, con):
                continue
            client = client or EbayClient()
            calls += 1
            try:
                ident = identity(client.item(item.item_id, lane=LANE))
            except Exception as exc:   # one bad item shouldn't stop the hunt
                print(f"specifics: {item.item_id}: {exc}", file=sys.stderr)
                continue
            con.execute("INSERT OR REPLACE INTO specifics (item_id, identity, checked_at) "
                        "VALUES (?,?,?)", (item.item_id, ident, int(now)))
        if apply(item, ident):
            done += 1
    return done
