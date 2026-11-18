"""Sold prices the bot saw for itself.

eBay blocks its sold-listings pages, so most prices used to be estimates. But
the Browse API still answers for an auction after it ends, with the final bid
and whether it sold. Every auction the search has seen is looked up once after
it closes and the result is kept here, so scoring can price a card from what
the same card really went for.

A Buy It Now that takes no offers can only sell at its listed price, so once
one drops out of the search it is looked up too and, if it sold, kept as a
fixed-price sale. A listing that takes offers is never recorded: the price it
really went for isn't shown.

Read-only, like the rest of the eBay code: this only ever reads one listing.
"""
from __future__ import annotations

import functools
import json
import math
import re
import statistics
import sys
import time
from collections.abc import Collection
from datetime import datetime, timezone
from functools import lru_cache

from . import config, db, profile, settings, usage
from .hidden import CODE, YEAR
from .ebay_search import EbayClient, Listing, _money

PER_RUN = 60            # look-ups per hourly run; the rest wait for the next one
SETTLE = 10 * 60        # seconds after the end before asking, so eBay has the result
GONE_AFTER = 3 * 3600   # a Buy It Now, or a listing from before end times, not seen this long
BIN_RECHECK = 24 * 3600  # a Buy It Now still for sale is asked about again after this
BIN_GIVE_UP = 30 * 86400  # and no longer once it has been out of the search this long
# Sold prices are never thrown away: the ledger is what the bot learns from, and a
# comp from two years ago still says what a card is worth when nothing newer exists.
PER_LISTING = 3         # sold prices offered to Claude for one listing
MAX_LINES = 60          # and for one scoring call, because every line is paid for
HINTS_PER_LISTING = 1   # of those, sales of another grade or print run of the card
# The share of words two titles must have in common is the setting min_overlap.
HINT = " (different grade/parallel)"
UNKNOWN_SHIPPING = 5.00
TRACK_CALLS = 30        # the latest calls per price band the track record looks at
TRACK_MIN = 8           # fewer than this in a band says nothing yet
# How far off a band's going rates must be to get a mention is the setting track_off.
MISSED_KEY = "missed_steals"
MISSED_UNDER = 35       # percent under the card's typical price that makes a sale a steal
MISSED_PRIOR = 2        # earlier sales of the same card needed to call a price typical
MISSED_DAYS = 30        # how far back sales are checked for steals the bot passed on
MISSED_KEEP = 40
# Cheap cards and dear ones miss in different directions, so the going rate is
# checked per band of the pick's value: (from, up to, how the prompt names it).
BANDS = ((0, 10, "under $10"), (10, 50, "$10 to $50"), (50, None, "over $50"))
# "#42" or "#US-123", but not the copy number in "#07/50".
HASH_NUMBER = re.compile(r"#\s?([A-Z]{0,4}-?\d{1,4}[A-Z]?)\b(?!\s?/\s?\d)", re.I)
# US123 or HMT45 written without a dash. Capitals only, so "Lot12" isn't one.
BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9/#-])([A-Z]{1,4}\d{2,4})(?![A-Za-z0-9])")
# Letters in front of a number that make it a grade or a count, not a card number.
NOT_NUMBER = {"PSA", "BGS", "SGC", "CGC", "CSG", "HGA", "TAG", "GEM", "MT", "MINT", "NM",
              "LOT", "AUTO", "QTY", "GMA", "PGI", "TAG", "HGA", "AGS", "ISA", "KSA"}
# Words that say nothing about which card it is.
FILLER = {"the", "and", "card", "cards", "baseball", "football", "basketball", "hockey",
          "mlb", "nfl", "nba", "nhl", "rookie", "rc", "sp", "free", "shipping", "ship",
          "mint", "nm", "nrmt", "qty", "lot", "invest", "hot", "look", "wow", "read"}


def words(title: str) -> frozenset[str]:
    """The words of a title that identify the card, without the player's name or team.

    On a team bot only the team's words go: the player's name is what tells two
    cards from the same set apart."""
    who = profile.get()
    return _words(title, _name_words(who.player, who.search_name, tuple(who.team_words)))


TITLE_CACHE = 16384   # titles whose words are kept: the ledger compares every sale with every other


@lru_cache(maxsize=64)
def _name_words(player: str, search_name: str, team_words: tuple) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", " ".join([player, search_name, *team_words]).lower()))


@lru_cache(maxsize=TITLE_CACHE)
def _words(title: str, name: frozenset[str]) -> frozenset[str]:
    """words() for one profile's name words; kept, since the same titles are read
    over and over (sales.history, ledger.groups and find_missed pair every sale)."""
    found = re.findall(r"[a-z0-9]+", title.lower().replace("'", ""))
    return frozenset(w for w in found if w not in name and w not in FILLER)


def _due(con, now: float) -> list:
    """Closed auctions first, then listings from before end times were kept, then
    Buy It Nows that have dropped out of the search."""
    return con.execute(
        "SELECT item_id, title, shipping FROM seen WHERE closed_at IS NULL"
        " AND (tracked IS NULL OR tracked=1) AND ("
        " (is_auction=1 AND end_ts IS NOT NULL AND end_ts<?)"
        " OR (is_auction IS NULL AND last_seen<?)"
        " OR (is_auction=0 AND last_seen<? AND last_seen>?"
        "     AND (checked_at IS NULL OR checked_at<?))) "
        "ORDER BY COALESCE(is_auction, 1)=0, end_ts IS NULL, end_ts, last_seen DESC LIMIT ?",
        (now - SETTLE, now - GONE_AFTER, now - GONE_AFTER, now - BIN_GIVE_UP,
         now - BIN_RECHECK, PER_RUN)).fetchall()


def _shipping(raw: dict) -> float | None:
    for opt in raw.get("shippingOptions") or []:
        shipping, _ = _money(opt.get("shippingCost"))
        if shipping is not None:
            return shipping
    return None


def _outcome(raw: dict, now: float) -> tuple[str, dict]:
    """What one eBay answer means: 'sold', 'unsold', 'running', 'live' or 'skip'."""
    end = raw.get("itemEndDate")
    end_ts = (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
              if end else None)
    options = raw.get("buyingOptions") or []
    sold = sum(int(a.get("estimatedSoldQuantity") or 0)
               for a in raw.get("estimatedAvailabilities") or [])
    if "AUCTION" not in options:
        # An accepted offer hides the real price; a plain fixed price can't.
        if "FIXED_PRICE" not in options or "BEST_OFFER" in options:
            return "skip", {}
        over = end_ts is not None and end_ts <= now
        price, currency = _money(raw.get("price"))
        if not sold:
            return ("unsold" if over else "live"), {}
        if price is None or currency != "USD":
            return "skip", {}
        return "sold", {"price": price, "shipping": _shipping(raw), "bids": None,
                        "end_ts": end_ts if over else now, "kind": "bin",
                        "image": (raw.get("image") or {}).get("imageUrl")}
    if end_ts is None or end_ts > now:
        return "running", {"end_ts": end_ts}
    price, currency = _money(raw.get("currentBidPrice") or raw.get("price"))
    # No bids but "sold" is an offer the seller took; its price isn't shown.
    if not sold or not raw.get("bidCount") or price is None or currency != "USD":
        return "unsold", {}
    return "sold", {"price": price, "shipping": _shipping(raw), "end_ts": end_ts,
                    "bids": raw.get("bidCount"), "kind": "auction",
                    "image": (raw.get("image") or {}).get("imageUrl")}


def collect(con, client: EbayClient | None = None, now: float | None = None) -> int:
    """Look up closed listings and keep what the sold ones went for. Returns sales added."""
    now = now or time.time()
    rows = _due(con, now)
    if not rows:
        return 0
    client = client or EbayClient(con)
    added = 0
    for row in rows:
        if not usage.ebay_allowed("item", con):
            break   # the day's eBay calls are spent; the rest wait for tomorrow
        try:
            raw = client.item(row["item_id"])
        except Exception as exc:   # leave it for the next run
            print(f"sales: {row['item_id']}: {exc}", file=sys.stderr)
            continue
        kind, info = _outcome(raw, now) if raw else ("skip", {})
        if kind == "running":   # now that the end is known it is asked again after it
            con.execute("UPDATE seen SET is_auction=1, end_ts=? WHERE item_id=?",
                        (info["end_ts"], row["item_id"]))
            con.commit()
            continue
        if kind == "live":   # a Buy It Now still for sale: look again another day
            con.execute("UPDATE seen SET is_auction=0, checked_at=? WHERE item_id=?",
                        (int(now), row["item_id"]))
            con.commit()
            continue
        if kind == "sold":
            shipping = info["shipping"] if info["shipping"] is not None else row["shipping"]
            con.execute(
                "INSERT OR REPLACE INTO sales (item_id, title, price, shipping, bids, sold_at,"
                " image, kind) VALUES (?,?,?,?,?,?,?,?)",
                (row["item_id"], raw.get("title") or row["title"], info["price"],
                 shipping, info["bids"], int(info["end_ts"]), info["image"], info["kind"]))
            added += 1
        con.execute("UPDATE seen SET closed_at=? WHERE item_id=?", (int(now), row["item_id"]))
        con.commit()   # one at a time, so a dashboard button never waits on this loop
    return added


@lru_cache(maxsize=TITLE_CACHE)   # a pure function of the title, asked for per pair
def card_number(title: str) -> str | None:
    """The card's number as printed, without "#" or dashes: BCP-236, #42, US123, TN-12."""
    for code in CODE.findall(title.upper()):   # the same pattern the no-name search uses
        left, right = code.split("-")
        if (any(ch.isdigit() for ch in right) and any(ch.isalpha() for ch in left)
                and left not in NOT_NUMBER):
            return left + right
    found = HASH_NUMBER.search(title)
    if found:
        return found.group(1).upper().replace("-", "")
    for token in BARE_NUMBER.findall(title):
        if token.rstrip("0123456789") not in NOT_NUMBER:
            return token
    return None


def _number_match(a: str, b: str) -> bool | None:
    """True for the same card number, False for different ones, None when one is missing."""
    one, two = card_number(a), card_number(b)
    if not one or not two:
        return None
    if one == two:
        return True
    # "#236" on one listing and "BCP-236" on the other is the same card
    short, long = sorted((one, two), key=len)
    return short.isdigit() and long.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") == short


@lru_cache(maxsize=TITLE_CACHE)
def _year(title: str) -> str | None:
    found = YEAR.search(title)
    return found.group(1) if found else None


# Separate prints of the same card number. A PSA 10 paper 2020 Bowman Draft sells for
# a third of the Chrome one with the same number, so no title with one of these may
# price a title without it. This is the one list: every matcher goes through
# different_product(). (ledger.SET_WORDS names whole products; these are only the
# ones that reprint another product's numbers: paper/Chrome, Chrome/Sapphire,
# Donruss/Optic, and Bowman's 1st Edition, a paper print of its own.)
PRODUCT_WORDS = frozenset({"chrome", "paper", "sapphire", "optic"})
FIRST_EDITION = re.compile(r"\b1st\s+ed(ition|\.)?\b", re.I)
# Finishes only Chrome stock has: paper Bowman and Topps have no Refractors.
CHROME_ONLY = re.compile(r"fractors?\b|\b(mojo|atomic|lava|raywave|sapphire)\b", re.I)


# Card-number prefixes that paper and Chrome both print (plus plain numbers): Bowman
# Draft's BD-57 and Pro Debut's PD-12 are each on paper and on Chrome. Every other
# prefix belongs to one print (BP/BCP, US/USC, HIFI, VV...), so the number alone
# says which card it is.
SHARED_PREFIXES = frozenset({"", "BD", "PD"})
AXIS = frozenset({"chrome", "paper"})


def product(title: str) -> frozenset[str]:
    """Which print a title is: its PRODUCT_WORDS, "1st edition", and usually one of
    "chrome" or "paper".

    A title that says neither "chrome" nor "paper" counts as paper, because that is
    how sellers title the paper card ("2020 Bowman Draft #BD-57"): the Chrome card
    almost always says Chrome, as it sells for more. So a bare title never matches a
    Chrome one, even with the same number and year (the BD-57 case that let a
    Chrome sale price a paper slab). Two exceptions:
    - a Chrome-only finish (CHROME_ONLY: "Bowman Draft BD-57 Refractor"): that card
      can only be Chrome, so it counts as Chrome;
    - a card number with a prefix only one print uses (not SHARED_PREFIXES:
      "2022 Bowman #HIFI-15 Hi-Fi Futures"): the number says which card it is, so
      the title is neither paper nor Chrome and matches both (sellers drop "Chrome"
      from Chrome-only inserts all the time).
    (The original fix request asked for bare-vs-Chrome to stay as it was; its own
    test, bare BD-57 vs Chrome BD-57 must not match, needs this stricter rule.)"""
    title = title or ""
    found = set(re.findall(r"[a-z0-9]+", title.lower())) & PRODUCT_WORDS
    if FIRST_EDITION.search(title):
        found.add("1st edition")
    if not found & AXIS:
        number = card_number(title)
        if CHROME_ONLY.search(title):
            found.add("chrome")
        elif not number or re.match(r"[A-Z]*", number.upper()).group() in SHARED_PREFIXES:
            found.add("paper")
    return frozenset(found)


def products_differ(one: frozenset[str], two: frozenset[str]) -> bool:
    """product() of two titles, compared: Sapphire, Optic and 1st Edition must agree,
    and Chrome and paper clash only when both titles have one."""
    if (one - AXIS) != (two - AXIS):
        return True
    return bool(one & AXIS and two & AXIS and one & AXIS != two & AXIS)


def different_product(a: str, b: str) -> bool:
    """True when two titles are different prints of a card (product()), however
    alike their words and numbers are."""
    return products_differ(product(a), product(b))


def identifiable(title: str) -> bool:
    """Whether a title says which card it is: a year, a card number or a set name
    (ledger.SET_WORDS). "PLAYER NAME RC ROOKIE REFRACTOR" says none of them and fits
    any Refractor, so a sale under such a title prices nothing: it is kept and
    listed, but never a ledger card, a comp or a "last sold"."""
    return _identifiable(title, id(profile.get()))   # words() follow the profile


@functools.lru_cache(maxsize=20000)
def _identifiable(title: str, _profile: int) -> bool:
    from . import ledger   # ledger imports this module; kept local
    if _year(title) or card_number(title):
        return True
    return bool(ledger._stems(words(title)) & ledger._stems(ledger.SET_WORDS))


def _closeness(title: str, mine: frozenset[str], sale: dict, min_overlap: float
               ) -> float | None:
    """How well a sale's title fits a listing's, or None when it isn't the same card.

    Another print (different_product: paper against Chrome, Chrome against
    Sapphire) or a different card number rules a sale out however alike the words
    are. Otherwise the same card number and year is a strong match even when the
    titles share few words, which is how a listing with a thin title gets its comps.
    """
    if different_product(title, sale["title"]) or not identifiable(sale["title"]):
        return None
    numbers = _number_match(title, sale["title"])
    if numbers is False:
        return None
    overlap = len(mine & sale["words"]) / len(mine | sale["words"])
    if numbers and _year(title) and _year(title) == _year(sale["title"]):
        return 1 + overlap
    if profile.get().mode == "team":
        # Many players' cards share a set and parallel; only the name tells them apart.
        if not same_player(title, sale["title"]):
            return None
        min_overlap = max(min_overlap, TEAM_OVERLAP)
    return overlap if overlap >= min_overlap else None


TEAM_OVERLAP = 0.7   # a team's comps without a card number need titles this alike


def same_player(a: str, b: str) -> bool:
    """Team mode: False when the two titles name different roster players."""
    who = profile.get()
    if who.mode != "team":
        return True
    one, two = who.roster_player(a), who.roster_player(b)
    return not (one and two and one != two)


def relevant(con, items: list[Listing]) -> list[dict]:
    """Past sales of the same cards as these listings, newest first.

    A sale counts when its title is close enough (see _closeness) and its
    grade, autograph and print run are the listing's. A sale that differs only
    in grade or print run is offered too, one per listing at most, and comes
    back with "hint" set so the prompt marks it as a different card.
    """
    sales = [dict(r) for r in con.execute(
        "SELECT item_id, title, price, shipping, bids, sold_at, kind, source FROM sales "
        "ORDER BY sold_at DESC")]
    if not sales:
        return []
    for sale in sales:
        sale["words"] = words(sale["title"])
        sale["traits"] = traits(sale["title"])
    chosen: dict[str, dict] = {}
    exact: set[str] = set()
    min_overlap = settings.load(con)["min_overlap"]
    for item in items:
        mine = words(item.title)
        if not mine:
            continue
        grade, auto, run = item_traits(item)
        same, hints = [], []
        for sale in sales:
            if sale["item_id"] == item.item_id or not sale["words"]:
                continue
            close = _closeness(item.title, mine, sale, min_overlap)
            if close is None or sale["traits"][1] != auto:
                continue   # an autograph against an unsigned copy tells nothing
            (same if sale["traits"] == (grade, auto, run) else hints).append((close, sale))
        for group in (same, hints):
            group.sort(key=lambda pair: (-pair[0], -pair[1]["sold_at"]))
        same = same[:PER_LISTING]
        exact.update(sale["item_id"] for _, sale in same)
        for _, sale in same + hints[:min(HINTS_PER_LISTING, PER_LISTING - len(same))]:
            chosen[sale["item_id"]] = sale
    out = sorted(chosen.values(), key=lambda s: -s["sold_at"])[:MAX_LINES]
    for sale in out:   # a hint for one listing may be the very card of another
        sale["hint"] = sale["item_id"] not in exact
    return out


def index(con) -> dict[str, list[dict]]:
    """Every recorded sale by card number, for checking hundreds of listings at once.

    `relevant` compares each listing with every sale, which is fine for one
    scoring call and far too slow for a team's whole search."""
    out: dict[str, list[dict]] = {}
    for r in con.execute("SELECT item_id, title, price, shipping, kind FROM sales"):
        number = card_number(r["title"])
        if number:
            out.setdefault(number, []).append({
                "item_id": r["item_id"], "title": r["title"], "words": words(r["title"]),
                "traits": traits(r["title"]), "year": _year(r["title"]),
                "total": delivered(r["price"], r["shipping"])})
    return out


def under_sold(sold: dict[str, list[dict]], item: Listing) -> int | None:
    """Percent this listing is under the usual price of the same card, from `index`.

    The same card means the same card number, year, grade, autograph and print
    run, and titles at least loosely alike (the same number can be another set).
    None when there is no such sale or it isn't cheaper."""
    number = card_number(item.title)
    if not number:
        return None
    mine, year, kind = words(item.title), _year(item.title), traits(item.title)
    same = [s["total"] for s in sold.get(number, [])
            if s["item_id"] != item.item_id and s["traits"] == kind
            and same_player(item.title, s["title"])
            and (not year or not s["year"] or s["year"] == year)
            and mine and len(mine & s["words"]) / len(mine | s["words"]) >= 0.4]
    if not same:
        return None
    usual = statistics.median(same)
    pct = round(100 * (1 - delivered(item.price, item.shipping) / usual))
    return pct if pct > 0 else None


def delivered(price: float, shipping: float | None) -> float:
    """Price plus shipping, with the usual assumption when the shipping wasn't stated."""
    return round(price + (UNKNOWN_SHIPPING if shipping is None else shipping), 2)


def bid_count(bids: int) -> str:
    return "1 bid" if bids == 1 else f"{bids} bids"


def lines(sales: list[dict]) -> str:
    """The sales as the scoring prompt shows them, one per line."""
    out = []
    for s in sales:
        day = datetime.fromtimestamp(s["sold_at"], timezone.utc).astimezone(config.TZ)
        ship = "ship ?" if s["shipping"] is None else f"+{s['shipping']:.2f} ship"
        bids = f", {bid_count(s['bids'])}" if s["bids"] else ""
        if s.get("kind") == "bin":   # a fixed price, paid as listed
            bids = ", BIN"
        if s.get("source") in SOURCE_LABEL:   # not one the bot looked up itself
            bids += f", {SOURCE_LABEL[s['source']]}"
        out.append(f"{day:%Y-%m-%d} | ${s['price']:.2f} {ship}{bids} | {s['title']}"
                   + (HINT if s.get("hint") else ""))
    return "\n".join(out)


def win_bid(value: float | None, shipping: float | None, max_bid: float | None
            ) -> float | None:
    """A bid that should usually win: the going rate less shipping, on odd cents.

    The max is a cheapskate's price and most good cards sell above it. This is
    the other number: what the card goes for, so a snipe here is a fair buy
    rather than a steal. None when it would be no higher than the max.
    """
    if not value or max_bid is None:
        return None
    bid = value - (UNKNOWN_SHIPPING if shipping is None else shipping)
    bid = int(bid) + 0.07 if bid >= 1 else 0
    return round(bid, 2) if bid > max_bid * 1.1 else None


def with_calls(con, since: float = 0) -> list[dict]:
    """Sales with the pick the bot made on the same auction, if any. Newest first."""
    out = []
    for row in con.execute(
            "SELECT s.item_id, s.title, s.price, s.shipping, s.bids, s.sold_at, s.image, "
            "s.kind, s.source, p.data "
            "FROM sales s LEFT JOIN picks p USING (item_id) WHERE s.sold_at>=? "
            "ORDER BY s.sold_at DESC", (since,)):
        row = dict(row)
        pick = json.loads(row.pop("data")) if row["data"] else None
        row["total"] = delivered(row["price"], row["shipping"])
        row["estimated"] = row["shipping"] is None   # the total assumes UNKNOWN_SHIPPING
        row["call"] = None
        row["picked"] = pick is not None
        # Only an auction tests a call: a Buy It Now pick was at or under its max by definition.
        if pick and pick.get("max") is not None and row["kind"] != "bin":
            value = pick.get("value")
            win = win_bid(value, row["shipping"], pick["max"])
            # Claude's own guess before calibrate.py corrected it (the same when it didn't)
            raw = pick.get("value_raw", value)
            row["call"] = {"tier": pick.get("tier"), "max": pick["max"], "value": value,
                           "value_raw": raw, "basis": pick.get("max_basis"),
                           # whether calibration moved it, when something did
                           **({"calibrated": pick.get("calibrated")}
                              if "value_raw" in pick else {}),
                           "under_max": row["price"] <= pick["max"], "win": win,
                           # a snipe at the win bid would have been in the running
                           "under_win": row["price"] <= (win or pick["max"])}
            # how far the real delivered price was from the going rate; + means it sold higher
            row["off"] = (row["total"] - value) / value if value else None
            row["off_raw"] = (row["total"] - raw) / raw if raw else None
        out.append(row)
    return out


def band(value: float) -> str:
    """Which price band a going rate falls in."""
    return next(name for low, high, name in BANDS if high is None or value < high)


def by_band(rows: list[dict], latest: int | None = None) -> list[dict]:
    """How far off the going rates ran in each price band, cheapest band first.

    `rows` are newest first; `latest` looks only at that many calls per band.
    """
    out = []
    for _, _, name in BANDS:
        off = [r["off"] for r in rows if r["call"] and r["off"] is not None
               and band(r["call"]["value"]) == name][:latest]
        out.append({
            "band": name, "calls": len(off),
            "value_gap_pct": round(100 * statistics.median(abs(o) for o in off)) if off else None,
            "value_bias_pct": round(100 * statistics.median(off)) if off else None})
    return out


def scorecard(rows: list[dict]) -> dict:
    """How the calls among these sales turned out.

    A call is a pick whose auction then sold. It is "under the max" when the
    final bid was at or below the max the bot gave, so a snipe at that max
    would have been in the running.
    """
    made = [r for r in rows if r["call"]]
    under = sum(r["call"]["under_max"] for r in made)
    off = [r["off"] for r in made if r["off"] is not None]
    return {
        "calls": len(made), "under_max": under, "over_max": len(made) - under,
        "under_win": sum(r["call"]["under_win"] for r in made),
        # how far the going-rate guess was from the real final price, typically
        "value_gap_pct": round(100 * statistics.median(abs(o) for o in off)) if off else None,
        # and which way: above zero means cards sold for more than the guess
        "value_bias_pct": round(100 * statistics.median(off)) if off else None,
        "valued": len(off),
        # the same two numbers for cheap, mid-priced and dear cards apart
        "bands": by_band(made),
        # and by where the going rate came from, so it shows which of them is off
        "by_source": {name: {**_accuracy(group), "bands": by_band(group)}
                      for name in SOURCES
                      if (group := [r for r in made if value_source(r["call"]) == name])},
    }


SOURCES = ("sales", "guessed", "calibrated")


def value_source(call: dict) -> str:
    """Where a call's going rate came from: real sales, Claude's guess, or the
    guess as calibrate.py corrected it."""
    if call.get("basis") == "sales":
        return "sales"
    if "calibrated" in call:   # set when the pick says whether calibration moved it
        return "calibrated" if call["calibrated"] else "guessed"
    raw = call.get("value_raw")
    return "calibrated" if raw is not None and raw != call.get("value") else "guessed"


def _accuracy(made: list[dict]) -> dict:
    off = [r["off"] for r in made if r["off"] is not None]
    return {
        "calls": len(made), "under_max": sum(r["call"]["under_max"] for r in made),
        "value_gap_pct": round(100 * statistics.median(abs(o) for o in off)) if off else None,
        "value_bias_pct": round(100 * statistics.median(off)) if off else None}


def track_record(con, corrected: Collection[str] = ()) -> str:
    """One line for the scoring prompt when the going rates have been running off.

    Each price band is judged on its own: a band is mentioned once it has
    TRACK_MIN calls and its going rates are track_off percent out or more.
    `corrected` are bands calibrate.py already corrects after Claude answers;
    they are left out, or Claude would add the same correction on top.
    """
    off = [f"for cards valued {b['band']}, final prices ran {abs(b['value_bias_pct'])}% "
           f"{'more' if b['value_bias_pct'] > 0 else 'less'} than your \"value\" "
           f"(last {b['calls']} picks)"
           for b in by_band(with_calls(con), TRACK_CALLS)
           if b["calls"] >= TRACK_MIN and b["band"] not in corrected
           and abs(b["value_bias_pct"]) >= settings.load(con)["track_off"]]
    if not off:
        return ""
    return ("- Your track record on picks whose auctions then sold, real final price with "
            f"shipping: {'; '.join(off)}. Correct for that when you estimate a going rate.\n")


def market(con, now: datetime, limit: int = 40) -> dict:
    """For the dashboard: the latest sold prices, and how the bot's own calls turned out."""
    rows = with_calls(con)
    week = now.timestamp() - 7 * 86400
    sold = [{
        "item_id": r["item_id"], "title": r["title"], "price": r["price"],
        "shipping": r["shipping"], "total": r["total"], "estimated": r["estimated"],
        "bids": r["bids"], "kind": r["kind"] or "auction",
        "sold_at": datetime.fromtimestamp(r["sold_at"], config.TZ).isoformat(
            timespec="minutes"),
        "image": r["image"], "url": f"https://www.ebay.com/itm/{r['item_id']}",
        "call": r["call"]} for r in rows[:limit]]
    return {"count": len(rows), "count_7d": sum(r["sold_at"] >= week for r in rows),
            **scorecard(rows), "sold": sold, "history": history(con),
            # cheap sales the bot never picked; worked out by the hourly run
            "missed_steals": len(missed(con)), "missed": missed(con)[:10]}


SAME_CARD = 0.75        # title overlap that makes two listings the same card


def card_key(title: str) -> frozenset[str]:
    """The identifying words, with "07/50" read as "/50": which copy doesn't matter."""
    return words(_copyless(title))


@lru_cache(maxsize=TITLE_CACHE)
def _copyless(title: str) -> str:
    return re.sub(r"\b\d{1,3}\s?/\s?(\d{1,3})\b", r" \1 ", title)


GRADE = re.compile(r"\b(psa|bgs|sgc|cgc|beckett)\s*(\d{1,2}(?:\.5)?)?", re.I)
# Smaller graders (GMA, PGI, TAG...): a slab of theirs sells for a fraction of a PSA one
# and well over the raw card, so it is its own grade. Their names are common words
# ("price tag"), so only with a grade after them.
OTHER_GRADE = re.compile(r"\b(gma|pgi|tag|hga|csg|ags|isa|ksa)\s*(\d{1,2}(?:\.5)?)\b", re.I)
AUTO = re.compile(r"\b(auto|autos|autograph|autographed|signed|rpa)\b", re.I)
RUN = re.compile(r"/\s?(\d{1,4})\b")


def item_traits(item: Listing) -> tuple:
    """A listing's traits: the title's, with what its item specifics add.

    A print run or autograph only the specifics mention is still part of the card
    (that is how a mislabeled listing gets the right comps), and the fake-autograph
    check overrules a title's "auto"."""
    grade, auto, run = traits(item.title)
    specifics = getattr(item, "aspects", None) or {}
    if specifics:
        from . import aspects   # it reads the database; only listings read in full need it
        names = aspects.names()
        if run is None:
            found = re.search(r"(\d{1,4})\s*$", specifics.get(names["print_run"], "").strip())
            run = found.group(1) if found and found.group(1) != "0" else None
        if not auto and specifics.get(names["autographed"], "").strip().lower() == "yes":
            auto = True
    if "fake_auto" in getattr(item, "flags", ()):
        auto = False
    elif "aftermarket_auto" in getattr(item, "flags", ()):
        auto = "ip"   # its own price pool: an IP/TTM signature is not a certified auto
    return grade, auto, run


def traits(title: str) -> tuple:
    """What changes a card's price without changing many words: grade, autograph, print run.
    The autograph is True, False, or "ip" for one signed in person or by mail
    (filters.aftermarket_auto), so those sales never price a certified auto."""
    from . import filters   # filters reads the profile; kept local
    grade = GRADE.search(title) or OTHER_GRADE.search(title)
    run = RUN.search(title)
    auto: bool | str = bool(AUTO.search(title))
    if auto and filters.aftermarket_auto(title):
        auto = "ip"
    return (grade.group(1).lower().replace("beckett", "bgs") + (grade.group(2) or "")
            if grade else None, auto, run.group(1) if run else None)


@lru_cache(maxsize=TITLE_CACHE)
def _title_traits(title: str) -> tuple:
    """traits(), kept per title: ledger_comps asks it of every sale for every listing."""
    return traits(title)


def _same(a: str, b: str) -> bool:
    """Two titles for the same card: like words, the same print (different_product),
    the same grade, autograph and print run, and no card numbers that disagree."""
    one, two = card_key(a), card_key(b)
    return (bool(one and two) and len(one & two) / len(one | two) >= SAME_CARD
            and not different_product(a, b)
            and traits(a) == traits(b) and _number_match(a, b) is not False
            and (profile.get().mode != "team" or same_player(a, b)))


def _alike_keys(one: frozenset[str], two: frozenset[str]) -> bool:
    """The word half of _same, on card keys already worked out."""
    return bool(one and two) and len(one & two) / len(one | two) >= SAME_CARD


def history(con, limit: int = 12) -> list[dict]:
    """Cards that have sold more than once, with every price: the start of a price trend."""
    groups: list[dict] = []
    # _same wants the same traits, the same print and SAME_CARD of the words: only
    # groups that pass those cheap checks are asked, so 2,000 sales take seconds
    by_traits: dict[tuple, list[dict]] = {}
    for row in with_calls(con):   # newest first
        if not identifiable(row["title"]):
            continue
        key = card_key(row["title"])
        made = product(row["title"])
        bucket = (traits(row["title"]), made - AXIS)   # Chrome and paper: checked per group
        group = next((g for g in by_traits.get(bucket, ())
                      if not products_differ(made, g["product"]) and _alike_keys(key, g["key"])
                      and _same(row["title"], g["title"])), None)
        if group is None:
            group = {"title": row["title"], "image": row["image"],
                     "item_id": row["item_id"], "sales": [], "key": key, "product": made}
            groups.append(group)
            by_traits.setdefault(bucket, []).append(group)
        group["sales"].append(row)
    out = []
    for g in groups:
        if len(g["sales"]) < 2:
            continue
        totals = [s["total"] for s in reversed(g["sales"])]   # oldest first
        out.append({
            "title": g["title"], "image": g["image"], "n": len(totals), "prices": totals[-12:],
            "last": totals[-1], "low": min(totals), "high": max(totals),
            "typical": round(statistics.median(totals), 2),
            # some of these totals assume the usual shipping because none was stated
            "estimated": any(s["estimated"] for s in g["sales"]),
            "last_at": datetime.fromtimestamp(g["sales"][0]["sold_at"], config.TZ).isoformat(
                timespec="minutes"),
            "url": f"https://www.ebay.com/itm/{g['item_id']}"})
    out.sort(key=lambda g: g["last_at"], reverse=True)
    out.sort(key=lambda g: -g["n"])
    return out[:limit]


def find_missed(con, now: float | None = None, days: int = MISSED_DAYS,
                owned: list[dict] | None = None) -> list[dict]:
    """Steals the bot passed on: sales it made no pick for that went cheap.

    Cheap means MISSED_UNDER percent or more below the typical price of the
    same card's earlier sales, shipping counted, with at least MISSED_PRIOR of
    those to go by. Biggest discount first.

    A card already in the collection (or the very listing that was bought) is
    not a miss: skipping it was right. `owned` is the inventory's rows, read
    when not given.
    """
    from . import inventory, snipecheck   # both import the scoring side; kept local
    now = now or time.time()
    owned = inventory.read()[1] if owned is None else owned
    bought = {(r.get("ebay_item_id") or "").strip() for r in owned} - {""}
    rows = with_calls(con)                       # newest first
    # a steal by your own rule: no sale over max_share of the going rate counts
    under = max(MISSED_UNDER, round(100 * (1 - settings.load(con)["max_share"])))
    by_traits: dict[tuple, list] = {}            # only the same grade and print run can match
    for row in rows:
        row["key"], row["number"] = card_key(row["title"]), card_number(row["title"])
        if not identifiable(row["title"]):
            row["key"] = frozenset()   # says no card: neither a miss nor a typical price
        row["parallel"] = _parallel(row["key"])
        row["product"] = product(row["title"])
        by_traits.setdefault(traits(row["title"]), []).append(row)
    out = []
    for row in rows:
        if row["picked"] or row["sold_at"] < now - days * 86400 or not row["key"]:
            continue
        earlier = [r["total"] for r in by_traits[traits(row["title"])]
                   if r["sold_at"] < row["sold_at"] and r["key"]
                   and len(row["key"] & r["key"]) / len(row["key"] | r["key"]) >= SAME_CARD
                   and r["parallel"] == row["parallel"]   # base isn't cheap next to Refractors
                   and not products_differ(r["product"], row["product"])   # nor paper next to Chrome
                   and not (row["number"] and r["number"]
                            and _number_match(row["title"], r["title"]) is False)]
        if len(earlier) < MISSED_PRIOR:
            continue
        typical = statistics.median(earlier)
        pct = round(100 * (1 - row["total"] / typical))
        if pct >= under and row["item_id"] not in bought \
                and not snipecheck.owned_copy(row["title"], owned):
            out.append({"item_id": row["item_id"], "title": row["title"], "total": row["total"],
                        "typical": round(typical, 2), "pct": pct, "n": len(earlier),
                        "kind": row["kind"] or "auction", "sold_at": row["sold_at"],
                        "url": f"https://www.ebay.com/itm/{row['item_id']}"})
    return sorted(out, key=lambda m: (-m["pct"], -m["sold_at"]))


def save_missed(con, now: float | None = None) -> int:
    """Work the missed steals out once an hour and keep them, newest first.

    Comparing every sale with every other is too slow to do each time the
    dashboard asks, so the prompt and the dashboard read this copy.
    """
    found = sorted(find_missed(con, now), key=lambda m: -m["sold_at"])[:MISSED_KEEP]
    db.kv_set(con, MISSED_KEY, json.dumps(found, ensure_ascii=False))
    return len(found)


def missed(con) -> list[dict]:
    """The missed steals the last hourly run found, newest first."""
    return json.loads(db.kv_get(con, MISSED_KEY, "[]"))


def missed_lines(rows: list[dict]) -> str:
    """Missed steals as the scoring prompt shows them, one per line."""
    return "\n".join(f"${m['total']:.2f} delivered, typically ${m['typical']:.2f} | {m['title']}"
                     for m in rows)


def last_sold(rows: list[dict], title: str, item_id: str = "") -> dict | None:
    """The latest recorded sale of the same card as this title, and how many there are."""
    same = [r for r in _candidates(rows, title)
            if r["item_id"] != item_id and identifiable(r["title"]) and _same(title, r["title"])]
    if not same:
        return None
    return {"total": same[0]["total"], "price": same[0]["price"], "n": len(same),
            "estimated": same[0]["estimated"],
            "at": datetime.fromtimestamp(same[0]["sold_at"], config.TZ).date().isoformat()}


_rows_index: list = [None, -1, None, None]   # the rows last indexed, their length, the profile, the index


def _candidates(rows: list[dict], title: str) -> list[dict]:
    """The rows _same could call the same card as this title, in order: two card keys
    with SAME_CARD of their words in common share one of any (n - SAME_CARD * n + 1)
    of the title's n words, so only rows holding one of its rarest few can match.
    The word index is kept for the rows it was built on: callers ask about a whole
    run's listings against one list (snipecheck, lowball, offers, the snapshot)."""
    who = profile.get()
    if _rows_index[0] is not rows or _rows_index[1] != len(rows) or _rows_index[2] is not who:
        posting: dict[str, list[int]] = {}
        for pos, r in enumerate(rows):
            for w in card_key(r["title"]):
                posting.setdefault(w, []).append(pos)
        _rows_index[:] = [rows, len(rows), who, posting]
    posting = _rows_index[3]
    key = card_key(title)
    if not key:
        return []
    need = math.ceil(len(key) * SAME_CARD - 1e-9)   # words a match must share
    rare = sorted(key, key=lambda w: (len(posting.get(w, ())), w))[:len(key) - need + 1]
    return [rows[i] for i in sorted({i for w in rare for i in posting.get(w, ())})]


FRESH_DAYS = 180   # comps older than this count for less (score.thin_comps)
SOURCE_LABEL = {"gixen": "your Gixen win", "won": "your win", "manual": "entered by hand"}


def ledger_comps(con, item: Listing, now: float | None = None) -> list[dict]:
    """Recorded sales of exactly this card, newest first.

    Exactly: the same card number and year where the titles have them, the
    same set and parallel words (min_overlap), the same print run, autograph
    and grade, and raw against raw, graded against graded (eBay's condition
    says which when the title doesn't)."""
    mine = words(item.title)
    if not mine:
        return []
    want = item_traits(item)
    graded = getattr(item, "graded", None)
    min_overlap = settings.load(con)["min_overlap"]
    out = []
    for r in con.execute("SELECT item_id, title, price, shipping, sold_at, kind, source "
                         "FROM sales ORDER BY sold_at DESC"):
        if r["item_id"] == item.item_id or _title_traits(r["title"]) != want:
            continue
        sale = dict(r)
        sale["words"] = words(sale["title"])
        if not sale["words"]:
            continue
        if graded is not None and want[0] is None and graded:
            continue   # the title names no grade, but eBay says graded
        if _closeness(item.title, mine, sale, min_overlap) is None:
            continue
        sale["total"] = delivered(sale["price"], sale["shipping"])
        out.append(sale)
    return same_parallel(item.title, out)


def _parallel(ws) -> frozenset[str]:
    """The parallel words among a title's words ("refractors" counted as "refractor")."""
    from . import ledger   # ledger imports this module; kept local
    return ledger._stems(ws) & ledger._PARALLEL_STEMS


def same_parallel(title: str, comps: list[dict]) -> list[dict]:
    """Of a card's comps, the ones with the title's parallel words: a Refractor prices
    from Refractors and a base card from base cards, not from both. The same card
    number and year let a Sapphire or a Refractor sale through _closeness.

    When no comp has the same parallel, all of them, each marked "mixed", so the
    value they make is held to a guess's stars (score.thin_comps)."""
    mine = _parallel(words(title))
    same = [c for c in comps if _parallel(c.get("words") or words(c["title"])) == mine]
    if same or not comps:
        return same
    return [dict(c, mixed=True) for c in comps]


def from_mail(con) -> int:
    """Your own wins from email (eBay "You won", Gixen end notices) as sales.

    One per item number; a price the bot already looked up on eBay is kept."""
    added = 0
    for r in con.execute("SELECT data FROM mail_events WHERE type='won'"):
        ev = json.loads(r["data"])
        item_id, price = str(ev.get("item_id") or ""), ev.get("price")
        if not item_id.isdigit() or price is None:
            continue
        try:
            when = datetime.fromisoformat(str(ev.get("date"))).replace(
                hour=12, tzinfo=config.TZ).timestamp()
            price = float(price)
            shipping = float(ev["shipping"]) if ev.get("shipping") is not None else None
        except (TypeError, ValueError):
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO sales (item_id, title, price, shipping, bids, sold_at, image,"
            " kind, source) VALUES (?,?,?,?,NULL,?,NULL,'auction',?)",
            (item_id, str(ev.get("title") or "")[:200], price, shipping, int(when),
             "gixen" if ev.get("source") == "gixen" else "won"))
        added += cur.rowcount
    con.commit()
    return added


def add_manual(con, item_id: str, price: float, shipping: float | None, title: str,
               now: float | None = None) -> None:
    """A sold price you saw yourself, kept like any other (and replacing it)."""
    if not str(item_id).isdigit():
        raise ValueError("the item number is digits only")
    if price <= 0 or (shipping is not None and shipping < 0):
        raise ValueError("price must be over 0 and shipping 0 or more")
    title = " ".join(str(title).split())[:200]
    if not title:
        raise ValueError("give the card's title, so it can be matched to listings")
    con.execute("INSERT OR REPLACE INTO sales (item_id, title, price, shipping, bids, sold_at,"
                " image, kind, source) VALUES (?,?,?,?,NULL,?,NULL,'auction','manual')",
                (str(item_id), title, round(price, 2),
                 None if shipping is None else round(shipping, 2), int(now or time.time())))
    con.commit()


def search(con, query: str, limit: int = 25) -> list[dict]:
    """Sales whose titles have every word of the query, newest first."""
    wanted = re.findall(r"[a-z0-9/#-]+", query.lower())
    rows = [r for r in with_calls(con)
            if all(w in r["title"].lower() for w in wanted)]
    return rows[:limit]


def price_check(con, query: str) -> str:
    """What the ledger says about a card, as text for the command line."""
    found = search(con, query)
    if not found:
        return (f"No sold prices on record for {query!r}. "
                f"{count(con)} sales are on record in all.")
    prices = sorted(r["total"] for r in found)
    out = [lines(found), "",
           f"{len(found)} sales · ${prices[0]:.2f} to ${prices[-1]:.2f} with shipping · "
           f"typical ${statistics.median(prices):.2f}"]
    unknown = sum(r["estimated"] for r in found)
    if unknown:
        out.append(f"{unknown} of them didn't state shipping; ${UNKNOWN_SHIPPING:.0f} is assumed.")
    return "\n".join(out)


def count(con, since: float = 0) -> int:
    return con.execute("SELECT count(*) FROM sales WHERE sold_at>=?", (since,)).fetchone()[0]


def main() -> None:
    con = db.connect()
    print(f"{collect(con)} sales added, {count(con)} on record")


if __name__ == "__main__":
    main()
