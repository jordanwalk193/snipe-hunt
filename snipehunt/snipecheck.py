"""A second opinion on the amounts you put into Gixen.

Read-only, like everything that touches Gixen: this only looks at the snipes
you set and says how each amount compares with what the card goes for. The
going rate comes from the bot's own pick for that auction, then from its record
of what the same card sold for, and for a card it knows nothing about, from
one Claude appraisal that is kept so it is only ever paid for once.
"""
from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

from . import db, profile, sales, settings

KEY = "snipe_values"
MAX_APPRAISED = 20       # snipes in one appraisal call
KEEP_DAYS = 30
# How far under (or over) the going rate you would pay, shipping included.
OVER, FAIR, LONG_SHOT = -5, 15, 45
VERDICTS = {
    "outbid": "Already bid past your snipe",
    "over": "More than it sells for",
    "fair": "About the going rate: likely to win",
    "steal": "A real discount if it holds",
    "long_shot": "Far under what it sells for: rarely wins",
    "unknown": "No price to judge it by yet",
}
PROMPT = """You are {app}, a card-collecting assistant. {collector} collects \
{player} and has set snipes on the eBay auctions below. For each one, say what \
that exact card (same parallel, serial number and grade) has recently sold for, \
shipping included: its going rate. You only read and recommend; never bid.
{lookup_rule}
{sold}Reply with ONLY one JSON object, no prose, no code fence:
{{"values": [{{"item_id": "…", "value": 25.00, "basis": "sales|estimate", \
"note": "one short line: the sales you found, or why it is an estimate"}}]}}

=== snipes (JSON lines) ===
{snipes}
"""
LOOKUPS = ("Check recent sold prices with web search (130point.com, eBay sold listings) for "
           "at most {lookups} of them, the dearest first, and ask for them all in one step. "
           "Do not open ebay.com pages. Use \"estimate\" where you found no sale.")
NO_LOOKUPS = "You have no web access in this run: give your honest best estimate for each."


def _cache(con) -> dict:
    return json.loads(db.kv_get(con, KEY, "{}"))


def going_rate(con, snipe: dict, rows: list[dict], cache: dict) -> dict | None:
    """What this card goes for and how we know: {"value", "basis", "note"}."""
    row = con.execute("SELECT data FROM picks WHERE item_id=?", (snipe["item_id"],)).fetchone()
    pick = json.loads(row["data"]) if row else {}
    if pick.get("value"):
        return {"value": pick["value"], "note": pick.get("comps") or "",
                "basis": "sales" if pick.get("max_basis") == "sales" else "estimate"}
    last = sales.last_sold(rows, snipe.get("title") or "", snipe["item_id"])
    if last:
        return {"value": last["total"], "basis": "sold",
                "note": f"the same card sold for ${last['total']:.2f} delivered"
                        + (" (shipping assumed)" if last["estimated"] else "")
                        + f" on {last['at']}"
                        + (f" ({last['n']} sales on record)" if last["n"] > 1 else "")}
    return cache.get(snipe["item_id"])


def _shipping(con, item_id: str) -> float | None:
    row = con.execute("SELECT shipping FROM seen WHERE item_id=?", (item_id,)).fetchone()
    return row["shipping"] if row else None


def judge(snipe: dict, rate: dict | None, shipping: float | None) -> dict:
    """One snipe's verdict: how its amount sits against the going rate."""
    amount, bid = snipe.get("max_bid"), snipe.get("current_bid") or 0
    ship = sales.UNKNOWN_SHIPPING if shipping is None else shipping
    out = {"verdict": "unknown", "value": None, "basis": None, "note": "", "pct": None,
           "win": None}
    if rate and rate.get("value") and amount:
        value = float(rate["value"])
        pct = round(100 * (1 - (amount + ship) / value))
        out.update(value=round(value, 2), basis=rate.get("basis"), pct=pct,
                   note=str(rate.get("note") or "")[:200],
                   win=sales.win_bid(value, shipping, amount),
                   verdict="over" if pct < OVER else "fair" if pct < FAIR
                   else "steal" if pct < LONG_SHOT else "long_shot")
    if amount and bid >= amount:
        out["verdict"] = "outbid"
        # still under the going rate at today's bid: worth knowing before it ends
        out["room"] = bool(out["value"]) and bid + ship < out["value"] * 0.9
    out["label"] = VERDICTS[out["verdict"]]
    return out


def check(con, snipes: list[dict]) -> tuple[list[dict], dict]:
    """Every snipe's verdict, in order, and a summary of the lot."""
    rows, cache = sales.with_calls(con), _cache(con)
    checks = [judge(s, going_rate(con, s, rows, cache), _shipping(con, s["item_id"]))
              for s in snipes]
    live = [(s, c) for s, c in zip(snipes, checks) if c["verdict"] != "outbid"]
    valued = [(s, c) for s, c in live if c["value"]]
    counts = {name: sum(c["verdict"] == name for c in checks) for name in VERDICTS}
    return checks, {
        **counts, "snipes": len(snipes),
        # what the snipes still in play would cost at their maxes, and what those cards go for
        "committed": round(sum(s["max_bid"] or 0 for s, _ in valued), 2),
        "worth": round(sum(c["value"] for _, c in valued), 2),
    }


def appraise(con, snipes: list[dict], now: float | None = None) -> int:
    """Ask Claude once for the going rate of snipes nothing else can price. Returns how many."""
    from . import score   # score imports sales; kept out of module import to avoid a loop
    now = now or time.time()
    rows, cache = sales.with_calls(con), _cache(con)
    cache = {k: v for k, v in cache.items() if v.get("at", 0) > now - KEEP_DAYS * 86400}
    todo = [s for s in snipes if s.get("title")
            and going_rate(con, s, rows, cache) is None][:MAX_APPRAISED]
    if not todo:
        return 0
    who, lookups = profile.get(), settings.load()["price_lookups"]
    sold = sales.relevant(con, [SimpleNamespace(item_id=s["item_id"], title=s["title"])
                                for s in todo])
    prompt = PROMPT.format(
        app=who.app_name, collector=who.collector, player=who.described,
        lookup_rule=LOOKUPS.format(lookups=lookups) if lookups else NO_LOOKUPS,
        sold=score.SOLD.format(lines=sales.lines(sold)) if sold else "",
        snipes="\n".join(json.dumps({
            "item_id": s["item_id"], "title": s["title"], "bid_now": s.get("current_bid"),
            "ends": s.get("end_time")}, ensure_ascii=False) for s in todo))
    # A Claude call takes a minute: don't hold the write lock through it (the Gixen
    # cache read above writes), or the dashboard and the cost record wait on it.
    con.commit()
    reply = score.extract_json(score.ask_claude(
        prompt, tools=score.WEB_TOOLS if lookups else ""))
    asked, done = {s["item_id"] for s in todo}, 0
    for raw in reply.get("values") or []:
        try:
            item_id, value = str(raw["item_id"]), round(float(raw["value"]), 2)
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in asked and value > 0:
            cache[item_id] = {"value": value, "at": int(now),
                              "basis": "sales" if raw.get("basis") == "sales" else "estimate",
                              "note": str(raw.get("note") or "")[:200]}
            done += 1
    db.kv_set(con, KEY, json.dumps(cache, ensure_ascii=False))
    print(f"snipecheck: {done} of {len(todo)} snipes appraised", file=sys.stderr)
    return done
