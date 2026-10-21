"""Calibration: the bot corrects its own guessed going rates from how they turned out.

Telling Claude "your values have been running 22% low" in the prompt (see
sales.track_record) barely moved its guesses. So the bot now applies the
correction itself, after Claude answers: a guessed value ("estimate" basis) is
multiplied by a factor learned from the guesses in the same price band whose
auctions have since sold. Values backed by real sales are left alone.

The factor is always measured against Claude's raw guess (the pick's
"value_raw"), never against an already corrected value, so it settles where
the raw guesses really are instead of chasing its own tail. It is shrunk
toward 1 while a band has few results and held inside FACTOR_RANGE, so one
odd sale can't swing it.
"""
from __future__ import annotations

import statistics

from . import sales, settings

MIN_CALLS = 8                 # fewer guesses than this in a band and it stays at 1
LATEST = 30                   # the newest guesses per band it learns from
SHRINK = 10                   # weight = n / (n + SHRINK): 8 results count ~44%, 30 count 75%
FACTOR_RANGE = (0.75, 1.40)


def factors(con, rows: list[dict] | None = None) -> dict[str, dict]:
    """Per price band: {"factor", "calls", "bias_pct"} learned from guessed values.

    Only bands that have enough results and are off by at least `track_off`
    percent get a factor other than 1. Empty when `calibrate` is off.
    """
    cfg = settings.load(con)
    if not cfg.get("calibrate", True):
        return {}
    rows = sales.with_calls(con) if rows is None else rows   # newest first
    out = {}
    for _, _, name in sales.BANDS:
        off = [r["off_raw"] for r in rows
               if r["call"] and r.get("off_raw") is not None
               and r["call"].get("basis") == "estimate"
               and sales.band(r["call"]["value_raw"]) == name][:LATEST]
        if len(off) < MIN_CALLS:
            continue
        bias = statistics.median(off)
        if abs(bias) * 100 < cfg["track_off"]:
            continue
        weight = len(off) / (len(off) + SHRINK)
        factor = min(max(1 + bias * weight, FACTOR_RANGE[0]), FACTOR_RANGE[1])
        out[name] = {"factor": round(factor, 3), "calls": len(off),
                     "bias_pct": round(100 * bias)}
    return out


def adjust(value: float | None, basis: str, learned: dict[str, dict]) -> float:
    """The factor for one guessed value (1.0 when nothing applies)."""
    if value is None or basis == "sales" or not learned:
        return 1.0
    return learned.get(sales.band(value), {}).get("factor", 1.0)


def summary(learned: dict[str, dict]) -> str:
    """One line for reports: what is being corrected, and by how much."""
    if not learned:
        return "no corrections (not enough results yet, or guesses on target)"
    return "; ".join(f"{band}: x{f['factor']:.2f} (guesses ran {abs(f['bias_pct'])}% "
                     f"{'low' if f['bias_pct'] > 0 else 'high'}, {f['calls']} sold)"
                     for band, f in learned.items())
