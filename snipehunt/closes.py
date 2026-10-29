"""Where steals happen: what the sold-price ledger says about how auctions close.

Besides the card, two things decide an auction's closing price: who is selling
it and when it ends. Measured on one bot's 175 picked auctions that sold:

- A consignment seller (ten or more auctions on record) closed at a median
  136% of the bot's going rate, and the bot's max won 15% of the time. A
  seller with one or two auctions on record closed at 82%, and the max won
  44% of the time. Consignors have a following; one-off sellers don't.
- At those small sellers, an auction ending in the daytime (8am to 8pm in
  the bot's time zone) closed at 77% of the going rate with a median of two
  bids; an evening ending closed at 94% with four. Consignors end their
  auctions in the evening almost without exception.

So the bot learns both from its own ledger and says so on each auction pick:
a quiet seller and a daytime ending are where a max is likely to win, a hot
seller is where it rarely does. Lowball snipes skip hot sellers. Nothing
here bids; it only tells you where to look.
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime

from . import config, sales

SELLER_MIN = 4       # results a seller (or a group) needs before its median counts
CONSIGNOR = 10       # auctions sold from one seller on record: a consignor
MIN_CALLS = 30       # results before any of this is trusted at all
HOT = 1.15           # closes at this share of the going rate or more: a hot seller
COLD = 0.90          # at this or less: a quiet seller
DAY = (8, 20)        # local hours [from, to) whose endings close cheapest
SHOW = 5             # hottest and quietest sellers named in the snapshot


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def learn(con) -> dict:
    """Closing price as a share of the going rate, by seller and by ending block.

    From the bot's picks whose auctions then sold (sales.with_calls). The hour
    table is built from small sellers only: consignors all end in the evening,
    so counting them would make "evening" look like "consignor".
    """
    rows = [r for r in sales.with_calls(con)
            if r["call"] and r["call"]["value"] and r["kind"] != "bin"]
    seller_of = {r["item_id"]: r["seller"] for r in con.execute(
        "SELECT item_id, seller FROM seen WHERE seller IS NOT NULL AND seller != ''")}
    counts = Counter(r["seller"] for r in con.execute(
        "SELECT e.seller FROM sales s JOIN seen e USING (item_id) "
        "WHERE COALESCE(s.kind, 'auction') = 'auction' AND e.seller IS NOT NULL AND e.seller != ''"))
    per: dict[str, list[float]] = {}
    small, big, day, evening = [], [], [], []
    for r in rows:
        name, share = seller_of.get(r["item_id"]), r["total"] / r["call"]["value"]
        if name:
            per.setdefault(name, []).append(share)
        consignor = bool(name) and counts.get(name, 0) >= CONSIGNOR
        (big if consignor else small).append(share)
        if not consignor:
            hour = datetime.fromtimestamp(r["sold_at"], config.TZ).hour
            (day if DAY[0] <= hour < DAY[1] else evening).append(share)
    return {
        "calls": len(rows), "trusted": len(rows) >= MIN_CALLS,
        "sellers": {s: {"n": len(v), "share": _median(v)} for s, v in per.items()
                    if len(v) >= SELLER_MIN},
        "counts": dict(counts),
        "groups": {"small": {"n": len(small), "share": _median(small)},
                   "consignor": {"n": len(big), "share": _median(big)}},
        "hours": {"day": {"n": len(day), "share": _median(day)},
                  "evening": {"n": len(evening), "share": _median(evening)}},
    }


def seller(learned: dict | None, name: str | None) -> dict | None:
    """How this seller's auctions close: {"kind": hot|warm|cold, "share", "n", "basis"}.

    The seller's own median once it has SELLER_MIN results, else the median of
    its group (consignor or small). None until the ledger is big enough."""
    if not learned or not learned.get("trusted") or not name:
        return None
    own = learned["sellers"].get(name)
    if own:
        share, n, basis = own["share"], own["n"], "own"
    else:
        group = "consignor" if learned["counts"].get(name, 0) >= CONSIGNOR else "small"
        g = learned["groups"][group]
        if not g["share"] or g["n"] < SELLER_MIN:
            return None
        share, n, basis = g["share"], g["n"], group
    kind = "hot" if share >= HOT else "cold" if share <= COLD else "warm"
    return {"kind": kind, "share": share, "n": n, "basis": basis}


def ending(learned: dict | None, end_time: str | None) -> dict | None:
    """How auctions ending at this hour close (small sellers): {"block", "share", "n"}."""
    if not learned or not learned.get("trusted") or not end_time:
        return None
    hour = datetime.fromisoformat(end_time).astimezone(config.TZ).hour
    block = "day" if DAY[0] <= hour < DAY[1] else "evening"
    g = learned["hours"][block]
    if not g["share"] or g["n"] < SELLER_MIN:
        return None
    return {"block": block, "share": g["share"], "n": g["n"]}


def annotate(pick: dict, learned: dict | None) -> dict:
    """Give an auction pick its "closes": the seller's heat, the ending block, and
    the price it is likely to close at. Other picks come back unchanged."""
    if not pick.get("is_auction") or not learned:
        return pick
    s, e = seller(learned, pick.get("seller")), ending(learned, pick.get("end_time"))
    if not s and not e:
        pick.pop("closes", None)
        return pick
    out: dict = {}
    if s:
        out.update(seller=s["kind"], seller_share=s["share"], seller_n=s["n"])
    if e:
        out.update(ending=e["block"], ending_share=e["share"])
    # A hot seller's following sets the price whatever the hour. At a small
    # seller the hour table (built from small sellers) is the better guide,
    # tempered by the seller's own record when it has one.
    if s and s["kind"] == "hot":
        share = s["share"]
    elif e and s and s["basis"] == "own":
        share = round((e["share"] + s["share"]) / 2, 2)
    elif e:
        share = e["share"]
    else:
        share = s["share"]
    out["likely_share"] = share
    if pick.get("value"):
        out["likely"] = round(float(pick["value"]) * share, 2)
    pick["closes"] = out
    return pick


def summary(learned: dict) -> dict:
    """For the dashboard: the groups, the hour blocks, and the sellers at each end."""
    named = sorted(learned["sellers"].items(), key=lambda kv: kv[1]["share"])
    return {
        "calls": learned["calls"], "trusted": learned["trusted"],
        "groups": learned["groups"], "hours": learned["hours"],
        "quiet": [{"seller": s, **v} for s, v in named if v["share"] <= COLD][:SHOW],
        "hot": [{"seller": s, **v} for s, v in reversed(named) if v["share"] >= HOT][:SHOW],
    }


def line(pick: dict) -> str:
    """One line for an alert, or "" when there is nothing learned about this auction."""
    c = pick.get("closes")
    if not c:
        return ""
    if c.get("seller") == "hot":
        return (f"🔥 Hot seller: their auctions close ~{round(100 * c['seller_share'])}% of "
                f"value ({c['seller_n']} sold), a max rarely wins")
    parts = []
    if c.get("seller") == "cold":
        parts.append("quiet seller")
    if c.get("ending") == "day":
        parts.append("daytime ending")
    if not parts:
        return ""
    likely = f" · likely to close ~${c['likely']:.2f}" if c.get("likely") else ""
    return f"🧊 {' · '.join(parts).capitalize()}: closes ~{round(100 * c['likely_share'])}% of value here{likely}"
