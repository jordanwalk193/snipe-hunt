"""Searches that don't use the player's name.

A card listed without the player's name is the easiest one to win: nobody
searching for the player ever sees it. But the listing still names the card:
"2020 Bowman Draft BD-57". So this learns the player's card numbers from the
titles of listings it has already seen, then searches for the card numbers and
keeps only the listings that leave the name out. Claude decides whether each
one really is the player's card.

Read-only, like every other search.
"""
from __future__ import annotations

import sys
import time
from collections import Counter

from . import ebay_search, known_cards, profile, usage
from .ebay_search import ALL_FORMATS, EbayClient, Listing
from .known_cards import CODE, YEAR   # one definition of a card number, shared

PER_RUN = 6        # eBay calls per hourly run; the card numbers take turns
MIN_SEEN = 3       # a card number has to turn up this often to be trusted
MAX_CODES = 24
LOOK_BACK_DAYS = 120


def card_numbers(con, now: float | None = None) -> list[tuple[str, str]]:
    """(year, card number) pairs seen often on the player's listings, most common first."""
    now = now or time.time()
    who = profile.get()
    initials = "".join(word[0] for word in who.player.split()).upper()
    counts: Counter = Counter()
    for row in con.execute("SELECT title FROM seen WHERE last_seen>?",
                           (now - LOOK_BACK_DAYS * 86400,)):
        title = row["title"] or ""
        if who.classify(title) != "yes":
            continue
        year = YEAR.search(title)
        if not year:
            continue
        for code in set(CODE.findall(title.upper())):
            # a real card number has a digit, or ends in the player's initials (CDA-JW)
            if any(ch.isdigit() for ch in code) or code.split("-")[1].startswith(initials):
                counts[(year.group(1), code)] += 1
    learned = [pair for pair, n in counts.most_common(MAX_CODES) if n >= MIN_SEEN]
    # and the numbers known_cards.toml pins to one year, which need no learning
    table = known_cards.load(with_inventory=False)
    filed = [(str(cards[0].year), cards[0].number.upper())
             for cards in table.by_number.values() if len({c.year for c in cards}) == 1]
    return list(dict.fromkeys(learned + filed))[:MAX_CODES]


def search(con, known: set[str], client: EbayClient | None = None,
           now: float | None = None, verbose: bool = False) -> list[Listing]:
    """Listings of the player's card numbers that leave the player's name out."""
    now = now or time.time()
    pairs = card_numbers(con, now)
    if not pairs:
        return []
    who = profile.get()
    turn = int(now // 3600)
    mine = [pairs[(turn * PER_RUN + n) % len(pairs)] for n in range(min(PER_RUN, len(pairs)))]
    client = client or EbayClient()
    client.lane = "hidden"
    found: dict[str, Listing] = {}
    for year, code in dict.fromkeys(mine):
        if not usage.ebay_allowed("hidden"):
            print("hidden: daily eBay call budget reached, skipped", file=sys.stderr)
            break
        try:
            raw_items = client.search(f"{year} {code}", ALL_FORMATS, "newlyListed", limit=50)
        except Exception as exc:   # one bad search shouldn't stop the hunt
            print(f"hidden: {year} {code}: {exc}", file=sys.stderr)
            continue
        for raw in raw_items:
            item = ebay_search.parse_item(raw)
            if item is None or item.currency != "USD" or item.item_id in known:
                continue
            title = item.title.upper()
            if code not in CODE.findall(title) or year not in title:
                continue
            if who.classify(item.title) != "no" or who._wrong_re.search(item.title):
                continue   # the name is there (the usual searches have it) or it is ruled out
            item.match = "unclear"
            item.queries.append(f"no-name {year} {code}")
            found.setdefault(item.item_id, item)
    if verbose:
        print(f"  hidden: {len(mine)} card numbers searched, {len(found)} listings "
              "without the player's name", file=sys.stderr)
    return list(found.values())
