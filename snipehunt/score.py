"""Steal scoring: hand new listings to Claude with the inventory and want list.

Claude only reads and recommends. It is given web fetch/search for sold
prices and nothing that can act on eBay or Gixen.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import config, profile
from .ebay_search import Listing

BATCH = 80
MAX_BATCHES = 6          # anything beyond this waits for the next hourly run
TIMEOUT = 1200
TIERS = {"fire", "green", "yellow"}

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
- For anything you would max above $10, check recent sold prices with web \
fetch/search (130point.com, eBay sold listings) and set max_basis to "sales". \
Otherwise use "estimate". Do at most {lookups} lookups; if one fails, use \
"estimate" and say so in comps.
- Auctions whose current price already exceeds your max are not steals; leave \
them out. Buy It Now only counts when the price is at or under \
your max.
- Tiers: "fire" = brag-worthy (autos, patch autos, /50 or lower, case hits, \
SSPs, want-list hits); "green" = numbered or clear value; "yellow" = cheap \
card not yet in the collection.
- Be picky. Most listings are not steals. It is fine to return no picks.

Reply with ONLY one JSON object, no prose, no code fence:
{{"picks": [{{"item_id": "…", "card": "short card name, e.g. 2023 Bowman's \
Best Refractor PSA 10", "tier": "fire|green|yellow", "max": 12.12, "max_basis": \
"sales|estimate", "comps": "what recent sales show, or why it is an estimate", \
"why": "one short phone-friendly line"}}], "not_player": ["item_id", …]}}

=== preferences.md ===
{preferences}

=== wantlist.md ===
{wantlist}

=== inventory.csv ===
{inventory}

=== listings (JSON lines) ===
{listings}
"""


def _compact(item: Listing) -> str:
    return json.dumps({
        "item_id": item.item_id, "title": item.title, "price": item.price,
        "shipping": "ship ?" if item.shipping is None else item.shipping,
        "type": "auction" if item.is_auction else (
            "best_offer" if "BEST_OFFER" in item.buying_options else "buy_it_now"),
        "bids": item.bid_count, "ends": item.end_time, "seller": item.seller,
        "match": item.match,
    }, ensure_ascii=False)


def build_prompt(items: list[Listing], lookups: int = 8) -> str:
    who = profile.get()
    return PROMPT.format(
        app=who.app_name, collector=who.collector, player=who.described,
        name=who.player, lookups=lookups,
        preferences=config.PREFERENCES.read_text(),
        wantlist=config.WANTLIST.read_text(),
        inventory=config.INVENTORY.read_text(),
        listings="\n".join(_compact(i) for i in items))


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in Claude's reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def ask_claude(prompt: str, tools: str = "WebFetch,WebSearch",
               timeout: int = TIMEOUT) -> str:
    """Run `claude -p` in the state directory, away from any project files.

    Listing titles and email bodies are text written by strangers, so Claude
    gets only the tools named here: `--tools` removes everything else (shell,
    file edits) and `--strict-mcp-config` drops any MCP servers set up for this user.
    """
    cmd = [config.get("CLAUDE_BIN"), "-p", "--output-format", "json",
           "--tools", tools, "--strict-mcp-config"]
    if tools:
        cmd += ["--allowedTools", tools]
    env = dict(os.environ)
    if config.get("ANTHROPIC_API_KEY"):   # may come from the env file, not the environment
        env["ANTHROPIC_API_KEY"] = config.get("ANTHROPIC_API_KEY")
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env,
                          timeout=timeout, cwd=str(config.ensure_state_dir()))
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exit {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:300]}")
    outer = json.loads(proc.stdout)
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
    return {
        "item_id": item.item_id, "card": str(raw.get("card") or item.title)[:90],
        "tier": tier, "max": max_bid,
        "max_basis": "sales" if raw.get("max_basis") == "sales" else "estimate",
        "comps": str(raw.get("comps", ""))[:300], "why": str(raw.get("why", ""))[:140],
        "title": item.title, "price": item.price, "shipping": item.shipping,
        "total": item.total, "is_auction": item.is_auction,
        "best_offer": "BEST_OFFER" in item.buying_options,
        "end_time": item.end_time, "seller": item.seller,
        "url": item.url, "image": item.image,
    }


def score(items: list[Listing], verbose: bool = False, on_batch=None
          ) -> tuple[list[dict], list[Listing]]:
    """Return (picks, listings that were actually scored).

    `on_batch(batch, batch_picks)` runs after each batch so the caller can
    save progress. A batch that fails ends the run early; what was scored
    before it is kept and the rest waits for the next hourly run.
    """
    items = sorted(items, key=lambda i: (not i.is_auction, i.end_time or "9"))
    picks: list[dict] = []
    scored: list[Listing] = []
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)][:MAX_BATCHES]
    for n, batch in enumerate(batches, 1):
        by_id = {i.item_id: i for i in batch}
        try:
            reply = extract_json(ask_claude(build_prompt(batch)))
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
