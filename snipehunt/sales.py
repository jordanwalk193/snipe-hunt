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

import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone

from . import config, db, profile, settings, usage
from .hidden import CODE, YEAR
from .ebay_search import EbayClient, Listing, _money

PER_RUN = 60            # look-ups per hourly run; the rest wait for the next one
SETTLE = 10 * 60        # seconds after the end before asking, so eBay has the result
GONE_AFTER = 3 * 3600   # a Buy It Now, or a listing from before end times, not seen this long
BIN_RECHECK = 24 * 3600  # a Buy It Now still for sale is asked about again after this
BIN_GIVE_UP = 30 * 86400  # and no longer once it has been out of the search this long
KEEP_DAYS = 365       # a year of sold prices: seasonal and older cards sell rarely
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
              "LOT", "AUTO", "QTY"}
# Words that say nothing about which card it is.
FILLER = {"the", "and", "card", "cards", "baseball", "football", "basketball", "hockey",
          "mlb", "nfl", "nba", "nhl", "rookie", "rc", "sp", "free", "shipping", "ship",
          "mint", "nm", "nrmt", "qty", "lot", "invest", "hot", "look", "wow", "read"}


def words(title: str) -> frozenset[str]:
    """The words of a title that identify the card, without the player's name or team.

    On a team bot only the team's words go: the player's name is what tells two
    cards from the same set apart."""
    who = profile.get()
    name = set(re.findall(r"[a-z0-9]+", " ".join(
        [who.player, who.search_name, *who.team_words]).lower()))
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
    con.execute("DELETE FROM sales WHERE sold_at<?", (now - KEEP_DAYS * 86400,))
    con.commit()
    return added


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


def _year(title: str) -> str | None:
    found = YEAR.search(title)
    return found.group(1) if found else None


def _closeness(title: str, mine: frozenset[str], sale: dict, min_overlap: float
               ) -> float | None:
    """How well a sale's title fits a listing's, or None when it isn't the same card.

    A different card number rules a sale out however alike the words are. The
    same card number and year is a strong match even when the titles share
    few words, which is how a listing with a thin title gets its comps.
    """
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
            row["call"] = {"tier": pick.get("tier"), "max": pick["max"], "value": value,
                           "under_max": row["price"] <= pick["max"], "win": win,
                           # a snipe at the win bid would have been in the running
                           "under_win": row["price"] <= (win or pick["max"])}
            # how far the real delivered price was from the going rate; + means it sold higher
            row["off"] = (row["total"] - value) / value if value else None
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
    }


def track_record(con) -> str:
    """One line for the scoring prompt when the going rates have been running off.

    Each price band is judged on its own: a band is mentioned once it has
    TRACK_MIN calls and its going rates are track_off percent out or more.
    """
    off = [f"for cards valued {b['band']}, final prices ran {abs(b['value_bias_pct'])}% "
           f"{'more' if b['value_bias_pct'] > 0 else 'less'} than your \"value\" "
           f"(last {b['calls']} picks)"
           for b in by_band(with_calls(con), TRACK_CALLS)
           if b["calls"] >= TRACK_MIN
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
    return words(re.sub(r"\b\d{1,3}\s?/\s?(\d{1,3})\b", r" \1 ", title))


GRADE = re.compile(r"\b(psa|bgs|sgc|cgc|beckett)\s*(\d{1,2}(?:\.5)?)?", re.I)
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
    return grade, auto and "fake_auto" not in getattr(item, "flags", ()), run


def traits(title: str) -> tuple:
    """What changes a card's price without changing many words: grade, autograph, print run."""
    grade = GRADE.search(title)
    run = RUN.search(title)
    return (grade.group(1).lower().replace("beckett", "bgs") + (grade.group(2) or "")
            if grade else None, bool(AUTO.search(title)), run.group(1) if run else None)


def _same(a: str, b: str) -> bool:
    """Two titles for the same card: like words, the same grade, autograph and print run,
    and no card numbers that disagree."""
    one, two = card_key(a), card_key(b)
    return (bool(one and two) and len(one & two) / len(one | two) >= SAME_CARD
            and traits(a) == traits(b) and _number_match(a, b) is not False
            and (profile.get().mode != "team" or same_player(a, b)))


def history(con, limit: int = 12) -> list[dict]:
    """Cards that have sold more than once, with every price: the start of a price trend."""
    groups: list[dict] = []
    for row in with_calls(con):   # newest first
        group = next((g for g in groups if _same(row["title"], g["title"])), None)
        if group is None:
            group = {"title": row["title"], "image": row["image"],
                     "item_id": row["item_id"], "sales": []}
            groups.append(group)
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


def find_missed(con, now: float | None = None, days: int = MISSED_DAYS) -> list[dict]:
    """Steals the bot passed on: sales it made no pick for that went cheap.

    Cheap means MISSED_UNDER percent or more below the typical price of the
    same card's earlier sales, shipping counted, with at least MISSED_PRIOR of
    those to go by. Biggest discount first.
    """
    now = now or time.time()
    rows = with_calls(con)                       # newest first
    by_traits: dict[tuple, list] = {}            # only the same grade and print run can match
    for row in rows:
        row["key"], row["number"] = card_key(row["title"]), card_number(row["title"])
        by_traits.setdefault(traits(row["title"]), []).append(row)
    out = []
    for row in rows:
        if row["picked"] or row["sold_at"] < now - days * 86400 or not row["key"]:
            continue
        earlier = [r["total"] for r in by_traits[traits(row["title"])]
                   if r["sold_at"] < row["sold_at"] and r["key"]
                   and len(row["key"] & r["key"]) / len(row["key"] | r["key"]) >= SAME_CARD
                   and not (row["number"] and r["number"]
                            and _number_match(row["title"], r["title"]) is False)]
        if len(earlier) < MISSED_PRIOR:
            continue
        typical = statistics.median(earlier)
        pct = round(100 * (1 - row["total"] / typical))
        if pct >= MISSED_UNDER:
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
    same = [r for r in rows if r["item_id"] != item_id and _same(title, r["title"])]
    if not same:
        return None
    return {"total": same[0]["total"], "price": same[0]["price"], "n": len(same),
            "estimated": same[0]["estimated"],
            "at": datetime.fromtimestamp(same[0]["sold_at"], config.TZ).date().isoformat()}


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
        sale = dict(r)
        if sale["item_id"] == item.item_id:
            continue
        sale["words"] = words(sale["title"])
        if not sale["words"] or traits(sale["title"]) != want:
            continue
        sale_graded = traits(sale["title"])[0] is not None
        if graded is not None and want[0] is None and sale_graded != graded:
            continue   # the title names no grade, but eBay says graded (or raw)
        if _closeness(item.title, mine, sale, min_overlap) is None:
            continue
        sale["total"] = delivered(sale["price"], sale["shipping"])
        out.append(sale)
    return out


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
