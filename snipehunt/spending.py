"""What the collector has paid for cards, and what they are likely to pay next.

Read from inventory.csv (price_paid and order_date), so nothing here asks
Claude or eBay. It gives the dashboard and the weekly report a spending
history by month, the usual price per kind of card (autograph, graded,
numbered, rookie, base), this month's pace against the monthly budget, and a
forecast: next month's likely spend and the likely price of the next card.
The forecast is a plain weighted average of recent months, said with its range,
not a promise.
"""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date, datetime

from . import config, inventory, rarity, settings

MONTHS_SHOWN = 12
FORECAST_MONTHS = 3       # recent full months the forecast is weighted over, newest heaviest
MIN_MONTHS = 2            # fewer full months than this and there is no forecast yet
RECENT_CARDS = 20         # the latest purchases the "next card" estimate is taken from
KIND_MIN = 3              # purchases of a kind needed before its usual price is shown
KINDS = ("auto", "graded", "numbered", "rookie", "base")


def _money(value) -> float | None:
    try:
        found = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
        return float(found.group(0)) if found else None
    except ValueError:
        return None


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def kind(row: dict) -> str:
    """The first of auto, graded, numbered, rookie or base that fits an inventory row."""
    if (row.get("auto") or "").strip():
        return "auto"
    if (row.get("graded") or "").strip():
        return "graded"
    if re.search(r"/\s?\d+", row.get("serial") or ""):
        return "numbered"
    if (row.get("rc") or "").strip():
        return "rookie"
    return "base"


def kind_of_title(title: str) -> str:
    """The same five kinds, read from a listing title."""
    if rarity.AUTO.search(title):
        return "auto"
    if rarity.GRADED.search(title):
        return "graded"
    if rarity.print_run(title) is not None:
        return "numbered"
    if re.search(r"\b(rc|rookie)\b", title, re.I):
        return "rookie"
    return "base"


def purchases(rows: list[dict]) -> list[dict]:
    """Every card with a price and an order date, oldest first."""
    out = []
    for r in rows:
        paid, day = _money(r.get("price_paid")), _day(r.get("order_date"))
        if paid is None or day is None:
            continue
        out.append({"paid": paid, "day": day, "kind": kind(r),
                    "player": (r.get("player") or "").strip() or None})
    return sorted(out, key=lambda p: p["day"])


def _month(d: date) -> str:
    return d.strftime("%Y-%m")


def _months_back(today: date, n: int) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _spread(values: list[float]) -> dict:
    values = sorted(values)
    if len(values) >= 4:
        q = statistics.quantiles(values, n=4)
        low, high = q[0], q[2]
    else:
        low, high = values[0], values[-1]
    return {"usual": round(statistics.median(values), 2), "low": round(low, 2),
            "high": round(high, 2), "n": len(values)}


def summary(rows: list[dict] | None = None, today: date | None = None,
            budget: float | None = None) -> dict:
    """The spending picture the dashboard and the report show."""
    today = today or datetime.now(config.TZ).date()
    rows = rows if rows is not None else inventory.read()[1]
    if budget is None:
        budget = settings.load()["monthly_budget"]
    bought = purchases(rows)
    if not bought:
        return {"purchases": 0}
    keys = _months_back(today, MONTHS_SHOWN)
    per = {k: {"month": k, "spent": 0.0, "cards": 0} for k in keys}
    for p in bought:
        if _month(p["day"]) in per:
            per[_month(p["day"])]["spent"] += p["paid"]
            per[_month(p["day"])]["cards"] += 1
    months = [dict(m, spent=round(m["spent"], 2)) for m in per.values()]
    this = per[_month(today)]
    days = calendar.monthrange(today.year, today.month)[1]
    pace = round(this["spent"] / today.day * days, 2) if today.day else this["spent"]
    # Full months since the first purchase; the current month isn't over yet.
    first = _month(bought[0]["day"])
    full = [m for m in months[:-1] if m["month"] >= first]
    forecast = None
    if len(full) >= MIN_MONTHS:
        recent = full[-FORECAST_MONTHS:]
        weights = range(1, len(recent) + 1)
        likely = sum(w * m["spent"] for w, m in zip(weights, recent)) / sum(weights)
        spread = _spread([m["spent"] for m in full[-6:]])
        forecast = {"next_month": round(likely, 2), "low": spread["low"],
                    "high": spread["high"], "months_used": len(recent),
                    "cards_next_month": round(sum(w * m["cards"] for w, m in
                                                  zip(weights, recent)) / sum(weights))}
    latest = [p["paid"] for p in bought[-RECENT_CARDS:]]
    by_kind = {}
    for k in KINDS:
        paid = [p["paid"] for p in bought if p["kind"] == k]
        if len(paid) >= KIND_MIN:
            by_kind[k] = _spread(paid)
    by_player = {}
    for p in bought:
        if p["player"]:
            by_player.setdefault(p["player"], []).append(p["paid"])
    players = sorted(({"player": name, **_spread(v), "spent": round(sum(v), 2)}
                      for name, v in by_player.items()), key=lambda r: -r["spent"])[:15]
    out = {
        "purchases": len(bought), "spent": round(sum(p["paid"] for p in bought), 2),
        "months": months,
        "this_month": {"spent": round(this["spent"], 2), "cards": this["cards"],
                       "pace": pace},
        "forecast": forecast,
        "next_card": _spread(latest),       # the likely price of the next card bought
        "by_kind": by_kind,
        "by_player": players,
        "budget": budget or None,
    }
    if budget:
        out["budget_left"] = round(budget - this["spent"], 2)
        out["over_pace"] = pace > budget
    return out


def usual_for(spend: dict, title: str) -> dict | None:
    """What the collector usually pays for the kind of card in this title."""
    return (spend.get("by_kind") or {}).get(kind_of_title(title))


def lines(spend: dict) -> list[str]:
    """The weekly report's Spending section."""
    if not spend.get("purchases"):
        return ["- No purchases with a price and an order date in inventory.csv yet."]
    this, out = spend["this_month"], []
    line = (f"- This month: **${this['spent']:.2f}** on {this['cards']} cards, "
            f"on pace for ${this['pace']:.2f}")
    if spend.get("budget"):
        line += (f" against a ${spend['budget']:.2f} budget"
                 + (" (over pace)" if spend.get("over_pace") else ""))
    out.append(line)
    f = spend.get("forecast")
    if f:
        out.append(f"- Next month, likely **${f['next_month']:.2f}** on about "
                   f"{f['cards_next_month']} cards (recent months ran ${f['low']:.2f} to "
                   f"${f['high']:.2f})")
    else:
        out.append(f"- No forecast yet: it needs {MIN_MONTHS} full months of purchases.")
    n = spend["next_card"]
    out.append(f"- Your next card will likely cost about **${n['usual']:.2f}** "
               f"(most of your last {n['n']} were ${n['low']:.2f} to ${n['high']:.2f})")
    for k, s in spend["by_kind"].items():
        out.append(f"  - {k}: usually ${s['usual']:.2f} ({s['n']} bought, "
                   f"${s['low']:.2f} to ${s['high']:.2f})")
    return out
