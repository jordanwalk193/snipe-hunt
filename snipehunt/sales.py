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


def lines(sales: list[dict]) -> str:
    """The sales as the scoring prompt shows them, one per line."""
    out = []
    for s in sales:
        day = datetime.fromtimestamp(s["sold_at"], timezone.utc).astimezone(config.TZ)
        ship = "ship ?" if s["shipping"] is None else f"+{s['shipping']:.2f} ship"
        bids = f", {s['bids']} bids" if s["bids"] else ""
        out.append(f"{day:%Y-%m-%d} | ${s['price']:.2f} {ship}{bids} | {s['title']}")
    return "\n".join(out)


def market(con, now: datetime, limit: int = 40) -> dict:
    """For the dashboard: the latest sold prices, and how the bot's own calls turned out.

    A call is a pick whose auction then sold. It is "under the max" when the
    final bid was at or below the max the bot gave, so a snipe at that max
    would have been in the running.
    """
    rows = [dict(r) for r in con.execute(
        "SELECT s.item_id, s.title, s.price, s.shipping, s.bids, s.sold_at, s.image, p.data "
        "FROM sales s LEFT JOIN picks p USING (item_id) ORDER BY s.sold_at DESC")]
    week = now.timestamp() - 7 * 86400
    sold, under, over, gaps = [], 0, 0, []
    for row in rows:
        pick = json.loads(row.pop("data")) if row["data"] else None
        total = round(row["price"] + (row["shipping"] or 0), 2)
        call = None
        if pick and pick.get("max") is not None:
            won = row["price"] <= pick["max"]
            under, over = under + won, over + (not won)
            if pick.get("value"):
                gaps.append(abs(total - pick["value"]) / pick["value"])
            call = {"tier": pick.get("tier"), "max": pick["max"], "value": pick.get("value"),
                    "under_max": won}
        if len(sold) < limit:
            sold.append({
                "item_id": row["item_id"], "title": row["title"], "price": row["price"],
                "shipping": row["shipping"], "total": total, "bids": row["bids"],
                "sold_at": datetime.fromtimestamp(row["sold_at"], config.TZ).isoformat(
                    timespec="minutes"),
                "image": row["image"], "url": f"https://www.ebay.com/itm/{row['item_id']}",
                "call": call})
    return {
        "count": len(rows), "count_7d": sum(r["sold_at"] >= week for r in rows),
        "calls": under + over, "under_max": under, "over_max": over,
        # how far the going-rate guess was from the real final price, typically
        "value_gap_pct": round(100 * statistics.median(gaps)) if gaps else None,
        "sold": sold,
    }


def count(con, since: float = 0) -> int:
    return con.execute("SELECT count(*) FROM sales WHERE sold_at>=?", (since,)).fetchone()[0]


def main() -> None:
    con = db.connect()
    print(f"{collect(con)} sales added, {count(con)} on record")


if __name__ == "__main__":
    main()
