"""Signs a listing is mislabeled: the seller got something wrong that buyers search for.

A misspelled name, an autograph or parallel that only the item specifics
mention, the wrong year, the wrong category. Each detector looks at one
listing and returns a Signal, or None. Pure functions: no eBay, no Claude, no
database. The mid-tier lane (midtier.py) runs them; the fake-autograph and
junk-word checks are shared with every lane (filters.py).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import aspects, filters, profile
from .ebay_search import Listing
from .known_cards import KnownCards
from .profile import typos

ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
SINGLES = "261328"           # Sports Trading Card Singles
TITLE_AUTO = re.compile(r"\b(auto|autos|autograph|autographed|signed|sig)\b", re.I)
SIGNED_FORMATS = {"hard signed", "on-card", "on card", "sticker"}
NOT_PARALLEL = {"", "base", "[base]", "n/a", "none", "no"}
SERIAL = re.compile(r"\b(\d{1,3})\s?/\s?(\d{1,4})\b")
# Words a parallel name can share with plain titles; the rest of the name must show.
GENERIC = {"parallel", "refractor", "variation", "foil", "prizm", "card", "insert", "the", "and"}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIMING = {"odd_end_time"}    # signals that can tip a close call but never make one
# What the detectors default to without a [search.midtier] block (midtier.DEFAULTS).
DEFAULTS = {"odd_hours_et": [1, 6], "odd_weekdays": ["Mon", "Tue", "Wed", "Thu"],
            "thin_listing_max_title_tokens": 7, "new_seller_feedback_below": 50}


@dataclass
class Signal:
    name: str
    weight: int
    positive: bool
    why: str

    def to_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        return f"{self.name}({self.why})"


def typo_name(item: Listing) -> Signal | None:
    """The player's name misspelled ("Jordon Walker"), and never spelled right."""
    who = profile.get()
    names = who.player.lower().split()
    if len(names) < 2:
        return None
    first, last = names[0], names[-1]
    if re.search(rf"\b{re.escape(first)}\s+{re.escape(last)}\b", item.title, re.I):
        return None
    firsts = {first, *typos(first), *[f for f in who.first_names if len(f) >= 3]}
    lasts = {last, *typos(last), *[x for x in who.last_names if len(x) >= 3]}
    words = re.findall(r"[A-Za-z]+", item.title)
    for a, b in zip(words, words[1:]):
        if a.lower() in firsts and b.lower() in lasts:
            wrong = [w for w, right in ((a, first), (b, last)) if w.lower() != right]
            if wrong:
                return Signal("typo_name", 3, True, '"' + " ".join(wrong) + '"')
    return None


def hidden_auto(item: Listing, names: dict) -> Signal | None:
    """Item specifics say signed; the title never says auto."""
    if "fake_auto" in item.flags or TITLE_AUTO.search(item.title):
        return None
    signed = item.aspects.get(names["autographed"], "").strip().lower() == "yes"
    form = item.aspects.get(names["autograph_format"], "").strip().lower()
    if signed or form in SIGNED_FORMATS:
        what = f"{names['autograph_format']}: {item.aspects[names['autograph_format']]}" if (
            form in SIGNED_FORMATS) else f"{names['autographed']}: Yes"
        return Signal("hidden_auto", 3, True, f"{what} in specifics, not title")
    return None


def facsimile_guard(item: Listing, names: dict) -> Signal | None:
    """Says auto, isn't one (or is a reprint, custom or proxy). Holds the alert back."""
    junk = filters.junk(item.title, lots=True)
    why = filters.fake_auto(item.title, item.short_description, item.aspects, names) or (
        f'"{junk}" in title' if junk else None)
    return Signal("facsimile_guard", -5, False, why) if why else None


def _denominator(text: str) -> str | None:
    found = SERIAL.search(text or "")
    if found:
        return found.group(2)
    digits = re.findall(r"\d{1,4}", text or "")
    return digits[-1] if digits else None


def hidden_parallel(item: Listing, names: dict) -> Signal | None:
    """A print run or parallel in the specifics or description that the title leaves out."""
    title = item.title.lower()
    run = item.aspects.get(names["print_run"], "").strip()
    found = run if run.lower() not in NOT_PARALLEL else ""
    if not found:
        for text in [*item.aspects.values(), item.short_description]:
            serial = SERIAL.search(text or "")
            if serial and serial.group(0) != item.aspects.get(names["card_number"], ""):
                found = serial.group(0)
                break
    den = _denominator(found) if found else None
    if den and den != "1" and not re.search(rf"/\s?0*{den}\b", title):
        return Signal("hidden_parallel", 2, True, f"/{den} in specifics, not title")
    if den == "1" and not re.search(r"\b1\s?/\s?1\b|\b1 of 1\b|superfractor", title):
        return Signal("hidden_parallel", 2, True, "1/1 in specifics, not title")
    parallel = item.aspects.get(names["parallel"], "").strip()
    if parallel.lower() not in NOT_PARALLEL:
        words = [w for w in re.findall(r"[a-z0-9]+", parallel.lower()) if w not in GENERIC]
        words = words or re.findall(r"[a-z0-9]+", parallel.lower())
        if any(w not in title for w in words):
            return Signal("hidden_parallel", 2, True, f'"{parallel}" in specifics, not title')
    return None


def year_signal(item: Listing, known: KnownCards) -> Signal | None:
    """The title's year doesn't fit its card number (known_cards.toml)."""
    verdict = known.check(item.title)
    years = "/".join(str(y) for y in verdict.years)
    if verdict.kind == "mismatch":
        return Signal("year_set_mismatch", 2, True,
                      f"{verdict.number} is {years} {verdict.product}, title says "
                      f"{verdict.title_year}")
    if verdict.kind == "drift":
        return Signal("year_drift", 1, True,
                      f"{verdict.number} is {years}, often listed as {verdict.title_year}")
    return None


def wrong_category(item: Listing) -> Signal | None:
    if item.category_id and item.category_id != SINGLES:
        return Signal("wrong_category", 1, True, f"listed in category {item.category_id}")
    return None


def thin_listing(item: Listing, cfg: dict, names: dict,
                 value: float | None = None) -> Signal | None:
    """Little effort went into the listing: few photos, a short title, no card number.

    The same listing from a new seller at a price far under its value is how a
    scam looks, so then it counts against the listing instead."""
    pay = item.price + (item.shipping or 0)
    if filters.new_seller_risk(item.seller_feedback_score, pay, value,
                               cfg["new_seller_feedback_below"]):
        return Signal("too_good_new_seller", -3, False,
                      f"{item.seller_feedback_score} feedback, under 40% of value")
    why = []
    if item.image_count is not None and item.image_count <= 1:
        why.append("1 photo")
    if len(item.title.split()) <= cfg["thin_listing_max_title_tokens"]:
        why.append(f"{len(item.title.split())}-word title")
    if item.aspects and names["card_number"] not in item.aspects:
        why.append("no card number")
    return Signal("thin_listing", 1, True, ", ".join(why)) if why else None


def end_et(item: Listing) -> datetime | None:
    """When the auction ends, in Eastern time whatever the bot's own time zone."""
    if not item.end_time:
        return None
    return datetime.fromisoformat(item.end_time).astimezone(ET)


def odd_end_time(item: Listing, cfg: dict) -> Signal | None:
    """An auction ending while the country sleeps. A tiebreaker only (see TIMING)."""
    end = end_et(item) if item.is_auction else None
    if end is None:
        return None
    start, last = cfg["odd_hours_et"]   # both hours included: [1, 6] is 1:00 to 6:59
    day = DAYS[end.weekday()]
    if (day in cfg["odd_weekdays"] and start <= end.hour <= last) or end.hour < 7:
        return Signal("odd_end_time", 1, True, f"{day} {end.strftime('%-I %p')} ET")
    return None


def title_signals(item: Listing, known: KnownCards, cfg: dict | None = None) -> list[Signal]:
    """The detectors that need only what a search returns (no item specifics yet)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    names = aspects.FALLBACK
    found = [typo_name(item), year_signal(item, known), wrong_category(item),
             facsimile_guard(item, names), odd_end_time(item, cfg)]
    return [s for s in found if s]


def detect(item: Listing, known: KnownCards, cfg: dict | None = None,
           value: float | None = None, names: dict | None = None) -> list[Signal]:
    """Every detector, once the item specifics have been read (ebay_search.enrich)."""
    cfg = {**DEFAULTS, **(cfg or {})}
    names = names or aspects.names()
    found = [typo_name(item), hidden_auto(item, names), facsimile_guard(item, names),
             hidden_parallel(item, names), year_signal(item, known), wrong_category(item),
             thin_listing(item, cfg, names, value), odd_end_time(item, cfg)]
    return [s for s in found if s]


def net_weight(signals: list[Signal]) -> int:
    return sum(s.weight for s in signals)


def qualifies(signals: list[Signal], forgive_facsimile: bool = False) -> bool:
    """Enough to call a listing mislabeled: more for than against, and at least one
    sign that isn't just the end time. `forgive_facsimile`: the card is priced from
    real sales of the non-auto card, so a fake autograph no longer counts against it."""
    counted = [s for s in signals if not (forgive_facsimile and s.name == "facsimile_guard")]
    return net_weight(counted) > 0 and any(s.positive and s.name not in TIMING for s in counted)


def as_signals(rows: list[dict]) -> list[Signal]:
    return [Signal(**r) for r in rows]
