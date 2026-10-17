"""Bounded self-tuning: one small, reversible settings change a week.

The weekly self-review (reflect.py) may ask for one change to one of the
tunables listed in settings.TUNABLES. This module decides whether that is
allowed, applies it as an ordinary setting, and two weeks later checks the
metric the change was judged by. A change that made things worse is put back.

Nothing here touches code, and nothing outside settings.TUNABLES can be
changed: not the read-only rules, quiet hours, which alerts are on,
credentials or how the bot signs in to Gixen. The setting `self_tuning`
freezes all of it.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from . import config, settings

DAY = 86400
MIN_GAP = 7 * DAY        # at most one change a week
WAIT = 14 * DAY          # how long a change runs before it is judged
GIVE_UP = 42 * DAY       # still too little to judge by after this: stop watching
BASELINE = 28 * DAY      # the stretch before a change its metric is measured over
MIN_SAMPLE = 8           # fewer results than this, before or after, proves nothing
NOISE_PCT = 15           # a metric has to move this much of itself to count as worse
NOISE_FLOOR = 3.0        # and at least this much outright (most metrics are percentages)
NOISE_FLOORS = {"cost_per_pick": 0.01}   # ...except the ones in dollars
WATCHING, KEPT, REVERTED = "watching", "kept", "reverted"

# measure(metric name, from, to) -> (value or None, how many results it rests on)
Measure = Callable[[str, float, float], tuple[float | None, int]]


class TuneError(ValueError):
    """Why a proposed change was not applied."""


def current(con) -> dict:
    """Every tunable with its value now, its default, its bounds and its metric."""
    cfg = settings.load(con)
    return {name: {"value": cfg[name], "default": settings.defaults()[name], **spec}
            for name, spec in settings.TUNABLES.items()}


def check(con, proposal, now: float) -> tuple[str, float]:
    """Return (name, value) for a change that is allowed, or say why it is not."""
    if not settings.load(con)["self_tuning"]:
        raise TuneError("self-tuning is switched off")
    if not isinstance(proposal, dict):
        raise TuneError("no change proposed")
    name, value = proposal.get("name"), proposal.get("value")
    if name not in settings.TUNABLES:
        raise TuneError(f"{str(name)[:40]!r} is not a tunable")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TuneError("the new value is not a number")
    spec, was = settings.TUNABLES[name], settings.load(con)[name]
    if not spec["min"] <= value <= spec["max"]:
        raise TuneError(f"{value} is outside {spec['min']} to {spec['max']}")
    if abs(value - was) > spec["step"] + 1e-9:
        raise TuneError(f"{was} to {value} is more than one step of {spec['step']}")
    if abs(value - was) < 1e-9:
        raise TuneError("that is the value it already has")
    last = con.execute("SELECT max(applied_at) FROM tuning_log").fetchone()[0] or 0
    if now - last < MIN_GAP:
        raise TuneError("one change a week: the last one was less than a week ago")
    clash = con.execute("SELECT name FROM tuning_log WHERE status=? AND metric=?",
                        (WATCHING, spec["metric"])).fetchone()
    if clash:   # two changes judged by one metric couldn't be told apart
        raise TuneError(f"still watching the change to {clash['name']}, judged by the same metric")
    return name, value


def apply(con, proposal, measure: Measure, now: float | None = None,
          dry_run: bool = False) -> dict:
    """Apply one proposed change if it is allowed. Returns what happened, for the report."""
    now = now or time.time()
    try:
        name, value = check(con, proposal, now)
    except TuneError as exc:
        return {"applied": False, "note": str(exc)}
    spec, was = settings.TUNABLES[name], settings.load(con)[name]
    baseline, sample = measure(spec["metric"], now - BASELINE, now)
    out = {"applied": not dry_run, "name": name, "old": was, "new": value,
           "metric": spec["metric"], "baseline": baseline, "sample": sample,
           "note": "dry run: not applied" if dry_run else "applied"}
    if dry_run:
        return out
    settings.set_value(con, name, value)
    con.execute(
        "INSERT INTO tuning_log (name, old, new, reason, metric, baseline, sample, applied_at,"
        " status) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, was, settings.load(con)[name], str(proposal.get("reason") or "")[:300],
         spec["metric"], baseline, sample, int(now), WATCHING))
    con.commit()
    return out


def worse(metric: str, baseline: float, result: float, better: str) -> bool:
    """Did the metric get worse by more than noise?"""
    drop = baseline - result if better == "higher" else result - baseline
    return drop > max(NOISE_FLOORS.get(metric, NOISE_FLOOR), abs(baseline) * NOISE_PCT / 100)


def review(con, measure: Measure, now: float | None = None, dry_run: bool = False
           ) -> list[dict]:
    """Judge every change that has run its two weeks; put back the ones that hurt.

    Too few results before or after to tell is "inconclusive, keep watching",
    up to six weeks. A setting changed by hand since is left alone.
    """
    now = now or time.time()
    if not settings.load(con)["self_tuning"]:
        return []   # frozen: nothing is changed, not even back
    out = []
    for row in con.execute("SELECT * FROM tuning_log WHERE status=? AND applied_at<=? "
                           "ORDER BY applied_at", (WATCHING, now - WAIT)).fetchall():
        spec = settings.TUNABLES.get(row["name"])
        if spec is None:   # no longer a tunable: nothing to judge or undo
            status, result, note = KEPT, None, "no longer tunable; left as it is"
        else:
            result, sample = measure(row["metric"], row["applied_at"], now)
            thin = (result is None or row["baseline"] is None or sample < MIN_SAMPLE
                    or (row["sample"] or 0) < MIN_SAMPLE)
            if thin and now - row["applied_at"] < GIVE_UP:
                status, note = WATCHING, "inconclusive, keep watching"
            elif thin:
                status, note = KEPT, "inconclusive after six weeks; kept"
            elif not worse(row["metric"], row["baseline"], result, spec["better"]):
                status, note = KEPT, "no worse than before; kept"
            elif abs(settings.load(con)[row["name"]] - row["new"]) > 1e-9:
                status, note = KEPT, "got worse, but the setting was changed by hand since"
            else:
                status, note = REVERTED, "got worse; put back"
                if not dry_run:
                    settings.set_value(con, row["name"], row["old"])
        if not dry_run:
            con.execute("UPDATE tuning_log SET status=?, checked_at=?, result=?, note=? "
                        "WHERE id=?", (status, int(now), result, note, row["id"]))
        out.append({"name": row["name"], "old": row["old"], "new": row["new"],
                    "metric": row["metric"], "baseline": row["baseline"], "result": result,
                    "status": status, "note": note})
    if not dry_run:
        con.commit()
    return out


def view(con, limit: int = 10) -> list[dict]:
    """The latest changes and how each turned out, for the dashboard and the report."""
    rows = con.execute("SELECT * FROM tuning_log ORDER BY applied_at DESC, id DESC LIMIT ?",
                       (limit,)).fetchall()
    return [{"name": r["name"], "old": r["old"], "new": r["new"], "reason": r["reason"],
             "metric": r["metric"], "baseline": r["baseline"], "result": r["result"],
             "status": r["status"], "note": r["note"],
             "applied_at": datetime.fromtimestamp(r["applied_at"], config.TZ).isoformat(
                 timespec="minutes")} for r in rows]


def _number(value) -> str:
    return "?" if value is None else f"{value:g}"


def lines(rows: list[dict]) -> list[str]:
    """The tuning log as Markdown bullets."""
    out = []
    for r in rows:
        judged = "" if r["result"] is None else f" → {_number(r['result'])}"
        out.append(f"- {r['applied_at'][:10]} · `{r['name']}` {_number(r['old'])} → "
                   f"{_number(r['new'])} · {r['status']}"
                   + (f" ({r['note']})" if r.get("note") else "")
                   + f" · judged by {r['metric']}: {_number(r['baseline'])}{judged}")
    return out
