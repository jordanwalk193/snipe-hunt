"""The brag board: how cheaply you got your cards, worth showing off.

Built from the picks you bought (inventory.csv rows with an eBay item number,
matched to the bot's value for that pick; steals.bought). Totals, the biggest
steals, badges, a weekly streak, and a short brag card to paste on a forum.

The brag card is safe to share: card names, prices and percentages only. No
seller, item number, order date, email or anything from your settings.
Nothing here asks Claude or sends anything.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import config, profile, steals

TOP = 5
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


def board(con, now: datetime | None = None) -> dict:
    """Everything the dashboard's brag board shows."""
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
    }


def card(rows: list[dict], totals: dict, now: datetime) -> str:
    """The brag card: a few lines to paste anywhere. Empty when nothing is bought yet."""
    if not rows:
        return ""
    try:
        app = profile.get().app_name
    except (profile.ProfileError, OSError, ValueError):
        app = "Snipe Hunt"
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
    return ["## Brag board", "", head, "", "```", b["card"], "```"]
