"""Sold prices the bot saw for itself.

eBay blocks its sold-listings pages, so most prices used to be estimates. But
the Browse API still answers for an auction after it ends, with the final bid
and whether it sold. Every auction the search has seen is looked up once after
it closes and the result is kept here, so scoring can price a card from what
the same card really went for.

Read-only, like the rest of the eBay code: this only ever reads one listing.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone

from . import config, db, profile
from .ebay_search import EbayClient, Listing, _money

PER_RUN = 60            # look-ups per hourly run; the rest wait for the next one
SETTLE = 10 * 60        # seconds after the end before asking, so eBay has the result
GONE_AFTER = 3 * 3600   # a listing from before this feature, not seen for this long
KEEP_DAYS = 180
PER_LISTING = 3         # sold prices offered to Claude for one listing
MAX_LINES = 60          # and for one scoring call, because every line is paid for
MIN_OVERLAP = 0.5
UNKNOWN_SHIPPING = 5.00
TRACK_CALLS = 30        # the latest calls the scoring prompt's track record looks at
TRACK_MIN = 8           # fewer than this says nothing yet
TRACK_OFF = 10          # percent the going rate has to be off before it is mentioned
# Words that say nothing about which card it is.
FILLER = {"the", "and", "card", "cards", "baseball", "football", "basketball", "hockey",
          "mlb", "nfl", "nba", "nhl", "rookie", "rc", "sp", "free", "shipping", "ship",
          "mint", "nm", "nrmt", "qty", "lot", "invest", "hot", "look", "wow", "read"}


def words(title: str) -> frozenset[str]:
    """The words of a title that identify the card, without the player's name or team."""
    who = profile.get()
    name = set(re.findall(r"[a-z0-9]+", " ".join(
        [who.player, who.search_name, *who.team_words]).lower()))
    found = re.findall(r"[a-z0-9]+", title.lower().replace("'", ""))
    return frozenset(w for w in found if w not in name and w not in FILLER)


def _due(con, now: float) -> list:
    """Auctions that have closed, then listings from before end times were kept."""
    return con.execute(
        "SELECT item_id, title, shipping FROM seen WHERE closed_at IS NULL AND ("
        " (is_auction=1 AND end_ts IS NOT NULL AND end_ts<?)"
        " OR (is_auction IS NULL AND last_seen<?)) "
        "ORDER BY end_ts IS NULL, end_ts LIMIT ?",
        (now - SETTLE, now - GONE_AFTER, PER_RUN)).fetchall()


def _outcome(raw: dict, now: float) -> tuple[str, dict]:
    """What one eBay answer means: 'sold', 'unsold', 'running' or 'skip'."""
    end = raw.get("itemEndDate")
    end_ts = (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
              if end else None)
    if "AUCTION" not in (raw.get("buyingOptions") or []):
        return "skip", {}   # a fixed price can hide an accepted offer; not a clean price
    if end_ts is None or end_ts > now:
        return "running", {"end_ts": end_ts}
    sold = sum(int(a.get("estimatedSoldQuantity") or 0)
               for a in raw.get("estimatedAvailabilities") or [])
    price, currency = _money(raw.get("currentBidPrice") or raw.get("price"))
    # No bids but "sold" is an offer the seller took; its price isn't shown.
    if not sold or not raw.get("bidCount") or price is None or currency != "USD":
        return "unsold", {}
    shipping = None
    for opt in raw.get("shippingOptions") or []:
        shipping, _ = _money(opt.get("shippingCost"))
        if shipping is not None:
            break
    return "sold", {"price": price, "shipping": shipping, "end_ts": end_ts,
                    "bids": raw.get("bidCount"),
                    "image": (raw.get("image") or {}).get("imageUrl")}


def collect(con, client: EbayClient | None = None, now: float | None = None) -> int:
    """Look up closed auctions and keep what the sold ones went for. Returns sales added."""
    now = now or time.time()
    rows = _due(con, now)
    if not rows:
        return 0
    client = client or EbayClient()
    added = 0
    for row in rows:
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
        if kind == "sold":
            shipping = info["shipping"] if info["shipping"] is not None else row["shipping"]
            con.execute(
                "INSERT OR REPLACE INTO sales (item_id, title, price, shipping, bids, sold_at,"
                " image) VALUES (?,?,?,?,?,?,?)",
                (row["item_id"], raw.get("title") or row["title"], info["price"],
                 shipping, info["bids"], int(info["end_ts"]), info["image"]))
            added += 1
        con.execute("UPDATE seen SET closed_at=? WHERE item_id=?", (int(now), row["item_id"]))
        con.commit()   # one at a time, so a dashboard button never waits on this loop
    con.execute("DELETE FROM sales WHERE sold_at<?", (now - KEEP_DAYS * 86400,))
    con.commit()
    return added


def relevant(con, items: list[Listing]) -> list[dict]:
    """Past sales whose titles look like these listings, newest first."""
    sales = [dict(r) for r in con.execute(
        "SELECT item_id, title, price, shipping, bids, sold_at FROM sales "
        "ORDER BY sold_at DESC")]
    if not sales:
        return []
    for sale in sales:
        sale["words"] = words(sale["title"])
    chosen: dict[str, dict] = {}
    for item in items:
        mine = words(item.title)
        if not mine:
            continue
        close = []
        for sale in sales:
            if sale["item_id"] == item.item_id or not sale["words"]:
                continue
            overlap = len(mine & sale["words"]) / len(mine | sale["words"])
            if overlap >= MIN_OVERLAP:
                close.append((overlap, sale))
        close.sort(key=lambda pair: (-pair[0], -pair[1]["sold_at"]))
        for _, sale in close[:PER_LISTING]:
            chosen[sale["item_id"]] = sale
    return sorted(chosen.values(), key=lambda s: -s["sold_at"])[:MAX_LINES]


def bid_count(bids: int) -> str:
    return "1 bid" if bids == 1 else f"{bids} bids"


def lines(sales: list[dict]) -> str:
    """The sales as the scoring prompt shows them, one per line."""
    out = []
    for s in sales:
        day = datetime.fromtimestamp(s["sold_at"], timezone.utc).astimezone(config.TZ)
        ship = "ship ?" if s["shipping"] is None else f"+{s['shipping']:.2f} ship"
        bids = f", {bid_count(s['bids'])}" if s["bids"] else ""
        out.append(f"{day:%Y-%m-%d} | ${s['price']:.2f} {ship}{bids} | {s['title']}")
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
            "SELECT s.item_id, s.title, s.price, s.shipping, s.bids, s.sold_at, s.image, p.data "
            "FROM sales s LEFT JOIN picks p USING (item_id) WHERE s.sold_at>=? "
            "ORDER BY s.sold_at DESC", (since,)):
        row = dict(row)
        pick = json.loads(row.pop("data")) if row["data"] else None
        row["total"] = round(row["price"] + (row["shipping"] or 0), 2)
        row["call"] = None
        if pick and pick.get("max") is not None:
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
    }


def track_record(con) -> str:
    """One line for the scoring prompt when the going rates have been running off."""
    recent = [r for r in with_calls(con) if r["call"] and r["off"] is not None][:TRACK_CALLS]
    if len(recent) < TRACK_MIN:
        return ""
    bias = round(100 * statistics.median(r["off"] for r in recent))
    if abs(bias) < TRACK_OFF:
        return ""
    way = "more" if bias > 0 else "less"
    return (f"- Your track record: of your last {len(recent)} picks whose auctions then sold, "
            f"the real final price with shipping was typically {abs(bias)}% {way} than the "
            f"\"value\" you gave. Correct for that when you estimate a going rate.\n")


def market(con, now: datetime, limit: int = 40) -> dict:
    """For the dashboard: the latest sold prices, and how the bot's own calls turned out."""
    rows = with_calls(con)
    week = now.timestamp() - 7 * 86400
    sold = [{
        "item_id": r["item_id"], "title": r["title"], "price": r["price"],
        "shipping": r["shipping"], "total": r["total"], "bids": r["bids"],
        "sold_at": datetime.fromtimestamp(r["sold_at"], config.TZ).isoformat(
            timespec="minutes"),
        "image": r["image"], "url": f"https://www.ebay.com/itm/{r['item_id']}",
        "call": r["call"]} for r in rows[:limit]]
    return {"count": len(rows), "count_7d": sum(r["sold_at"] >= week for r in rows),
            **scorecard(rows), "sold": sold, "history": history(con)}


SAME_CARD = 0.75        # title overlap that makes two listings the same card


def card_key(title: str) -> frozenset[str]:
    """The identifying words, with "07/50" read as "/50": which copy doesn't matter."""
    return words(re.sub(r"\b\d{1,3}\s?/\s?(\d{1,3})\b", r" \1 ", title))


def _same(a: frozenset[str], b: frozenset[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= SAME_CARD


def history(con, limit: int = 12) -> list[dict]:
    """Cards that have sold more than once, with every price: the start of a price trend."""
    groups: list[dict] = []
    for row in with_calls(con):   # newest first
        key = card_key(row["title"])
        group = next((g for g in groups if _same(key, g["key"])), None)
        if group is None:
            group = {"key": key, "title": row["title"], "image": row["image"],
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
            "last_at": datetime.fromtimestamp(g["sales"][0]["sold_at"], config.TZ).isoformat(
                timespec="minutes"),
            "url": f"https://www.ebay.com/itm/{g['item_id']}"})
    out.sort(key=lambda g: g["last_at"], reverse=True)
    out.sort(key=lambda g: -g["n"])
    return out[:limit]


def last_sold(rows: list[dict], title: str, item_id: str = "") -> dict | None:
    """The latest recorded sale of the same card as this title, and how many there are."""
    key = card_key(title)
    same = [r for r in rows if r["item_id"] != item_id and _same(key, card_key(r["title"]))]
    if not same:
        return None
    return {"total": same[0]["total"], "price": same[0]["price"], "n": len(same),
            "at": datetime.fromtimestamp(same[0]["sold_at"], config.TZ).date().isoformat()}


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
    return "\n".join(out)


def count(con, since: float = 0) -> int:
    return con.execute("SELECT count(*) FROM sales WHERE sold_at>=?", (since,)).fetchone()[0]


def main() -> None:
    con = db.connect()
    print(f"{collect(con)} sales added, {count(con)} on record")


if __name__ == "__main__":
    main()
