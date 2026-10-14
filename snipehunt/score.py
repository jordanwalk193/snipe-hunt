"""Steal scoring: hand new listings to Claude with the inventory and want list.

Claude only reads and recommends. It is given web fetch/search for sold
prices and nothing that can act on eBay or Gixen.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys

from datetime import datetime

from . import config, profile, sales, settings, usage
from .ebay_search import Listing

WEB_TOOLS = "WebFetch,WebSearch"
BATCH = 80
MAX_BATCHES = 6          # anything beyond this waits for the next hourly run
TIMEOUT = 1200
TIERS = {"fire", "green", "yellow"}
# Why a pick is more than a fair price: a seller's pricing mistake, a listing
# buyers won't find, or an auction nobody is watching.
EDGES = {"mispriced", "hidden", "sleeper"}
EDGE_MIN_PCT = 25         # an edge has to be this far under the going rate
UNKNOWN_SHIPPING = sales.UNKNOWN_SHIPPING   # what the prompt tells Claude to assume for "ship ?"
# The inventory columns that say which card a row is. Scoring only needs these
# to tell what is already owned; order dates, sellers and notes are left out
# because every extra word is paid for on every run.
CARD_COLUMNS = ("year", "set", "card", "parallel", "card_no", "serial", "rc", "auto",
                "relic", "graded", "status")
# Replaces the Claude CLI's own long coding-assistant instructions, which this
# job has no use for.
SYSTEM = ("You are a careful sports card collecting assistant. You read and "
          "recommend; you never bid, buy or contact anyone. Today is {today}.")
# A deal's stars: how far under its going rate you would pay, shipping included.
STARS = ((50, 5), (35, 4), (20, 3), (10, 2))


def rate(pay: float, value: float | None) -> tuple[int | None, int | None]:
    """Return (stars 1-5, percent under value) for paying `pay` on a card worth `value`."""
    if not value or value <= 0:
        return None, None
    pct = round(100 * (1 - pay / value))
    return next((stars for floor, stars in STARS if pct >= floor), 1), max(pct, 0)



def rerate(pick: dict) -> dict:
    """Work a pick's deal stars out again after its price or shipping changed."""
    ship = pick["shipping"] if pick.get("shipping") is not None else UNKNOWN_SHIPPING
    pay = (pick["max"] if pick.get("is_auction") else pick["price"]) + ship
    pick["deal"], pick["deal_pct"] = rate(pay, pick.get("value"))
    if pick.get("edge") and (pick["deal_pct"] or 0) < EDGE_MIN_PCT:
        pick["edge"] = None   # the numbers no longer back the claim
    return pick


PROMPT = """You are {app}, a card-collecting assistant. {collector} collects \
{player}. Review the eBay listings below and pick out the steals.

HARD RULES: recommend only. Never bid, buy, make offers, message sellers or \
touch Gixen. You may only read web pages.

How to judge:
- Drop anything that isn't a {name} card. Listings marked match=unclear need \
your judgement; list the ones you reject in "not_player".
- Skip cards {collector} already owns (same set, card number and parallel in \
the inventory) unless it is a clearly better copy worth having, e.g. a graded \
copy of a key card.
- Favor unique cards and new photos over more parallels of cards already owned.
- Follow preferences.md and wantlist.md for what to chase and what to skip.
- Always count shipping. "ship ?" means unknown: assume about $5 unless the \
seller is a known cheap-shipping consignment seller.
- Set "max" as an odd-cent cheapskate snipe (3.03, 6.06, 12.12, 25.25, 35.35…) \
unless preferences.md asks for a different style. The max is the bid itself, \
shipping not included, set so that winning AT the max plus shipping still \
beats recent sales.
{lookup_rule}
{sold_rule}- Set "value" to what this exact card (same parallel, serial and grade) has \
recently sold for, shipping included: the going rate, not your max. Use any \
sold prices you looked up; when you have none, give your honest best estimate.
- Auctions whose current price already exceeds your max are not steals; leave \
them out. Buy It Now only counts when the price is at or under \
your max.
- Hunt hardest for cards that slipped through the cracks, and say which kind in \
"edge": "mispriced" = a Buy It Now or best-offer listing priced well under what \
the card sells for (a seller's mistake or a quick-sale price); "hidden" = one \
other buyers will not find or will pass over: the player's name misspelled or \
missing, the wrong year or set, a key feature (autograph, serial number, short \
print, grade) left out of the title, a poor photo of a good card, or a good \
card buried in a lot; "sleeper" = an auction ending within about a day with \
few or no bids and still far under its going rate. Use "" for a card that is \
only a fair price. Claim an edge only when you are fairly sure of the going \
rate and paying your max plus shipping would still be at least 25% under it.
- A listing marked "found": "by card number…" came from a search for one of \
{name}'s card numbers and does not name the player. If the year, set and card \
number make it {name}'s card, it is "hidden" by definition and worth a close \
look; if that number belongs to another player in that set, put it in "not_player".
- Tiers: "fire" = brag-worthy (autos, patch autos, /50 or lower, case hits, \
SSPs, want-list hits); "green" = numbered or clear value; "yellow" = cheap \
card not yet in the collection.
- Be picky. Most listings are not steals. It is fine to return no picks.

Reply with ONLY one JSON object, no prose, no code fence:
{{"picks": [{{"item_id": "…", "card": "short card name, e.g. 2023 Bowman's \
Best Refractor PSA 10", "tier": "fire|green|yellow", "max": 12.12, "max_basis": \
"sales|estimate", "comps": "what recent sales show, or why it is an estimate", \
"value": 25.00, "edge": "mispriced|hidden|sleeper or empty", "why": "one short \
phone-friendly line; for an edge, say what the seller got wrong"}}], "not_player": ["item_id", …]}}

=== preferences.md ===
{preferences}

=== wantlist.md ===
{wantlist}

=== inventory.csv ===
{inventory}

{sold}=== listings (JSON lines) ===
{listings}
"""

SOLD_RULE = """- "sold prices seen" below are real eBay auction results this bot recorded \
itself. When one is the same card (same set, parallel, serial and grade), \
price from it: set max_basis to "sales", quote it in comps, and spend no \
lookup on that card. A line ending "(different grade/parallel)" is the same \
card in another grade or print run: only a hint, never a price.
"""
SOLD = """=== sold prices seen (date | final bid, shipping | title) ===
{lines}

"""


def _compact(item: Listing) -> str:
    return json.dumps({
        "item_id": item.item_id, "title": item.title, "price": item.price,
        "shipping": "ship ?" if item.shipping is None else item.shipping,
        "type": "auction" if item.is_auction else (
            "best_offer" if "BEST_OFFER" in item.buying_options else "buy_it_now"),
        "bids": item.bid_count, "ends": item.end_time, "seller": item.seller,
        "match": item.match,
        **({"found": "by card number; the title leaves the player's name out"}
           if any(q.startswith("no-name") for q in item.queries) else {}),
    }, ensure_ascii=False)


LOOKUPS = """- For anything you would max above $10, check recent sold prices with web \
search (130point.com, eBay sold listings) and set max_basis to "sales". \
Otherwise use "estimate". Do at most {lookups} lookups and ask for them all in \
one step; if one fails, use "estimate" and say so in comps. Do not open \
ebay.com pages: eBay blocks them, so a fetch there is wasted."""
NO_LOOKUPS = """- You have no web access in this run. Price every card from what you \
know, set max_basis to "estimate", and be more cautious with your max the less \
sure you are."""


def inventory_for_scoring() -> str:
    """The inventory cut down to the columns that identify a card."""
    text = config.INVENTORY.read_text()
    reader = csv.DictReader(io.StringIO(text))
    keep = [c for c in reader.fieldnames or [] if c in CARD_COLUMNS]
    if len(keep) < 3:   # a layout of the collector's own: send it as it is
        return text
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=keep, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(reader)
    return out.getvalue()


def build_prompt(items: list[Listing], lookups: int = 8,
                 sold: list[dict] | None = None, track: str = "") -> str:
    who = profile.get()
    return PROMPT.format(
        sold_rule=(SOLD_RULE if sold else "") + track,
        sold=SOLD.format(lines=sales.lines(sold)) if sold else "",
        app=who.app_name, collector=who.collector, player=who.described,
        name=who.player,
        lookup_rule=LOOKUPS.format(lookups=lookups) if lookups else NO_LOOKUPS,
        preferences=config.PREFERENCES.read_text(),
        wantlist=config.WANTLIST.read_text(),
        inventory=inventory_for_scoring(),
        listings="\n".join(_compact(i) for i in items))


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in Claude's reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def ask_claude(prompt: str, tools: str = WEB_TOOLS,
               timeout: int = TIMEOUT) -> str:
    """Run `claude -p` in the state directory, away from any project files.

    Listing titles and email bodies are text written by strangers, so Claude
    gets only the tools named here: `--tools` removes everything else (shell,
    file edits) and `--strict-mcp-config` drops any MCP servers set up for this user.
    """
    cmd = [config.get("CLAUDE_BIN"), "-p", "--output-format", "json",
           "--tools", tools, "--strict-mcp-config",
           "--no-session-persistence",   # or every call leaves a transcript on disk
           "--system-prompt",
           SYSTEM.format(today=datetime.now(config.TZ).strftime("%A, %B %-d, %Y"))]
    if tools:
        cmd += ["--allowedTools", tools]
    if config.get("CLAUDE_MODEL"):   # a smaller model costs less; see the README
        cmd += ["--model", config.get("CLAUDE_MODEL")]
    if config.get("CLAUDE_EFFORT"):   # how hard Claude thinks: low, medium or high
        cmd += ["--effort", config.get("CLAUDE_EFFORT")]
    env = dict(os.environ)
    if config.get("ANTHROPIC_API_KEY"):   # may come from the env file, not the environment
        env["ANTHROPIC_API_KEY"] = config.get("ANTHROPIC_API_KEY")
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env,
                              timeout=timeout, cwd=str(config.ensure_state_dir()))
    except FileNotFoundError:
        raise RuntimeError(
            f"The Claude CLI ({cmd[0]!r}) wasn't found. Install it with `npm install -g "
            "@anthropic-ai/claude-code`, or set CLAUDE_BIN to its full path") from None
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exit {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:300]}")
    outer = json.loads(proc.stdout)
    usage.record(outer.get("total_cost_usd") or 0)
    if outer.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(outer.get('result'))[:300]}")
    return outer.get("result", "")


def _pick_dict(item: Listing, raw: dict) -> dict | None:
    tier = str(raw.get("tier", "")).lower()
    try:
        max_bid = round(float(raw["max"]), 2)
    except (KeyError, TypeError, ValueError):
        return None
    if tier not in TIERS:
        return None
    # Belt and braces: never surface something already priced past the max.
    if item.price > max_bid:
        return None
    try:
        value = round(float(raw["value"]), 2)
    except (KeyError, TypeError, ValueError):
        value = None
    # What you would really pay: the asking price, or the snipe if it wins at its max.
    ship = item.shipping if item.shipping is not None else UNKNOWN_SHIPPING
    deal, deal_pct = rate((max_bid if item.is_auction else item.price) + ship, value)
    edge = str(raw.get("edge") or "").lower()
    if edge not in EDGES or (deal_pct or 0) < EDGE_MIN_PCT or (
            edge == "mispriced" and item.is_auction):
        edge = None   # the numbers have to back the claim
    return {
        "item_id": item.item_id, "card": str(raw.get("card") or item.title)[:90],
        "tier": tier, "max": max_bid, "value": value, "deal": deal, "deal_pct": deal_pct,
        "edge": edge,
        "win": sales.win_bid(value, item.shipping, max_bid) if item.is_auction else None,
        "max_basis": "sales" if raw.get("max_basis") == "sales" else "estimate",
        "comps": str(raw.get("comps", ""))[:300], "why": str(raw.get("why", ""))[:140],
        "title": item.title, "price": item.price, "shipping": item.shipping,
        "total": item.total, "is_auction": item.is_auction,
        "best_offer": "BEST_OFFER" in item.buying_options,
        "end_time": item.end_time, "seller": item.seller,
        "url": item.url, "image": item.image,
    }


def score(items: list[Listing], verbose: bool = False, on_batch=None, con=None
          ) -> tuple[list[dict], list[Listing]]:
    """Return (picks, listings that were actually scored).

    `on_batch(batch, batch_picks)` runs after each batch so the caller can
    save progress. A batch that fails ends the run early; what was scored
    before it is kept and the rest waits for the next hourly run. With `con`,
    each batch is shown what similar cards sold for, and how far off the going
    rates have been running (see sales.py).
    """
    items = sorted(items, key=lambda i: (not i.is_auction, i.end_time or "9"))
    # Sold-price lookups are close to half of what a run costs; 0 turns them off.
    lookups = settings.load()["price_lookups"]
    picks: list[dict] = []
    scored: list[Listing] = []
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)][:MAX_BATCHES]
    for n, batch in enumerate(batches, 1):
        by_id = {i.item_id: i for i in batch}
        try:
            sold = sales.relevant(con, batch) if con is not None else []
            track = sales.track_record(con) if con is not None else ""
            reply = extract_json(ask_claude(
                build_prompt(batch, lookups, sold, track),
                tools=WEB_TOOLS if lookups else ""))
        except Exception as exc:
            if not scored:
                raise
            print(f"score: batch {n}/{len(batches)} failed, keeping the first "
                  f"{len(scored)} listings: {exc}", file=sys.stderr)
            break
        batch_picks: list[dict] = []
        for raw in reply.get("picks", []) or []:
            item = by_id.get(str(raw.get("item_id")))
            pick = _pick_dict(item, raw) if item else None
            if pick:
                batch_picks.append(pick)
        picks.extend(batch_picks)
        scored.extend(batch)
        if on_batch:
            on_batch(batch, batch_picks)
        if verbose:
            print(f"score: batch {n}/{len(batches)}: {len(batch)} listings, "
                  f"{len(reply.get('picks', []) or [])} picks, "
                  f"{len(reply.get('not_player', []) or [])} rejected", file=sys.stderr)
    return picks, scored


def main() -> None:
    ap = argparse.ArgumentParser(description="Score listings (reads JSON from "
                                 "`python -m snipehunt.ebay_search --json` on stdin)")
    ap.add_argument("--limit", type=int, default=BATCH)
    args = ap.parse_args()
    raw = json.load(sys.stdin)[: args.limit]
    items = [Listing(**{k: v for k, v in r.items() if k != "total"}) for r in raw]
    picks, _ = score(items, verbose=True)
    print(json.dumps(picks, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
