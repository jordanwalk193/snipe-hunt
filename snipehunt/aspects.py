"""eBay's own names for item specifics, read once a week.

Detectors need to know what eBay calls "is it signed" or "how many were made"
in this category. Rather than trust guesses, one search a week asks eBay for
the item-specifics names and values it really uses for the player's cards
(fieldgroups=ASPECT_REFINEMENTS) and keeps them in the kv table. Until the
first refresh, FALLBACK holds the names the README documents.

If a search ever filters on an item specific (aspect_filter), the category id
has to be in both category_ids and the aspect filter, or eBay ignores it.
"""
from __future__ import annotations

import json
import re
import sys
import time

from . import db, profile, usage

KEY = "ebay_aspects"
EVERY = 7 * 86400
MAX_VALUES = 40   # values kept per name, most common first
# What a detector asks for, and how to spot eBay's name for it among the cached names.
CONCEPTS = {
    "autographed": re.compile(r"^autograph(ed)?$", re.I),
    "autograph_format": re.compile(r"^autograph (format|type)$", re.I),
    "parallel": re.compile(r"^parallel(/variety)?$", re.I),
    "print_run": re.compile(r"^print run$", re.I),
    "card_number": re.compile(r"^card number$", re.I),
    "season": re.compile(r"^(season|year)$", re.I),
    "set": re.compile(r"^set$", re.I),
    "graded": re.compile(r"^graded$", re.I),
}
# Used only before the first weekly refresh: the names in the eBay category's docs.
FALLBACK = {"autographed": "Autographed", "autograph_format": "Autograph Format",
            "parallel": "Parallel/Variety", "print_run": "Print Run",
            "card_number": "Card Number", "season": "Season", "set": "Set",
            "graded": "Graded"}


def cached(con) -> dict:
    return json.loads(db.kv_get(con, KEY, "{}") or "{}")


def name_for(concept: str, con=None) -> str:
    """eBay's item-specifics name for a concept, from the cache when it has one."""
    try:
        names = cached(con or db.connect()).get("names", {})
    except Exception:
        names = {}
    pattern = CONCEPTS[concept]
    return next((n for n in names if pattern.match(n)), FALLBACK[concept])


def values_for(concept: str, con=None) -> list[str]:
    """The values eBay has shown for that name, most common first (empty before a refresh)."""
    try:
        names = cached(con or db.connect()).get("names", {})
    except Exception:
        return []
    return names.get(name_for(concept, con), [])


def names(con=None) -> dict[str, str]:
    """Every concept's eBay name, for detectors that read several at once."""
    return {concept: name_for(concept, con) for concept in CONCEPTS}


def due(con, now: float | None = None) -> bool:
    return (now or time.time()) - cached(con).get("at", 0) >= EVERY


def refresh(con, client=None, now: float | None = None) -> int:
    """One search for the player, keeping eBay's item-specifics names and values.

    Returns how many names were saved; 0 when it wasn't due or couldn't run."""
    now = now or time.time()
    if not due(con, now) or not usage.ebay_allowed("aspects", con):
        return 0
    from .ebay_search import EbayClient   # it imports usage, which this module imports
    client = client or EbayClient(con)
    try:
        found = client.refinements(profile.get().search_name)
    except Exception as exc:   # next week will try again
        print(f"aspects: {exc}", file=sys.stderr)
        return 0
    out = {}
    for dist in found:
        name = str(dist.get("localizedAspectName") or "").strip()
        if not name:
            continue
        values = sorted(dist.get("aspectValueDistributions") or [],
                        key=lambda v: -int(v.get("matchCount") or 0))
        out[name] = [str(v.get("localizedAspectValue")) for v in values[:MAX_VALUES]
                     if v.get("localizedAspectValue")]
    db.kv_set(con, KEY, json.dumps({"at": now, "names": out}))
    con.commit()
    return len(out)
