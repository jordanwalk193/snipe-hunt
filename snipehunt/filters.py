"""Checks every lane runs on a listing before and after Claude sees it.

- Junk: reprints, customs, "you pick" listings and the like are dropped before
  scoring, so they cost nothing. The words are the profile's [search]
  junk_words, or JUNK_WORDS.
- Fake autographs: a title that says "auto" on a card that isn't signed
  (item specifics say Autographed: No, a printed or facsimile signature, or a
  Topps Chrome Platinum Anniversary base card) is kept but flagged, so it is
  never priced as an autograph.
- Too good from a new seller: little feedback and a price far under the going
  rate is how a scam looks. That needs the value, so it is checked on the pick.

Pure functions apart from reading the profile: nothing here calls eBay.
"""
from __future__ import annotations

import re

from . import aspects, profile

JUNK_WORDS = ["reprint", "rp", "custom", "digital", "facsimile", "proxy", "art card",
              "you pick", "pick your", "u pick", "complete your set"]
LOT = re.compile(r"\blots?\b(\s+of\b)?", re.I)
AUTO_WORDS = re.compile(r"\b(auto|autos|autograph|autographs|autographed|signed)\b", re.I)
PRINTED = re.compile(r"\b(facsimile|printed\s+signature|pre-?printed)\b", re.I)
PLATINUM = re.compile(r"\bplatinum\s+anniversary\b", re.I)
# An autograph card's own number (CPA-JW, PA-JW) or set name proves it is signed.
AUTO_NUMBER = re.compile(r"(?<![A-Z0-9])C?PA-[A-Z0-9]{1,5}\b", re.I)
AUTO_SET = re.compile(r"\bautographs\b", re.I)
NEW_SELLER_BELOW = 50     # feedback under this, and
TOO_GOOD = 0.40           # paying under this share of the going rate, looks like a scam
FAKE_AUTO_WHY = "printed signature, not an auto"


def _words_re(words: list[str]) -> re.Pattern:
    parts = [re.escape(w.strip()).replace(r"\ ", r"\s+") for w in words if w.strip()]
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I) if parts else re.compile("(?!x)x")


def junk_words() -> list[str]:
    who = profile.get()
    return JUNK_WORDS if who.junk_words is None else who.junk_words


def junk(title: str, words: list[str] | None = None, lots: bool | None = None) -> str | None:
    """The junk word in a title, or None. Lots count only when the profile turns them off."""
    found = _words_re(junk_words() if words is None else words).search(title)
    if found:
        return found.group(1).lower()
    if lots is None:
        lots = profile.get().lots
    if not lots and LOT.search(title):
        return "lot"
    return None


def fake_auto(title: str, description: str = "", specifics: dict | None = None,
              names: dict | None = None) -> str | None:
    """Why a listing that says "auto" isn't a real autograph, or None.

    `names` are eBay's item-specifics names (aspects.names()); looked up only
    when there are item specifics to read."""
    specifics = specifics or {}
    if specifics and names is None:
        names = aspects.names()
    names = names or aspects.FALLBACK
    said_auto = bool(AUTO_WORDS.search(title))
    signed = specifics.get(names["autographed"], "").strip().lower()
    if said_auto and signed == "no":
        return "item specifics say Autographed: No"
    printed = PRINTED.search(title) or PRINTED.search(description or "")
    if printed:
        return f'"{printed.group(1).lower()}"'
    if PLATINUM.search(title) and said_auto:
        card_set = specifics.get(names["set"], "")
        number = specifics.get(names["card_number"], "")
        if not (AUTO_NUMBER.search(title) or AUTO_NUMBER.search(number)
                or AUTO_SET.search(title) or AUTO_SET.search(card_set)):
            return "Platinum Anniversary base cards carry a printed signature"
    return None


def new_seller_risk(feedback: int | None, pay: float | None, value: float | None,
                    below: int = NEW_SELLER_BELOW) -> bool:
    """Little feedback and a price far under the going rate: treat it as a possible scam."""
    return bool(feedback is not None and feedback < below and pay and value
                and pay < TOO_GOOD * value)


def flag(item, names: dict | None = None) -> list[str]:
    """Set the listing's warning flags from what is known about it now; returns them."""
    why = fake_auto(item.title, item.short_description, item.aspects, names)
    if why and "fake_auto" not in item.flags:
        item.flags.append("fake_auto")
    return item.flags


def prefilter(items: list) -> tuple[list, dict[str, int]]:
    """(listings worth scoring, how many were dropped for each junk word).

    Fake autographs stay in, flagged; everything with a junk word goes."""
    words, lots = junk_words(), profile.get().lots
    kept, dropped = [], {}
    for item in items:
        word = junk(item.title, words, lots)
        if word:
            dropped[word] = dropped.get(word, 0) + 1
            continue
        flag(item)
        kept.append(item)
    return kept, dropped
