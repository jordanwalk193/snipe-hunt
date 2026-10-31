"""The brag board: how cheaply you got your cards, worth showing off.

Built from the picks you bought (inventory.csv rows with an eBay item number,
matched to the bot's value for that pick; steals.bought). Totals, the biggest
steals, badges, a weekly streak, and a short brag card to paste on a forum.

The Hall of Fame (hall) ranks the whole collection, not just the bot's picks:
every card you own against what the same card last sold for in the ledger (or
the bot's value for a pick the ledger can't price), the ten cheapest shares of
value first. It is the one list that says what the whole program has been worth.

The brag card is safe to share: card names, prices and percentages only. No
seller, item number, order date, email or anything from your settings.
Nothing here asks Claude or sends anything.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import config, profile, steals

TOP = 5
HALL = 10                 # the Hall of Fame's size
HALL_MIN_VALUE = 5.0      # a steal on a card worth less than this is pocket change
HALL_MIN_OFF = 20         # percent under value a card needs to make the hall
STEAL_SHARE = 50          # a "steal" is paid at this percent of value or less
# badges: (key, name, what earns it)
BADGES = (
    ("ninety", "90% Club", "paid 10% of value or less"),
    ("dollar", "Dollar Store", "a $10+ card for a dollar or less"),
    ("half", "Half-Off Hero", f"paid {STEAL_SHARE}% of value or less"),
)


def _badges(r: dict) -> list[str]:
    out = []
    if r["pct"] <= 10:
        out.append("ninety")
    if r["paid"] <= 1.0 and r["value"] >= 10:
        out.append("dollar")
    if r["pct"] <= STEAL_SHARE:
        out.append("half")
    return out


def _day(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def streak(rows: list[dict], today: date) -> int:
    """Weeks in a row, back from this one, with at least one steal bought. A week that
    has only just started and has none yet doesn't break the streak."""
    weeks = {d.isocalendar()[:2] for d in (_day(r["date"]) for r in rows
                                           if r["pct"] <= STEAL_SHARE) if d}
    week = today
    if week.isocalendar()[:2] not in weeks:
        week -= timedelta(days=7)
    n = 0
    while week.isocalendar()[:2] in weeks:
        n += 1
        week -= timedelta(days=7)
    return n


def _slim(r: dict) -> dict:
    return {"card": r["card"], "paid": r["paid"], "value": r["value"], "saved": r["saved"],
            "pct": r["pct"], "off_pct": 100 - r["pct"], "image": r.get("image"),
            "badges": _badges(r)}


def board(con, now: datetime | None = None, inventory_rows: list[dict] | None = None,
          groups: list[dict] | None = None) -> dict:
    """Everything the dashboard's brag board shows. `inventory_rows` and `groups`
    (ledger.groups) save the hall a second read when the snapshot has them."""
    now = now or datetime.now(config.TZ)
    rows = steals.bought(con)
    month = now.strftime("%Y-%m")
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    in_month = [r for r in rows if r["date"].startswith(month)]
    in_week = [r for r in rows if r["date"] >= week_start]
    counts = {key: sum(1 for r in rows if key in _badges(r)) for key, _, _ in BADGES}
    top = sorted(rows, key=lambda r: (r["pct"], -r["saved"]))[:TOP]
    totals = steals.savings(con, now)
    return {
        "totals": {k: totals[k] for k in ("month", "all")},
        "top": [_slim(r) for r in top],
        "steal_of_month": _slim(min(in_month, key=lambda r: (r["pct"], -r["saved"])))
        if in_month else None,
        "this_week": {"n": len(in_week),
                      "saved": round(sum(r["saved"] for r in in_week), 2)},
        "badges": [{"key": k, "name": name, "rule": rule, "count": counts[k]}
                   for k, name, rule in BADGES],
        "streak_weeks": streak(rows, now.date()),
        "card": card(rows, totals, now),
        "hall": hall(con, inventory_rows, groups, now),
    }


def _app_name() -> str:
    try:
        return profile.get().app_name
    except (profile.ProfileError, OSError, ValueError):
        return "Snipe Hunt"


def hall(con, rows: list[dict] | None = None, groups: list[dict] | None = None,
         now: datetime | None = None) -> dict:
    """The ten best steals of all time, from the whole collection.

    Each owned card is priced by the ledger's newest sale of the same card (as
    ledger.worth does), or, for a card bought as a pick the ledger can't price,
    by the bot's value at the time. Ranked by the share of value paid, then the
    dollars saved; cards worth under HALL_MIN_VALUE, or less than HALL_MIN_OFF
    percent under their value, don't make it. Totals count only the cards that could be priced,
    so the percentage is honest."""
    from . import inventory, ledger, report   # report imports brag's neighbours
    now = now or datetime.now(config.TZ)
    if rows is None:
        try:
            rows = inventory.read()[1]
        except OSError:
            rows = []
    if groups is None:
        groups = ledger.groups(con)
    picks: dict[str, dict] = {}
    for item_id, data in con.execute("SELECT item_id, data FROM picks"):
        pick = json.loads(data)
        if pick.get("value"):
            picks[item_id] = pick
    priced: list[dict] = []
    unpriced = 0
    for r in rows:
        if (r.get("status") or "").strip().lower().startswith("sold"):
            continue
        try:
            paid = float(r.get("price_paid") or 0)
        except ValueError:
            continue
        if paid <= 0:
            continue
        name = report.card_name(r)
        g = ledger.find(groups, name) if name else None
        pick = picks.get((r.get("ebay_item_id") or "").strip())
        if g:
            value, by, image = float(g["sales"][0]["total"]), "sales", g["image"]
        elif pick:
            value, by, image = float(pick["value"]), "bot", pick.get("image")
        else:
            unpriced += 1
            continue
        if value <= 0:
            unpriced += 1
            continue
        priced.append({"card": name[:90], "paid": round(paid, 2), "value": round(value, 2),
                       "saved": round(value - paid, 2), "pct": round(100 * paid / value),
                       "image": image, "by": by, "bot": pick is not None,
                       "year": (r.get("year") or "").strip() or None})
    ranked = sorted((c for c in priced if c["value"] >= HALL_MIN_VALUE
                     and c["pct"] <= 100 - HALL_MIN_OFF),
                    key=lambda c: (c["pct"], -c["saved"]))
    top = [dict(c, off_pct=100 - c["pct"], badges=_badges(c)) for c in ranked[:HALL]]
    paid_all = round(sum(c["paid"] for c in priced), 2)
    worth_all = round(sum(c["value"] for c in priced), 2)
    bot = [c for c in priced if c["bot"]]
    out = {
        "top": top,
        "n": len(ranked),
        "totals": {"cards": len(priced), "unpriced": unpriced, "paid": paid_all,
                   "worth": worth_all, "saved": round(worth_all - paid_all, 2),
                   "pct": round(100 * paid_all / worth_all) if worth_all else None},
        "bot": {"n": len(bot), "paid": round(sum(c["paid"] for c in bot), 2),
                "worth": round(sum(c["value"] for c in bot), 2),
                "saved": round(sum(c["saved"] for c in bot), 2)},
    }
    out["card"] = hall_card(out, now)
    return out


def hall_card(h: dict, now: datetime) -> str:
    """The Hall of Fame as text to paste anywhere: card names, prices and
    percentages only. Empty until something ranks."""
    if not h["top"]:
        return ""
    t = h["totals"]
    lines = [f"🏛️ {_app_name()} Hall of Fame · {now:%B %Y}",
             f"{t['cards']} cards priced by real sales: paid ${t['paid']:,.2f}, "
             f"worth ${t['worth']:,.2f}" + (f" (paid {t['pct']}% of value)" if t["pct"] is not None else "")]
    if h["bot"]["n"]:
        lines.append(f"{h['bot']['n']} of them found by the bot, ${h['bot']['saved']:,.2f} under value")
    for n, c in enumerate(h["top"], 1):
        lines.append(f"{n}. {c['card']}: ${c['paid']:.2f} → worth ${c['value']:.2f} ({c['off_pct']}% off)")
    return "\n".join(lines)


def card(rows: list[dict], totals: dict, now: datetime) -> str:
    """The brag card: a few lines to paste anywhere. Empty when nothing is bought yet."""
    if not rows:
        return ""
    app = _app_name()
    t = totals["all"]
    best = min(rows, key=lambda r: (r["pct"], -r["saved"]))
    counts = {name: sum(1 for r in rows if key in _badges(r)) for key, name, _ in BADGES}
    lines = [f"🏆 {app} brag board, {now:%B %Y}",
             f"Saved ${t['saved']:,.2f} on {t['n']} card{'s' if t['n'] != 1 else ''} "
             f"(paid {t['pct']}% of value)" if t["pct"] is not None else
             f"{t['n']} cards bought",
             f"Best steal: {best['card']}: paid ${best['paid']:.2f}, worth "
             f"${best['value']:.2f} ({100 - best['pct']}% off)"]
    earned = [f"{name} ×{n}" for name, n in counts.items() if n]
    if earned:
        lines.append(" · ".join(earned))
    weeks = streak(rows, now.date())
    if weeks >= 2:
        lines.append(f"Steal streak: {weeks} weeks")
    return "\n".join(lines)


def report_lines(con, now: datetime | None = None) -> list[str]:
    """The weekly report's brag section; nothing when no picks were bought."""
    b = board(con, now)
    if not b["card"]:
        return []
    w = b["this_week"]
    head = (f"This week: {w['n']} bought, ${w['saved']:.2f} under value."
            if w["n"] else "No steals bought this week.")
    lines = ["## Brag board", "", head, "", "```", b["card"], "```"]
    top = b["hall"]["top"][:3]
    if top:
        lines += ["", "All-time Hall of Fame:"] + [
            f"{n}. {c['card']}: ${c['paid']:.2f} for a ${c['value']:.2f} card ({c['off_pct']}% off)"
            for n, c in enumerate(top, 1)]
    return lines
