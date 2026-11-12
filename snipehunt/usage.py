"""What Claude and eBay have cost, so the dashboard can show it and lanes can stop in time.

The Claude CLI reports a dollar figure for every call. With an API key that is
what you are billed; on a Claude subscription it is what the same work would
have cost through the API. One running total is kept per day. eBay calls are
counted per lane, against the app's daily allowance (see ebay_allowed).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from . import config, db

KEY = "claude_cost"
KEEP_DAYS = 62
JOB_ENV = "SNIPEHUNT_JOB"   # set by jobs.main to the name of the job that is running


SPOOL = "claude_cost_spool.jsonl"   # costs that couldn't be written yet
BUSY_MS = 2000   # the run that called Claude may itself hold the write lock: don't wait 30s


def _spool(entry: dict) -> None:
    with open(config.ensure_state_dir() / SPOOL, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _take_spool() -> list[dict]:
    """The spooled costs, claimed by renaming the file so no two runs add them twice."""
    path = config.ensure_state_dir() / SPOOL
    claimed = path.with_suffix(f".{os.getpid()}")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        return []
    out = [json.loads(line) for line in claimed.read_text().splitlines() if line.strip()]
    claimed.unlink()
    return out


LOG_KEY = "claude_log"   # the last LOG_KEEP calls, one line each: what each one cost and why
LOG_KEEP = 400
COUNTED = ("turns", "web", "tokens_in", "tokens_out")   # per job per day, for the self-review


def detail(outer: dict, secs: float, step: str = "") -> dict:
    """What one `claude -p --output-format json` reply says the call took: turns,
    web searches, tokens in and out (cached input counted apart), seconds, model."""
    tokens_in = tokens_out = cached = web = 0
    models = []
    for name, m in (outer.get("modelUsage") or {}).items():
        if not isinstance(m, dict):
            continue
        models.append(str(name))
        tokens_in += int(m.get("inputTokens") or 0) + int(m.get("cacheCreationInputTokens") or 0)
        cached += int(m.get("cacheReadInputTokens") or 0)
        tokens_out += int(m.get("outputTokens") or 0)
        web += int(m.get("webSearchRequests") or 0)
    return {"step": step, "turns": int(outer.get("num_turns") or 0), "web": web,
            "tokens_in": tokens_in + cached, "cached": cached, "tokens_out": tokens_out,
            "secs": round(secs), "model": ",".join(models)}


def describe(usd, d: dict) -> str:
    """One log line per call, so an expensive run explains itself."""
    k = lambda n: f"{n / 1000:.0f}k" if n >= 1000 else str(n)   # noqa: E731
    return (f"{d.get('step') or 'call'}: ${float(usd or 0):.2f}, {d.get('turns', 0)} turns, "
            f"{d.get('web', 0)} web, {k(d.get('tokens_in', 0))} in "
            f"({k(d.get('cached', 0))} cached), {k(d.get('tokens_out', 0))} out, "
            f"{d.get('secs', 0)}s")


def record(usd, detail: dict | None = None) -> None:
    """Add one call's cost to today's total, and with `detail` (see detail()) its
    turns, web searches and tokens. Never raises: this is bookkeeping.

    When the database is busy (often this same run, mid-write), the cost goes to
    a spool file instead and is added at the next call that gets through.
    """
    try:
        entry = {"usd": float(usd), "day": datetime.now(config.TZ).date().isoformat(),
                 "job": os.environ.get(JOB_ENV) or "other"}   # which job spent it
    except (TypeError, ValueError):
        return
    if detail:
        entry.update({k: detail[k] for k in (*COUNTED, "step", "secs", "model", "cached")
                      if detail.get(k) is not None})
        entry["at"] = datetime.now(config.TZ).isoformat(timespec="seconds")
    try:
        con = db.connect()
        con.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        con.execute("BEGIN IMMEDIATE")   # take the lock now, or fail fast
    except Exception as exc:
        _spool(entry)
        print(f"usage: database busy, cost spooled: {exc}", file=sys.stderr)
        return
    pending = []
    try:
        pending = _take_spool()
        days = json.loads(db.kv_get(con, KEY, "{}"))
        log = json.loads(db.kv_get(con, LOG_KEY, "[]"))
        for e in [*pending, entry]:
            day = days.setdefault(e["day"], {"usd": 0.0, "calls": 0})
            day["usd"] = round(day["usd"] + e["usd"], 4)
            day["calls"] += 1
            db.bump(con, f"cost:{e['job']}", e["usd"], e["day"])
            db.bump(con, f"claude_calls:{e['job']}", 1, e["day"])
            for k in COUNTED:
                db.bump(con, f"claude_{k}:{e['job']}", e.get(k) or 0, e["day"])
            if "at" in e:
                log.append({k: v for k, v in e.items() if k != "day"})
        db.kv_set(con, LOG_KEY, json.dumps(log[-LOG_KEEP:]))
        db.kv_set(con, KEY, json.dumps(dict(sorted(days.items())[-KEEP_DAYS:])))
        con.commit()
    except Exception as exc:
        con.rollback()
        for e in [*pending, entry]:
            _spool(e)
        print(f"usage: could not record a Claude cost, spooled: {exc}", file=sys.stderr)
    finally:
        con.close()


def summary(con, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    days = json.loads(db.kv_get(con, KEY, "{}"))
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    return {
        "cost_today": round(days.get(today, {}).get("usd", 0.0), 2),
        "calls_today": days.get(today, {}).get("calls", 0),
        "cost_month": round(sum(d["usd"] for day, d in days.items()
                                if day.startswith(month)), 2),
    }


# eBay calls. The Browse API allows an app EBAY_DAY_LIMIT calls a day (setting
# ebay_daily_cap), shared by every lane. Past WARN_AT of it, lanes stop in
# CUT_ORDER: each group stops at its own share of the cap, so the two main
# searches and the sold-price look-ups are the last to stop.
WARN_AT = 0.80
CUT_ORDER = (("art", 0.75), ("midtier", 0.80), ("deep", 0.80), ("extra", 0.80), ("priority", 0.85), ("hidden", 0.85), ("lowball", 0.85),
             ("bids", 0.90), ("typo", 0.90), ("keyword", 0.95))
LANE_KEY = "ebay_lane"      # stats key prefix: ebay_lane:<lane>, per day
_today: dict = {}            # what earlier runs used today: read once per process
_warned: list = []


def lane_of(label: str) -> str:
    """Which lane a search label belongs to, for counting and for the cut order."""
    label = label or "other"
    if label in ("auctions-ending", "newly-listed"):
        return "core"
    for prefix, lane in (("midtier-item", "midtier-item"), ("midtier", "midtier"), ("deep-", "deep"),
                         ("priority-", "priority"),
                         ("typo-", "typo"), ("extra-", "extra"), ("loose-", "extra"),
                         ("sellers", "extra"), ("no-name", "hidden"), ("hidden", "hidden"),
                         ("item", "item"), ("art", "art"), ("aspects", "aspects"), ("backfill", "other"),
                         ("other", "other")):
        if label.startswith(prefix):
            return lane
    return "keyword"   # "<name> auto", roster and "ending-" searches


def _earlier(con=None) -> dict:
    """Today's eBay calls by lane, and in all, from the runs that already finished."""
    day = datetime.now(config.TZ).date().isoformat()
    if _today.get("day") != day:
        try:
            totals = db.stat_totals(con or db.connect(), day, day)
        except Exception:   # no database yet
            totals = {}
        _today.clear()
        _today.update(day=day, total=sum(n for k, n in totals.items()
                                         if k.startswith("ebay_calls:")),
                      lanes={k.split(":", 1)[1]: n for k, n in totals.items()
                             if k.startswith(LANE_KEY + ":")})
    return _today


def ebay_used(lane: str | None = None, con=None) -> int:
    """eBay calls made today, in all or in one lane, this run included."""
    from . import ebay_search   # it imports this module; kept local
    earlier = _earlier(con)
    if lane is None:
        return int(earlier["total"] + ebay_search.API_CALLS)
    return int(earlier["lanes"].get(lane, 0) + ebay_search.LANE_CALLS[lane])


def ebay_allowed(lane: str, con=None) -> bool:
    """May this lane make another eBay call today? Logs once when the warning line is crossed."""
    from . import settings
    cap = settings.load(con)["ebay_daily_cap"]
    used = ebay_used(con=con)
    if used >= cap * WARN_AT and not _warned:
        _warned.append(used)
        print(f"usage: {used} of {cap} eBay calls used today; cutting lanes "
              "(midtier and extra first)", file=sys.stderr)
    share = dict(CUT_ORDER).get(lane, 1.0)
    return used < cap * share


def save_lanes(con, lanes) -> None:
    """Add this run's calls per lane to today's stats (ops.py does it as a job ends)."""
    for lane, n in lanes.items():
        db.bump(con, f"{LANE_KEY}:{lane}", n)
