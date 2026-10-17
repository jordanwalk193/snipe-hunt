"""Team mode's free filter: what is worth a Claude call, before anything is paid for.

A team lists about a thousand cards an hour. Nearly all of them are base
cards at the going rate, so they are filtered here for free, from the title,
the price and what the bot already knows: owned cards, recorded sales, price
cuts and relists. What is left is ranked by a cheap score that puts evidence
of a steal first (under a recorded sale, a price cut, a sleeper auction, a
misspelled title) and flash second (autographs, low print runs), and only the
top of that list goes to Claude.

Nothing here calls eBay, Claude or the web.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db, inventory, rarity, sales, settings
from .ebay_search import Listing

FUNNEL_KEY = "team_funnel"
CANDIDATES_KEY = "team_candidates"
BUNDLES_KEY = "team_bundles"
CANDIDATES_SHOWN = 15
SLEEPER_HOURS = 6         # an auction ending this soon with SLEEPER_BIDS or fewer bids
SLEEPER_BIDS = 1
BUNDLE_MIN = 3            # one seller with this many worth-a-look cards is a bundle
BUNDLE_FROM = 150         # bundles are built from the best-ranked this many, not the whole list
SELLER_TRUST_OVER = 30.0  # the same line score.apply_caps draws
# Lots, team sets and other things that aren't one card. Packed product and
# breaks are never cards; lots are, but their value is anyone's guess.
LOT = re.compile(r"\b(lot|lots|you pick|u pick|pick your|team set|complete set|"
                 r"\d+\s?cards|x\s?\d{2,}|\d{2,}\s?x)\b", re.I)
NOT_A_CARD = re.compile(r"\b(reprint|rp|custom|digital|break|case break|box|hobby box|"
                        r"blaster|sealed|pack|packs|pick a player|team break|random)\b", re.I)
PATCH = re.compile(r"\b(patch|relic|jersey|rpa|game used|game-used|memorabilia)\b", re.I)
ROOKIE = re.compile(r"\b(rc|rookie|1st bowman|first bowman|prospect|debut|draft)\b", re.I)
GEM = re.compile(r"\b(psa|bgs|sgc|cgc)\s?10\b", re.I)


@dataclass
class Look:
    """Why one listing is worth a look, and how much."""
    item: Listing
    points: float = 0.0
    reasons: list[str] = field(default_factory=list)
    under_pct: int | None = None   # under a recorded sale of the same card

    def add(self, points: float, reason: str) -> None:
        self.points += points
        self.reasons.append(reason)


def _hours_left(item: Listing, now: datetime) -> float | None:
    return item.ends_in_hours(now) if item.is_auction else None


def look(item: Listing, cfg: dict, sold: dict, moves: dict, want: list[set[str]],
         roster: list[str], now: datetime) -> Look:
    """Score one listing. Steal evidence counts most; looks alone count less."""
    out = Look(item)
    hours = _hours_left(item, now)
    # The evidence that something is underpriced.
    pct = sales.under_sold(sold, item) if (not item.is_auction or (
        hours is not None and hours <= SLEEPER_HOURS)) else None
    if pct is not None and pct >= 15:
        out.under_pct = pct
        out.add(min(8.0, pct / 8), f"{pct}% under a recorded sale")
    move = moves.get(item.item_id)
    if move and move["kind"] == "drop":
        out.add(3.0, f"price cut {move['cut_pct']}%")
    elif move and move["kind"] == "relist":
        out.add(1.5, "relisted")
    if hours is not None and 0 < hours <= SLEEPER_HOURS and (item.bid_count or 0) <= SLEEPER_BIDS:
        out.add(2.5, "ending soon, no bidders")
    if item.via == "typo":
        out.add(2.5, "misspelled title")
    title_words = set(re.findall(r"[a-z0-9]+", item.title.lower()))
    if any(len(w & title_words) >= max(2, round(0.75 * len(w))) for w in want):
        out.add(3.0, "want list")
    # What makes a card worth chasing at all.
    run = rarity.print_run(item.title)
    if run is not None and run <= cfg["team_max_run"]:
        out.add(4.0 if run == 1 else 3.0 if run <= 10 else 2.0 if run <= 25
                else 1.5 if run <= 50 else 1.0, f"/{run}")
    if rarity.AUTO.search(item.title):
        out.add(2.0, "auto")
    if PATCH.search(item.title):
        out.add(1.5, "patch")
    if ROOKIE.search(item.title):
        out.add(1.0, "rookie")
    if GEM.search(item.title):
        out.add(0.5, "gem mint")
    if item.player and item.player in roster:
        place = roster.index(item.player)
        out.add(max(1.0, 2.5 - 0.5 * place), item.player)
    # Doubts, and the frugal tilt: cheap copies of good cards first.
    if item.match == "unclear":
        # A roster player with no team word is usually right; a bare nickname with no
        # card word is usually another sport's team.
        out.add(-0.5 if item.via == "roster" else -2.5, "unclear match")
    total = sales.delivered(item.price, item.shipping)
    if (item.seller_feedback_score is not None and total > SELLER_TRUST_OVER
            and item.seller_feedback_score < cfg["min_seller_feedback"]):
        out.add(-1.0, "new seller")
    if out.points > 0 and total <= 10:
        out.add(0.5, "cheap")
    return out


# Reasons that add points but don't earn a look on their own: a plain rookie card
# or a plain card of a roster player at the going rate isn't a steal.
WEAK = ("unclear", "new seller", "cheap", "rookie", "gem mint", "combo")


def _strong(lk: Look) -> bool:
    """Enough to look at even under the price floor: a cheap copy of a good card is
    exactly what a frugal collection is built from."""
    run = rarity.print_run(lk.item.title)
    return (lk.under_pct is not None or "want list" in lk.reasons or "auto" in lk.reasons
            or "patch" in lk.reasons or (run is not None and run <= 25)
            or any(r.startswith("price cut") for r in lk.reasons))


def _reason_to_look(lk: Look) -> bool:
    roster = lk.item.player or ""
    return any(not r.startswith(WEAK) and r != roster for r in lk.reasons)


def want_words() -> list[set[str]]:
    """The want list's targets as word sets, for a free "is this on the list" check."""
    from .snapshot import wantlist   # snapshot imports most modules; kept local
    stop = {"a", "an", "the", "of", "any", "e", "g", "eg", "or", "and", "with", "for", "to",
            "card", "cards", "under", "recent", "sales", "only", "worth"}
    out = []
    for row in wantlist():
        words = {w for w in re.findall(r"[a-z0-9]+", row["target"].lower())
                 if w not in stop and len(w) >= 3}
        if len(words) >= 2:
            out.append(words)
    return out


def build(con, listings: list[Listing], moves: list[dict], rows: list[dict] | None = None,
          now: datetime | None = None, cfg: dict | None = None
          ) -> tuple[list[Look], dict, list[dict]]:
    """Filter and rank a team's listings.

    Returns (worth a look, best first; the funnel counts; seller bundles).
    """
    from . import profile
    now = now or datetime.now(timezone.utc)
    cfg = cfg or settings.load(con)
    rows = rows if rows is not None else inventory.read()[1]
    sold = sales.index(con)
    by_move = {m["item_id"]: m for m in moves}
    want = want_words()
    who = profile.get()
    roster = list(getattr(who, "roster", []))
    dropped = {"lot": 0, "not_a_card": 0, "owned": 0, "too_dear": 0, "too_cheap": 0,
               "plain": 0, "combo": 0}
    kept: list[Look] = []
    for item in listings:
        if NOT_A_CARD.search(item.title):
            dropped["not_a_card"] += 1
            continue
        if LOT.search(item.title) and not cfg["team_lots"]:
            dropped["lot"] += 1
            continue
        if inventory.owns(rows, item.title, item.player):
            dropped["owned"] += 1
            continue
        total = sales.delivered(item.price, item.shipping)
        if total > cfg["team_max_price"]:
            dropped["too_dear"] += 1
            continue
        lk = look(item, cfg, sold, by_move, want, roster, now)
        if getattr(who, "other_team", None) and who.other_team(item.title):
            if not item.player:   # a combo card with no one the collector chases on it
                dropped["combo"] += 1
                continue
            lk.add(-1.0, "combo card")
        if not _reason_to_look(lk):
            dropped["plain"] += 1
            continue
        if not item.is_auction and total < cfg["team_min_price"] and not _strong(lk):
            dropped["too_cheap"] += 1
            continue
        kept.append(lk)
    kept.sort(key=lambda lk: (-lk.points, sales.delivered(lk.item.price, lk.item.shipping)))
    funnel = {"found": len(listings), "dropped": dropped, "worth_a_look": len(kept)}
    return kept, funnel, bundles(kept[:BUNDLE_FROM])


def bundles(kept: list[Look]) -> list[dict]:
    """Sellers with several worth-a-look cards: one order, one shipping charge.

    Building a collection cheaply is often less about one steal than about
    paying shipping once for five cards."""
    by_seller: dict[str, list[Look]] = {}
    for lk in kept:
        if lk.item.seller:
            by_seller.setdefault(lk.item.seller, []).append(lk)
    out = []
    for seller, looks in by_seller.items():
        if len(looks) < BUNDLE_MIN:
            continue
        ships = [lk.item.shipping for lk in looks if lk.item.shipping]
        saved = round(sum(sorted(ships)[:-1]), 2) if len(ships) > 1 else 0.0
        out.append({
            "seller": seller, "count": len(looks),
            "total": round(sum(lk.item.price for lk in looks), 2),
            "shipping_saved": saved,   # if the seller combines: every charge but the largest
            "items": [{"item_id": lk.item.item_id, "title": lk.item.title,
                       "price": lk.item.price, "url": lk.item.url,
                       "is_auction": lk.item.is_auction} for lk in looks[:10]]})
    out.sort(key=lambda b: (-b["count"], -b["shipping_saved"]))
    return out[:10]


def candidate(lk: Look) -> dict:
    """A listing as the dashboard shows it before (or without) Claude."""
    item = lk.item
    return {"item_id": item.item_id, "title": item.title, "price": item.price,
            "shipping": item.shipping, "total": item.total, "is_auction": item.is_auction,
            "end_time": item.end_time, "seller": item.seller, "url": item.url,
            "image": item.image, "player": item.player, "points": round(lk.points, 1),
            "reasons": lk.reasons[:6], "under_pct": lk.under_pct}


def mark_tracked(con, kept: list[Look], listings: list[Listing]) -> None:
    """Only listings worth a look get their sold price looked up when they close:
    a team's 40,000 auctions a day would swamp eBay's daily call limit."""
    good = {lk.item.item_id for lk in kept}
    con.executemany("UPDATE seen SET tracked=? WHERE item_id=?",
                    [(1 if i.item_id in good else 0, i.item_id) for i in listings])


def save(con, funnel: dict, kept: list[Look], bundle_rows: list[dict]) -> None:
    db.kv_set(con, FUNNEL_KEY, json.dumps(funnel))
    db.kv_set(con, CANDIDATES_KEY, json.dumps(
        [candidate(lk) for lk in kept[:CANDIDATES_SHOWN]], ensure_ascii=False))
    db.kv_set(con, BUNDLES_KEY, json.dumps(bundle_rows, ensure_ascii=False))


def load(con) -> dict:
    return {"funnel": json.loads(db.kv_get(con, FUNNEL_KEY, "null")),
            "candidates": json.loads(db.kv_get(con, CANDIDATES_KEY, "[]")),
            "bundles": json.loads(db.kv_get(con, BUNDLES_KEY, "[]"))}
