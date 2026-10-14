"""Weekly report: wins and losses, unpaid wins, deliveries, snipes, totals."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import config, db, inventory, profile, sales, settings, tune
from .notify import TIER_ICON, TIER_ORDER
from .score import ask_claude

UNPAID = inventory.UNPAID

MONTHLY_PROMPT = """Today is {today}. {collector} collects {player}. Recommend \
only; you may only read web pages.

From the inventory below:
1. Pick the 10 most valuable cards. For each, check recent sold prices with \
web fetch/search (130point.com, eBay sold listings) and compare with the price \
paid. Say "estimate" when you could not find sales.
2. List the duplicates (dupe = Y, or the same set, card number and parallel \
twice) that could be traded.
3. Name any raw cards worth grading, and why. Skip this if none are.

Reply in short Markdown with the headings "### Top 10 value check" (a table: \
card, paid, recent sales, basis), "### Duplicates to trade" and \
"### Worth grading". No preamble.

=== inventory.csv ===
{inventory}"""


def card_name(row: dict) -> str:
    parts = [row.get("year"), row.get("set"), row.get("card")]
    if row.get("parallel") and row["parallel"] != "Base":
        parts.append(row["parallel"])
    if row.get("card_no"):
        parts.append(f"#{row['card_no']}")
    if row.get("serial"):
        parts.append(row["serial"])
    return " ".join(p for p in parts if p)


def binder_page(row: dict) -> str:
    if row.get("front_page") == "Y":
        return "front page"
    return profile.get().binder_page(row.get("year"))


def totals(rows: list[dict]) -> dict:
    owned = [r for r in rows if r.get("status") != "Sold-Traded"]
    spent, unpriced = 0.0, 0
    for r in owned:
        try:
            spent += float(r["price_paid"])
        except (KeyError, ValueError):
            unpriced += 1
    return {"cards": len(owned), "spent": round(spent, 2), "unpriced": unpriced,
            "in_binder": sum(r.get("status") == "In binder" for r in owned),
            "to_file": sum(r.get("status") == "Delivered" for r in owned)}


def _dedupe(events: list[dict]) -> list[dict]:
    seen, out = set(), []
    for ev in events:
        key = str(ev.get("item_id") or ev.get("title") or "").lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def wins_and_losses(events: list[dict]) -> tuple[list[dict], list[dict]]:
    wins = _dedupe([e for e in events if e.get("type") in ("won", "offer_accepted")])
    won_ids = {str(e.get("item_id")) for e in wins if e.get("item_id")}
    # An outbid notice followed by a win on the same item is not a loss.
    losses = _dedupe([e for e in events if e.get("type") == "lost"
                      and str(e.get("item_id")) not in won_ids])
    return wins, losses


def unpaid_by_seller(rows: list[dict], picks: list[dict]) -> list[dict]:
    """Group unpaid wins by seller and say which to hold for a combined invoice."""
    pending_sellers = {p.get("seller") for p in picks if p.get("is_auction")}
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("status") in UNPAID:
            groups.setdefault(r.get("seller") or "unknown seller", []).append(r)
    out = []
    for seller, cards in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if seller in pending_sellers:
            advice = "hold: another snipe from this seller is still open"
        elif len(cards) > 1:
            advice = "request a combined invoice, then pay once"
        elif seller in settings.load()["sellers"]:
            advice = "hold a few days for more wins, then one combined invoice"
        else:
            advice = "pay now"
        out.append({"seller": seller, "cards": cards, "advice": advice})
    return out


def delivered_this_week(rows: list[dict], events: list[dict]) -> list[dict]:
    found, ids = [], set()
    for ev in events:
        if ev.get("type") != "delivered":
            continue
        row = inventory.find(rows, str(ev.get("item_id") or ""), ev.get("match_row"))
        if row and row["id"] not in ids and row.get("status") == "Delivered":
            ids.add(row["id"])
            found.append(row)
    return found


def upcoming(picks: list[dict], now: datetime, limit: int = 10) -> list[dict]:
    live = []
    for p in picks:
        if p.get("tier") == "drop":
            continue
        if p.get("is_auction"):
            if not p.get("end_time") or datetime.fromisoformat(p["end_time"]) <= now:
                continue
        live.append(p)
    live.sort(key=lambda p: (TIER_ORDER.get(p.get("tier"), 9), p.get("end_time") or "9"))
    return live[:limit]


def _money(value) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "$?"


def _pick_line(p: dict) -> str:
    when = "Buy It Now"
    if p.get("is_auction") and p.get("end_time"):
        when = "ends " + config.local(
            datetime.fromisoformat(p["end_time"])).strftime("%a %-I:%M %p %Z")
    return (f"- {TIER_ICON.get(p.get('tier'), '•')} [{p['card']}]({p.get('url', '')}) · "
            f"{_money(p.get('price'))} now · {when} · **Max: {p['max']:.2f}** "
            f"({p.get('max_basis', 'estimate')}) · item `{p['item_id']}`")


def sold_section(con, since: datetime, top: int = 5) -> list[str]:
    """What closed auctions went for this week, and how the bot's calls on them did."""
    rows = sales.with_calls(con, since.timestamp())
    fixed = sum(r["kind"] == "bin" for r in rows)
    md = ["", f"## What sold this week ({len(rows) - fixed} auctions"
          + (f", {fixed} at a fixed price)" if fixed else ")")]
    if not rows:
        return md + ["- No watched auction closed with bids this week."]
    for r in sorted(rows, key=lambda r: -r["price"])[:top]:
        call = r["call"]
        note = "" if not call else (
            f" · bot's max {call['max']:.2f}: "
            + ("under it" if call["under_max"] else "went over"))
        md.append(f"- [{r['title']}](https://www.ebay.com/itm/{r['item_id']}) · "
                  f"**{_money(r['price'])}**"
                  + (f" · {sales.bid_count(r['bids'])}" if r["bids"] else "")
                  + (" · Buy It Now" if r["kind"] == "bin" else "") + note)
    card = sales.scorecard(rows)
    if card["calls"]:
        line = (f"- The bot had picked {card['calls']} of these: {card['under_max']} closed "
                f"at or under its max, {card['over_max']} went over.")
        if card["value_bias_pct"] is not None and card["valued"] >= 3:
            way = "above" if card["value_bias_pct"] > 0 else "below"
            line += (f" Final prices ran {abs(card['value_bias_pct'])}% {way} "
                     "its going rates.")
        md.append(line)
    return md


def missed_section(con, now: datetime, top: int = 10) -> list[str]:
    """This week's sales the bot made no pick for that went well under the card's usual price."""
    found = sales.find_missed(con, now.timestamp(), days=7)[:top]
    md = ["", f"## Missed steals ({len(found)})"]
    if not found:
        return md + ["- None: nothing the bot passed on sold well under its usual price."]
    for m in found:
        md.append(f"- [{m['title']}]({m['url']}) · **{_money(m['total'])}** delivered"
                  + (" · Buy It Now" if m["kind"] == "bin" else "")
                  + f" · typically {_money(m['typical'])} ({m['pct']}% under, "
                  f"{m['n']} earlier sales)")
    return md


def monthly_section() -> str:
    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    who = profile.get()
    return ask_claude(MONTHLY_PROMPT.format(
        today=today, collector=who.collector, player=who.described, inventory=config.INVENTORY.read_text()), timeout=1800).strip()


def build(con, now: datetime | None = None, releases: list[dict] | None = None,
          monthly: str | None = None, notes: list[str] | None = None
          ) -> tuple[str, str]:
    """Return (Markdown report, short phone summary)."""
    now = now or datetime.now(config.TZ)
    since = now - timedelta(days=7)
    _, rows = inventory.read()
    events = db.mail_events(con, since.strftime("%Y-%m-%d"))
    picks = db.recent_picks(con, int(since.timestamp()))
    wins, losses = wins_and_losses(events)
    unpaid = unpaid_by_seller(rows, picks)
    delivered = delivered_this_week(rows, events)
    snipes = upcoming(picks, now)
    tot = totals(rows)
    n_unpaid = sum(len(g["cards"]) for g in unpaid)

    who = profile.get()
    md = [f"# {who.app_name} weekly report: {now.strftime('%Y-%m-%d')}", "",
          f"Week of {since.strftime('%b %-d')} to {now.strftime('%b %-d, %Y')}.", "",
          "## Collection",
          f"- **{tot['cards']} cards**, **${tot['spent']:.2f} spent**"
          + (f" ({tot['unpriced']} cards have no price recorded yet)" if tot["unpriced"] else ""),
          f"- {tot['in_binder']} in the binder, {tot['to_file']} delivered and waiting to be filed",
          "", f"## Wins and losses ({len(wins)} won, {len(losses)} lost)"]
    for ev in wins:
        md.append(f"- ✅ {ev.get('title') or 'item ' + str(ev.get('item_id'))} · "
                  f"{_money(ev.get('price'))} · {ev.get('seller') or '?'}"
                  + (" · Gixen" if ev.get("source") == "gixen" else ""))
    for ev in losses:
        md.append(f"- ❌ {ev.get('title') or 'item ' + str(ev.get('item_id'))}")
    if not wins and not losses:
        md.append("- No auction results in this week's email.")

    md += ["", f"## Unpaid wins ({n_unpaid})"]
    for g in unpaid:
        md.append(f"- **{g['seller']}** ({len(g['cards'])}): {g['advice']}")
        md += [f"  - #{r['id']} {card_name(r)} · {_money(r.get('price_paid') or None)}"
               for r in g["cards"]]
    if not unpaid:
        md.append("- Nothing waiting on payment.")

    md += ["", f"## Delivered this week ({len(delivered)})"]
    md += [f"- #{r['id']} {card_name(r)} → **{binder_page(r)}**" for r in delivered]
    if not delivered:
        md.append("- No delivery emails this week.")

    md += ["", "## Best upcoming snipes"]
    md += [_pick_line(p) for p in snipes] or ["- None on the board right now."]
    md += sold_section(con, since)
    md += missed_section(con, now)

    md += ["", "## Self-tuning"]
    md += tune.lines(tune.view(con, 6)) or [
        "- No settings have been tuned yet." if settings.load(con)["self_tuning"]
        else "- Switched off."]

    if releases is not None:
        md += ["", "## New releases to watch"]
        for c in releases:
            date = f" ({c['release_date']})" if c.get("release_date") else ""
            md.append(f"- **{c.get('product', '?')}**{date}: {c.get('card', '')}"
                      + (f" · {c['note']}" if c.get("note") else ""))
        if not releases:
            md.append(f"- No new {who.last} cards found on recent checklists.")

    if monthly:
        md += ["", "## Monthly check", "", monthly]
    if notes:
        md += ["", "## Notes"] + [f"- {n}" for n in notes]

    summary = (f"{len(wins)} won, {len(losses)} lost · {n_unpaid} unpaid · "
               f"{len(delivered)} delivered · {len(snipes)} snipes on the board\n"
               f"{tot['cards']} cards, ${tot['spent']:.2f} spent")
    return "\n".join(md) + "\n", summary


if __name__ == "__main__":
    # Print this week's report without writing, committing or sending anything.
    report, short = build(db.connect())
    print(report)
    print("--- phone summary ---\n" + short)
