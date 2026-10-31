"""The market sweep: what was already up before the bot started, and the auctions
that haven't reached their last few hours yet.

The hourly searches read the first page of each: everything listed in the last
hour, and the hundred auctions ending soonest. For a player with thousands of
listings up at once that leaves most of the standing market unseen. A Buy It Now
listed in August is never on either page; an auction that runs a week is only
seen in its last half day (measured on the live bot: 615 of 936 live auctions
unknown, and page after page of older Buy It Nows nobody had looked at). This
lane pages through the rest, a few pages an hour, and comes back around:

- auctions: every live auction, every run (pages two onward of the ending-
  soonest search, AUCTION_PAGES at most), so each is seen within an hour of
  going live instead of the day it ends;
- Buy It Nows: the newly-listed search paged inside price bands (eBay stops a
  search at 10,000 results; each band is its own search, and the bands stop
  at the steals ceiling), `deep_pages` pages a run, the cursor kept in the
  database so each run picks up where the last one stopped. A full pass takes
  a day or two, then starts over.

Everything found is remembered like any other listing, so a price drop on an
old Buy It Now shows. But only a Buy It Now the ledger prices as a steal, or
one worth a look by itself (a low print run, an autograph, a slab, a thin
title), goes to Claude, SCORE_MAX a run; the rest wait for the ledger to learn
more. Auctions score as they always did, just earlier. Read-only, like every
other search.
"""
from __future__ import annotations

import json
import statistics
import sys
import time

from . import db, ebay_search, profile, rarity, sales, settings, steals
from .ebay_search import EbayClient, Listing

PAGE = 200             # eBay's largest page
AUCTION_PAGES = 5      # pages two to six of the ending-soonest search: 1,000 live auctions
CAP = 10000            # eBay answers no search past this many results
# Buy It Now price bands, in dollars: each is its own 10,000-result window. The
# last stops at the autograph ceiling; past it nothing is a steal in steals mode.
BANDS = ((0, 3), (3, 6), (6, 10), (10, 20), (20, 40), (40, 80))
CURSOR_KEY = "deep_cursor"
BIN_FILTER = "buyingOptions:{FIXED_PRICE|BEST_OFFER}"
SCORE_MAX = 15         # Buy It Nows from the sweep that may go to Claude in one run
STEAL_SHARE = 0.6      # with steals mode off: a steal is this share of the ledger's price
LABEL_AUCTIONS, LABEL_BIN = "deep-auctions", "deep-bin"


def on(con=None) -> bool:
    return settings.load(con)["deep_pages"] > 0 and profile.get().mode != "team"


def cursor(con) -> dict:
    cur = json.loads(db.kv_get(con, CURSOR_KEY, "{}") or "{}")
    return {"band": 0, "offset": 0, "passes": 0, "last_pass": None, "found": 0, **cur}


def band_text(cur: dict) -> str:
    lo, hi = BANDS[min(cur["band"], len(BANDS) - 1)]
    return f"${lo}–${hi} at {cur['offset']:,}"


def status(con) -> dict:
    """For the dashboard's bot health: where the sweep is and how far it has come."""
    cur = cursor(con)
    return {"on": on(con), "band": band_text(cur), "passes": cur["passes"],
            "last_pass": cur["last_pass"], "found": cur["found"]}


def _parse(raw_items: list[dict], known: set[str], label: str, found: dict) -> int:
    """Keep the player's listings not already found this run; how many were kept."""
    kept = 0
    for raw in raw_items:
        item = ebay_search.parse_item(raw)
        if item is None or item.currency != "USD" or item.item_id in known:
            continue
        verdict = ebay_search.classify(item.title)
        if verdict == "no":
            continue
        item.match = verdict
        item.queries.append(label)
        if found.setdefault(item.item_id, item) is item:
            kept += 1
    return kept


def search(con, known: set[str], client: EbayClient | None = None,
           now: float | None = None, verbose: bool = False) -> list[Listing]:
    """The standing market, a slice at a time. `known` are this run's listings."""
    if not on(con):
        return []
    now = now or time.time()
    client = client or EbayClient()
    name = profile.get().search_name
    found: dict[str, Listing] = {}
    # 1. every live auction past the first page, every run
    client.lane = LABEL_AUCTIONS
    auctions = 0
    for page in range(1, AUCTION_PAGES + 1):
        try:
            got = client.search(name, "buyingOptions:{AUCTION}", "endingSoonest",
                                limit=PAGE, offset=page * PAGE)
        except Exception as exc:   # one bad page shouldn't stop the hunt
            print(f"deep: auctions page {page}: {exc}", file=sys.stderr)
            break
        auctions += _parse(got, known, LABEL_AUCTIONS, found)
        if len(got) < PAGE:
            break
    # 2. the Buy It Nows, from where the last run stopped
    cur = cursor(con)
    pages = settings.load(con)["deep_pages"]
    client.lane = LABEL_BIN
    bins = 0
    for _ in range(pages):
        lo, hi = BANDS[cur["band"]]
        flt = f"{BIN_FILTER},{ebay_search.price_filter(lo, hi)}"
        try:
            got = client.search(name, flt, "newlyListed", limit=PAGE, offset=cur["offset"])
        except Exception as exc:
            print(f"deep: Buy It Nows {band_text(cur)}: {exc}", file=sys.stderr)
            break
        bins += _parse(got, known, LABEL_BIN, found)
        cur["offset"] += PAGE
        if len(got) < PAGE or cur["offset"] + PAGE > CAP:   # the band is done
            cur["band"], cur["offset"] = cur["band"] + 1, 0
            if cur["band"] >= len(BANDS):                   # a full pass: start over
                cur.update(band=0, passes=cur["passes"] + 1, last_pass=int(now))
    cur["found"] += len(found)
    db.kv_set(con, CURSOR_KEY, json.dumps(cur))
    if verbose:
        print(f"  deep: {auctions} more live auctions, {bins} standing Buy It Nows "
              f"({len(found)} new to this run); next {band_text(cur)}, pass {cur['passes'] + 1}",
              file=sys.stderr)
    return list(found.values())


def from_sweep(item: Listing) -> bool:
    return any(q.startswith("deep-") for q in item.queries)


def worth_scoring(con, item: Listing, now: float | None = None) -> bool:
    """Is a Buy It Now from the sweep worth a Claude call? Yes when the ledger says
    it is a steal (its delivered price at or under the steal share of the exact
    card's going rate), or when nothing on record prices it and it is the kind
    of card worth a look. Everything that isn't from the sweep, and every
    auction, is scored as usual."""
    if not from_sweep(item) or item.is_auction:
        return True
    comps = sales.ledger_comps(con, item, now)
    if comps:
        going = statistics.median(c["total"] for c in comps[:12])
        cfg = steals.profile_config() if steals.on() else None
        share = settings.load(con)["max_share"] if cfg else STEAL_SHARE
        return going > 0 and item.total <= share * going
    run = rarity.print_run(item.title)
    return ((run is not None and run <= 99) or bool(rarity.AUTO.search(item.title))
            or bool(rarity.GRADED.search(item.title)) or item.match == "unclear")


def pick_fresh(con, items: list[Listing], now: float | None = None) -> list[Listing]:
    """Of the sweep's Buy It Nows that need scoring, the SCORE_MAX worth it, the
    cheapest share of the ledger's price first; the rest come around next pass."""
    worth = [i for i in items if worth_scoring(con, i, now)]
    return sorted(worth, key=lambda i: i.total)[:SCORE_MAX]
