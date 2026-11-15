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


# Aftermarket autographs: signed in person or through the mail, not pulled from a
# pack. Worth the base card plus a few dollars, never a certified auto's price.
# Whole words only, and a hyphen counts as part of a word, so "IP-JW" (a card
# code), "IPA", "SHIPPING" and "TTMs" don't match.
_SEP = r"[\s\-]+"
# Third-party authenticators: a pack-pulled auto is certified by its maker, so a
# JSA, PSA/DNA or BAS signature was signed after the fact.
STRONG = ("aftermarket", "unauthenticated", f"not{_SEP}certified", "ip", "ttm",
          f"through{_SEP}the{_SEP}mail", f"signed{_SEP}in{_SEP}person", f"in{_SEP}person",
          f"no{_SEP}coa", "jsa", r"psa\s?[/\-]?\s?dna", "bas")
WEAK = (f"hand{_SEP}signed", "sharpie")   # real autos are hand signed too: only without proof
# Words that still mean aftermarket next to proof elsewhere in the listing.
ALWAYS = frozenset({"aftermarket", "unauthenticated"})
_AFTER = re.compile(r"(?<![\w-])(" + "|".join(STRONG) + r")(?![\w-])", re.I)
_WEAK = re.compile(r"(?<![\w-])(" + "|".join(WEAK) + r")(?![\w-])", re.I)
# "No IP autos here", "We do not deal in aftermarket or IP autos", "NOT an aftermarket
# auto": a negation earlier in the same clause, within NEGATION_WORDS words.
_CLAUSE = re.compile(r"[.!?;:,]|\bbut\b|\bhowever\b", re.I)
_NEGATION = re.compile(r"\b(no|not|never|isn'?t|aren'?t|don'?t|doesn'?t|won'?t|non)\b", re.I)
NEGATION_WORDS = 6
# "not certified by PSA": not graded, nothing to do with who signed it
_UNGRADED = re.compile(r"\s+(by|through|with)\s+(psa|bgs|sgc|cgc|beckett)\b", re.I)
PROOF = re.compile(r"certified\s+autograph\s+issue|\bon[\s-]card\b|\bsticker\s+auto|\bpack[\s-]pulled\b"
                   r"|\b(topps|bowman|panini|leaf|upper\s+deck|donruss|fanatics)\s+certified\b", re.I)
# An autograph card's own code, as the maker prints it: CPA-JW, BTPA-JW, CRA-JW.
AUTO_CODE = re.compile(r"(?<![A-Za-z0-9])(CPA|BCPA|BTPA|BDPA|BSPA|BCMA|BPA|BMA|CDA|CRA|CRDA|RA|"
                       r"FFA|TCA|PA|ROA|RAC|SSA|BA|TA|CA)-[A-Z]{2,4}\b")
MAKERS = frozenset({"topps", "bowman", "panini", "upper deck", "leaf", "manufacturer",
                    "fanatics", "donruss"})
AUTHENTICATION = "Autograph Authentication"   # eBay's item specific: who vouches for the signature
AFTERMARKET_WHY = "aftermarket signature (IP/TTM), not a certified auto"


def _norm(word: str) -> str:
    return re.sub(r"[\s\-/]+", " ", word.lower())


def auto_proof(title: str, description: str = "", specifics: dict | None = None) -> bool:
    """Something in the listing says the autograph is the maker's own: its certified
    autograph wording, on-card or sticker, pack pulled, an autograph card code, or the
    maker named as authenticator in item specifics. A bare "certified" doesn't count:
    "JSA certified" is how an in-person signature is sold."""
    if PROOF.search(title) or PROOF.search(description or ""):
        return True
    if AUTO_NUMBER.search(title) or AUTO_CODE.search(title):
        return True
    who = str((specifics or {}).get(AUTHENTICATION, "")).strip().lower()
    return any(m in who for m in MAKERS)


def _negated(text: str, start: int) -> bool:
    """A no/not/never earlier in the same clause, a few words before this word."""
    before = text[:start]
    cut = list(_CLAUSE.finditer(before))
    clause = before[cut[-1].end():] if cut else before
    return bool(_NEGATION.search(" ".join(clause.split()[-NEGATION_WORDS:])))


def aftermarket_auto(title: str, description: str = "", specifics: dict | None = None) -> str | None:
    """The word that makes this an aftermarket autograph (signed in person, by mail
    or authenticated by a third party), or None. "No IP autos here" doesn't count."""
    proof = auto_proof(title, description, specifics)
    texts = [title, description or ""] + [str(v) for v in (specifics or {}).values()]
    for text in texts:
        for m in _AFTER.finditer(text):
            word = _norm(m.group(1))
            if _negated(text, m.start()):
                continue
            if word == "not certified" and _UNGRADED.match(text, m.end()):
                continue
            if word in ALWAYS or not proof:
                return word
    if not proof:
        for text in texts:
            for m in _WEAK.finditer(text):
                if not _negated(text, m.start()):
                    return _norm(m.group(1))
    return None


def says_auto(pick: dict) -> bool:
    """The listing's title, or the scorer's name for the card, says autograph."""
    from . import sales   # sales imports ebay_search, which imports this; kept local
    return bool(sales.AUTO.search(pick.get("title") or "") or sales.AUTO.search(pick.get("card") or ""))


_RUN = re.compile(r"(?<!\d{4})/\s?(\d{1,4})\b")   # "2023/24" is a season, not a print run


def serial_unconfirmed(card: str, title: str, specifics: dict | None, text: str) -> str | None:
    """The print run ("99") the scorer's card name claims for an autograph that
    nothing the seller wrote backs up (title, item specifics, description), or None.
    Only autographs: a Gold /50 is numbered by definition, a raw title often leaves it
    out, and that hidden number is a real find."""
    from . import sales   # sales imports ebay_search, which imports this; kept local
    claimed = _RUN.search(card or "")
    if not claimed or not (sales.AUTO.search(card or "") or sales.AUTO.search(title or "")):
        return None
    run = claimed.group(1)
    seller = f"{title} {text or ''}"
    if re.search(rf"/\s?0*{run}\b|\b(of|to|numbered|#'?d)\s+(to\s+)?{run}\b", seller, re.I):
        return None
    if any(re.search(rf"(?<!\d)0*{run}(?!\d)", str(v)) for v in (specifics or {}).values()):
        return None
    return run


DOUBTS = ("ip_auto", "auto_unverified", "serial_unconfirmed")


def doubted(pick: dict) -> str | None:
    """Why this pick can't be a jackpot, bid-now, steal or alert, or None."""
    return next((d for d in DOUBTS if pick.get(d)), None)


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
    text = f"{item.short_description} {getattr(item, 'description', '')}"
    if "aftermarket_auto" not in item.flags and aftermarket_auto(item.title, text, item.aspects):
        item.flags.append("aftermarket_auto")
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
