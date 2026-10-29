"""The weekly self-review: what is the bot getting wrong, and what would fix it?

Once a week the bot adds up its own numbers (how its picks sold, the steals
it missed, what was hidden and what was sniped, alerts, cost, failures) and
asks Claude to review them: three to five proposed fixes, each with its
evidence and a way to measure it. The review is written to
reports/reflect-YYYY-MM-DD.md and, when GITHUB_TOKEN is set, to one GitHub
issue for the week.

Two things keep this safe:

- The numbers are numbers. Listing titles, seller names and email text are
  written by strangers, so none of them is ever put in the bundle or the
  prompt. Cards are grouped by type (auto, graded, numbered, insert, base),
  worked out here and never quoted.
- The review can change nothing but one tunable setting, through tune.py and
  inside its bounds. It has no tools, and this module never touches code:
  code changes arrive as pull requests a person merges (docs/SELF_IMPROVE.md).
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

import requests

from . import (calibrate, closes, config, db, gitsync, inventory, notify, profile, rarity, sales,
               score, tune, usage)

WEEK = 7 * 86400
LOOK_BACK = 4            # earlier reviews whose proposals are followed up
MAX_PROPOSALS = 5
TEXT = 300               # the longest any one field of a proposal may be
GITHUB_API = "https://api.github.com"
LABEL = "self-review"
JOBS = ("hourly", "daily", "weekly", "fast", "reflect", "other")
TIERS = ("fire", "green", "yellow")
CARD_TYPES = ("auto", "graded", "numbered", "insert", "base")
INSERT = re.compile(r"\b(insert|variation|ssp|sp|relic|patch|die[- ]?cut)\b", re.I)
# The metrics a proposal or a tunable can be judged by, and which way is good.
METRICS = {
    "under_max_rate": "higher",   # % of picked auctions that closed at or under the max
    "value_gap_pct": "lower",     # how far the going rate was from the real price, typically
    "missed_steals": "lower",     # cheap sales the bot never picked
    "skipped_pct": "lower",       # % of picks the collector hid with the Skip button
    "sniped_pct": "higher",       # % of auction picks that got a snipe
    "cost_per_pick": "lower",     # Claude dollars per pick
    "failures": "lower",          # failed job runs
    "alerts_stale": "lower",      # alerts dropped because the price had moved on
}
PROMPT = """You are reviewing {app}, a bot that searches eBay for underpriced \
sports cards of one player and recommends what to snipe and for how much. It \
only reads and recommends: it never bids, buys or changes a snipe. Below are \
its own numbers for the week ending {week} next to the four weeks before, the \
settings it is allowed to tune, and what earlier reviews proposed.

What is the bot getting wrong, what is the evidence in these numbers, and what \
are the 3 to 5 highest-value fixes? For each: the problem, the evidence, the \
proposed change (a setting tweak or a code change), the expected effect, how \
we would measure it, and the risk.

How to read the numbers:
- A "call" is a pick whose auction then sold; under_max_rate is the share of \
calls that closed at or under the max the bot gave, and value_gap_pct is how \
far its going rate was from the real delivered price, typically.
- value_bias_pct is (real delivered price - going rate) / going rate, as a \
median. ABOVE zero means cards sold for MORE than the bot's going rate (its \
values were too low); BELOW zero means they sold for LESS (its values were \
too high). Read the sign carefully before building on it.
- "calibration" shows the correction the bot already applies by itself to \
guessed (not sales-backed) going rates in each price band, learned from \
Claude's raw guesses. Don't propose a fix it is already making; say whether \
the bias that is left looks like what it can't correct. The scorecard's \
"by_source" splits the calls by where the going rate came from: "sales" \
(backed by real sold prices), "guessed" (Claude's estimate, not corrected) \
and "calibrated" (an estimate the calibration then corrected).
- "closes" is the closing price as a share of the bot's going rate (1.0 = at \
it) by seller type (small sellers against consignors with 10+ sales on record) \
and, at small sellers, by ending hour (daytime 8am-8pm against evening). \
Lowball snipes already skip hot sellers; picks and alerts already say which \
kind of seller and ending they are.
- "snipes" breaks the collector's snipes on picks down by tier and price \
band: sniped_pct, how far their max sat from the bot's, and won/lost (lost = \
sold to someone else; pending = still running; unknown = over, no result yet). \
"outcomes" adds them all up, "unattributed" are snipes not on any pick. \
won_paid_vs_value_pct is what won snipes cost against the bot's going rate, \
as a median; below zero is a steal.
- skipped_pct is the share of picks the collector hid with the Skip button, \
and sniped_pct the share of auction picks the collector set a snipe on. \
by_edge counts the picks the bot itself labelled as a price mistake \
("mispriced"), a listing buyers won't find ("hidden") or a sleeper auction; \
most picks are meant to be "none".
- Alerts, cost, runs and eBay calls have only been counted since \
{counting_since}. Before that date their zeros mean "not counted yet".

Rules:
- Argue only from the numbers below. Where a number rests on too few results \
to mean anything, say so and do not build a proposal on it.
- Never propose weakening the read-only rules, quiet hours, which alerts are \
switched on, credentials or how the bot signs in to Gixen.
- Do not repeat an earlier proposal that is marked done. If its metric did not \
move, say what that tells you.
- "metric" must be one of: {metrics}.
- In "tune" you may propose at most ONE change to one tunable: inside its min \
and max, no more than its step away from its value now. Use null unless the \
numbers clearly support a change. It is applied automatically and put back if \
its metric gets worse.

Reply with ONLY one JSON object, no prose, no code fence:
{{"summary": "two sentences on how the week went", "proposals": [{{"problem": \
"…", "evidence": "the numbers that show it", "kind": "setting|code", "change": \
"…", "expected": "…", "measure": "…", "metric": "one of the names above", \
"risk": "…"}}], "tune": {{"name": "…", "value": 0, "reason": "…"}}}}

=== metrics (JSON) ===
{bundle}

=== earlier proposals (JSON) ===
{earlier}
"""


def card_type(title: str) -> str:
    """Which broad kind of card a title describes. The title itself is never kept."""
    grade, auto, run = sales.traits(title)
    if auto:
        return "auto"
    if grade:
        return "graded"
    if run or rarity.print_run(title) is not None:
        return "numbered"
    return "insert" if INSERT.search(title) else "base"


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TZ).date().isoformat()


def window(con, start: float, end: float) -> dict:
    """The bot's numbers for one stretch of time. Numbers and fixed labels only."""
    sold = [r for r in sales.with_calls(con, start) if r["sold_at"] < end]
    card = sales.scorecard(sold)
    missed = [m for m in sales.find_missed(con, end, days=(end - start) / 86400)
              if m["sold_at"] < end]
    by_type = {}
    for kind in CARD_TYPES:
        pcts = [m["pct"] for m in missed if card_type(m["title"]) == kind]
        if pcts:
            by_type[kind] = {"count": len(pcts), "median_pct_under": statistics.median(pcts)}
    picks = [json.loads(r["data"]) for r in con.execute(
        "SELECT data FROM picks WHERE created_at>=? AND created_at<?", (start, end))]
    ids = {p["item_id"] for p in picks}
    auctions = {p["item_id"] for p in picks if p.get("is_auction")}
    hidden = ids & {p["item_id"] for p in db.passed(con)}
    snipes = [dict(r) for r in con.execute("SELECT * FROM snipe_log")]
    sniped = auctions & {s["item_id"] for s in snipes}
    gaps = [s["my_max"] - s["bot_max"] for s in snipes
            if start <= s["logged_at"] < end and s["bot_max"] is not None
            and s["my_max"] is not None]
    said = [f for f in db.feedback(con, since=int(start)) if f["created_at"] < end]
    stats = stat_totals(con, start, end)
    per_job = lambda prefix: {job: round(stats[f"{prefix}:{job}"], 2)   # noqa: E731
                              for job in JOBS if stats.get(f"{prefix}:{job}")}
    cost, runs, calls = per_job("cost"), per_job("runs"), per_job("ebay_calls")
    errors: Counter = Counter()
    for key, n in stats.items():
        if key.startswith("error:"):   # an exception's class name, never its message
            name = key.rsplit(":", 1)[1]
            errors[name if re.fullmatch(r"[A-Za-z_]{1,40}", name) else "Other"] += int(n)
    return {
        "sales_recorded": len(sold),
        "scorecard": {**card, "under_max_rate": _pct(card["under_max"], card["calls"])},
        # how auctions close by seller type and ending hour (closes.py): numbers only
        "closes": {k: v for k, v in closes.learn(con).items() if k in ("calls", "trusted", "groups", "hours")},
        "missed_steals": {
            "count": len(missed),
            "median_pct_under": statistics.median(m["pct"] for m in missed) if missed else None,
            "by_card_type": by_type},
        "picks": {
            "count": len(picks), "auctions": len(auctions),
            "by_tier": {t: sum(p.get("tier") == t for p in picks) for t in TIERS},
            "by_edge": {e: sum((p.get("edge") or "none") == e for p in picks)
                        for e in (*sorted(score.EDGES), "none")},
            "skipped_pct": _pct(len(hidden), len(picks)),
            "sniped_pct": _pct(len(sniped), len(auctions)),
            "snipes_on_picks": len(gaps),
            "avg_my_max_minus_bot_max": round(statistics.mean(gaps), 2) if gaps else None},
        "snipes": snipe_results(con, picks, start, end),
        "feedback": feedback_counts(said),
        "alerts": {
            "sent": int(stats.get("alerts_sent", 0)), "held": int(stats.get("alerts_held", 0)),
            "dropped_as_stale": int(stats.get("alerts_stale", 0)),
            "bedtime_bundles": int(stats.get("bedtime_bundles", 0)),
            "avg_bedtime_bundle_size": round(
                stats["bedtime_picks"] / stats["bedtime_bundles"], 1)
            if stats.get("bedtime_bundles") else None},
        "cost": {
            "usd_by_job": cost, "usd_total": round(sum(cost.values()), 2),
            "usd_per_pick": round(sum(cost.values()) / len(picks), 3) if picks else None},
        "ebay_calls_per_run": {job: round(calls[job] / runs[job], 1)
                               for job in calls if runs.get(job)},
        # every eBay call by lane (usage.lane_of), so a lane that costs more than it finds shows
        "ebay_calls_by_lane": {key.split(":", 1)[1]: int(n) for key, n in sorted(stats.items())
                               if key.startswith(usage.LANE_KEY + ":")},
        "reliability": {
            "runs": {job: int(n) for job, n in runs.items()},
            "failures": {job: int(n) for job, n in per_job("failures").items()},
            "error_types": dict(errors),
            "gixen_sign_outs": int(stats.get("gixen_signed_out", 0))},
    }


def stat_totals(con, start: float, end: float) -> dict[str, float]:
    """The daily counts added up over exactly this stretch of time.

    Counts are kept per local day, and a week that starts and ends on a Sunday
    evening touches eight days. Adding all eight whole showed eight days of
    runs and cost against seven of picks, so the days at either end count only
    for the share of them that falls inside the stretch.
    """
    first, last = _day(start), _day(end - 1)
    out: dict[str, float] = {}
    for row in con.execute("SELECT day, key, n FROM stats WHERE day>=? AND day<=?",
                           (first, last)):
        day = datetime.fromisoformat(row["day"]).replace(tzinfo=config.TZ)
        day_end = day + timedelta(days=1)
        inside = (min(end, day_end.timestamp()) - max(start, day.timestamp())) / (
            day_end.timestamp() - day.timestamp())
        out[row["key"]] = out.get(row["key"], 0) + row["n"] * max(0.0, min(1.0, inside))
    return out


OUTCOMES = ("won", "lost", "pending", "unknown")


def snipe_outcome(item_id: str, my_max: float | None, owned: set[str],
                  sold: dict[str, float], ends: dict[str, float | None], now: float) -> str:
    """How one snipe turned out.

    won: in the inventory under that item id, or the auction sold at or under
    the snipe's max (Gixen bids at the very end, so the highest max wins; the
    inventory just hasn't caught up yet). lost: sold for more than the max.
    pending: still running. unknown: over, but the result isn't in yet.
    """
    if item_id in owned:
        return "won"
    if item_id in sold:
        return "won" if my_max is not None and sold[item_id] <= my_max else "lost"
    end = ends.get(item_id)
    return "pending" if end is None or end > now else "unknown"


def snipe_results(con, picks: list[dict], start: float, end: float,
                  now: float | None = None) -> dict:
    """The collector's snipes logged in this stretch, by tier and price band: how
    many auction picks got one, how far their max sat from the bot's, and how they
    ended (see snipe_outcome).

    A snipe belongs to its pick even when the pick was made the week before, so
    the groups add up to every snipe on a pick. sniped_pct only counts snipes on
    this stretch's own picks, so it stays a share of them."""
    now = time.time() if now is None else now
    by_id = {p["item_id"]: p for p in picks if p.get("is_auction")}
    snipes = [dict(r) for r in con.execute(
        "SELECT * FROM snipe_log WHERE logged_at>=? AND logged_at<?", (start, end))]
    earlier = {}
    for s in snipes:
        if s["item_id"] not in by_id:
            row = con.execute("SELECT data FROM picks WHERE item_id=?",
                              (s["item_id"],)).fetchone()
            if row:
                earlier[s["item_id"]] = json.loads(row["data"])
    owned_rows = {r.get("ebay_item_id"): r for r in inventory.read()[1]
                  if r.get("ebay_item_id")}
    sold = {r["item_id"]: r["price"] for r in con.execute("SELECT item_id, price FROM sales")}
    ends = {r["item_id"]: r["end_ts"] for r in con.execute(
        "SELECT item_id, end_ts FROM seen WHERE item_id IN (%s)" % ",".join("?" * len(snipes)),
        [s["item_id"] for s in snipes])} if snipes else {}
    outcome = {s["item_id"]: snipe_outcome(s["item_id"], s["my_max"], set(owned_rows),
                                           sold, ends, now) for s in snipes}

    def group(key) -> dict:
        out: dict = {}

        def slot(p):
            return out.setdefault(key(p), {"auctions": 0, "sniped": 0, "_own": 0,
                                           **dict.fromkeys(OUTCOMES, 0), "_gaps": []})
        for p in by_id.values():
            slot(p)["auctions"] += 1
        for s in snipes:
            p = by_id.get(s["item_id"]) or earlier.get(s["item_id"])
            if p is None:
                continue
            g = slot(p)
            g["sniped"] += 1
            g["_own"] += s["item_id"] in by_id
            if s["my_max"] is not None and s["bot_max"] is not None:
                g["_gaps"].append(s["my_max"] - s["bot_max"])
            g[outcome[s["item_id"]]] += 1
        for g in out.values():
            gaps = g.pop("_gaps")
            g["sniped_pct"] = _pct(g.pop("_own"), g["auctions"])
            g["avg_my_max_minus_bot_max"] = round(statistics.mean(gaps), 2) if gaps else None
        return out

    paid = []   # what a won snipe cost against the going rate the bot gave
    for s in snipes:
        row, value = owned_rows.get(s["item_id"]), s["bot_value"]
        try:
            price = float(row["price_paid"]) if row else None
        except (TypeError, ValueError):
            price = None
        if price and value:
            paid.append((price - value) / value)
    on_picks = [s for s in snipes if s["item_id"] in by_id or s["item_id"] in earlier]
    return {
        "logged": len(snipes),
        "on_picks": len(on_picks),
        # snipes whose pick is gone, or that were never on a pick
        "unattributed": len(snipes) - len(on_picks),
        "outcomes": {o: sum(outcome[s["item_id"]] == o for s in on_picks) for o in OUTCOMES},
        # wins the inventory doesn't show yet, read off the sold price instead
        "won_not_in_inventory": sum(outcome[s["item_id"]] == "won"
                                    and s["item_id"] not in owned_rows for s in on_picks),
        "by_tier": group(lambda p: p.get("tier") or "none"),
        "by_band": group(lambda p: sales.band(p["value"]) if p.get("value") else "no value"),
        "won_paid_vs_value_pct": round(100 * statistics.median(paid)) if paid else None,
    }


def feedback_counts(rows: list[dict]) -> dict:
    """What was said about the picks, as counts by reason and tier. The reasons
    and tiers are the bot's own fixed labels; titles and notes are left out."""
    def reason(r):
        return r["reason"] if r["reason"] in db.REASONS else "other"

    def tier(r):
        return r["tier"] if r["tier"] in TIERS else "unknown"

    yours = [r for r in rows if r["source"] == "you"]
    by_both: dict = {}
    for r in yours:
        by_both.setdefault(reason(r), Counter())[tier(r)] += 1
    return {
        "given": len(yours),
        "by_reason": dict(Counter(reason(r) for r in yours)),
        "by_reason_and_tier": {k: dict(v) for k, v in by_both.items()},
        "sniped_picks": sum(r["source"] == "gixen" for r in rows),
        "on_estimates": sum(r["reason"] != "good_pick" and r["max_basis"] != "sales"
                            for r in yours),
    }


def measure(con, name: str, start: float, end: float) -> tuple[float | None, int]:
    """One metric over a stretch of time, and how many results it rests on."""
    return _pick_metric(window(con, start, end), name)


def _pick_metric(w: dict, name: str) -> tuple[float | None, int]:
    card, picks, alerts = w["scorecard"], w["picks"], w["alerts"]
    return {
        "under_max_rate": (card["under_max_rate"], card["calls"]),
        "value_gap_pct": (card["value_gap_pct"], card["valued"]),
        "missed_steals": (w["missed_steals"]["count"], w["sales_recorded"]),
        "skipped_pct": (picks["skipped_pct"], picks["count"]),
        "sniped_pct": (picks["sniped_pct"], picks["auctions"]),
        "cost_per_pick": (w["cost"]["usd_per_pick"], picks["count"]),
        "failures": (sum(w["reliability"]["failures"].values()),
                     sum(w["reliability"]["runs"].values())),
        "alerts_stale": (alerts["dropped_as_stale"],
                         alerts["sent"] + alerts["dropped_as_stale"]),
    }[name]


def bundle(con, now: datetime) -> dict:
    """Everything the review is shown: this week, the four before, and the tunables."""
    end = now.timestamp()
    return {
        "week_ending": now.date().isoformat(),
        "this_week": window(con, end - WEEK, end),
        "previous_4_weeks": window(con, end - 5 * WEEK, end - WEEK),
        "tunables": tune.current(con),
        "calibration": calibrate.factors(con),
        "tuning_log": [{k: row[k] for k in ("name", "old", "new", "metric", "baseline",
                                             "result", "status")}
                       for row in tune.view(con, 8)],
    }


def _text(value) -> str:
    return " ".join(str(value or "").split())[:TEXT]


def judge_for(con) -> tune.Measure:
    """measure() for one run, remembering each stretch of time it has already added up."""
    seen: dict[tuple, dict] = {}

    def judge(name: str, start: float, end: float) -> tuple[float | None, int]:
        if (start, end) not in seen:
            seen[(start, end)] = window(con, start, end)
        return _pick_metric(seen[(start, end)], name)
    return judge


def clean(reply: dict, week: str, judge: tune.Measure, now: datetime) -> list[dict]:
    """Claude's proposals in the shape the bot keeps: short, numbered, with a baseline."""
    out = []
    for raw in (reply.get("proposals") or [])[:MAX_PROPOSALS]:
        if not isinstance(raw, dict) or not raw.get("problem"):
            continue
        metric = raw.get("metric") if raw.get("metric") in METRICS else None
        baseline = (judge(metric, now.timestamp() - WEEK, now.timestamp())[0]
                    if metric else None)
        out.append({
            "id": f"SR-{week}-{len(out) + 1}",
            "kind": "code" if raw.get("kind") == "code" else "setting",
            **{k: _text(raw.get(k)) for k in ("problem", "evidence", "change", "expected",
                                              "measure", "risk")},
            "metric": metric, "baseline": baseline})
    return out


def earlier(con, week: str) -> list[dict]:
    """The reviews before this week's, newest first."""
    return [json.loads(r["data"]) for r in con.execute(
        "SELECT data FROM reflections WHERE week<? ORDER BY week DESC LIMIT ?",
        (week, LOOK_BACK))]


def follow_up(con, now: datetime, done: set[str], judge: tune.Measure) -> list[dict]:
    """Earlier proposals: was each one done, and did the metric it aimed at move?"""
    out = []
    for review in earlier(con, now.date().isoformat()):
        for p in review.get("proposals") or []:
            value = moved = None
            if p.get("metric") in METRICS:
                value = judge(p["metric"], now.timestamp() - WEEK, now.timestamp())[0]
            if value is not None and p.get("baseline") is not None:
                gain = value - p["baseline"]
                if METRICS[p["metric"]] == "lower":
                    gain = -gain
                moved = "better" if gain > 0 else "worse" if gain < 0 else "the same"
            out.append({"id": p["id"], "kind": p["kind"], "change": p["change"],
                        "done": p["id"] in done, "metric": p.get("metric"),
                        "then": p.get("baseline"), "now": value, "moved": moved})
    return out


# --- GitHub: issues only -----------------------------------------------------

def _repo() -> str | None:
    return config.get("GITHUB_REPO") or None


def _github(method: str, path: str, **kwargs) -> requests.Response:
    """One GitHub API call. The token, when there is one, needs Issues access only."""
    headers = {"Accept": "application/vnd.github+json"}
    if config.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {config.get('GITHUB_TOKEN')}"
    resp = requests.request(method, f"{GITHUB_API}/repos/{_repo()}{path}", headers=headers,
                            timeout=30, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path.split('?')[0]}: {resp.status_code}")
    return resp


def done_ids(now: datetime) -> set[str]:
    """Proposal ids that were carried out: ticked on their issue, or named in a commit.

    A pull request that implements a proposal names its id (SR-2026-10-18-2)
    in its title, so the merge commit does too. Reading a public repository
    needs no token; any failure just means nothing is marked done this week.
    """
    if not _repo():
        return set()
    found: set[str] = set()
    since = (now - timedelta(weeks=LOOK_BACK + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        for commit in _github("GET", f"/commits?since={since}&per_page=100").json():
            found.update(re.findall(r"SR-\d{4}-\d\d-\d\d-\d", commit["commit"]["message"]))
        for issue in _github("GET", f"/issues?labels={LABEL}&state=all&per_page=20").json():
            found.update(re.findall(r"- \[[xX]\] \*\*(SR-\d{4}-\d\d-\d\d-\d)\*\*",
                                    issue.get("body") or ""))
    except (requests.RequestException, RuntimeError, KeyError, ValueError, TypeError) as exc:
        print(f"reflect: could not read GitHub: {exc}", file=sys.stderr)
    return found


def issue_title(week: str) -> str:
    return f"Weekly self-review {week}"


def issue_body(review: dict, ticked: set[str] = frozenset()) -> str:
    """The week's proposals as a checklist. Boxes already ticked stay ticked."""
    md = [review.get("summary") or "", ""]
    for p in review["proposals"]:
        box = "x" if p["id"] in ticked else " "
        md += [f"- [{box}] **{p['id']}** ({p['kind']}) {p['problem']}",
               f"  - Change: {p['change']}", f"  - Evidence: {p['evidence']}",
               f"  - Expected: {p['expected']}",
               f"  - Measure: {p['measure']}" + (f" (`{p['metric']}`, now "
                                                 f"{tune._number(p['baseline'])})"
                                                 if p["metric"] else ""),
               f"  - Risk: {p['risk']}"]
    if not review["proposals"]:
        md.append("No proposals this week.")
    md += ["", "A pull request that carries one of these out names its id in the title. "
               "See `docs/SELF_IMPROVE.md`."]
    return "\n".join(md)


def post_issue(review: dict) -> str | None:
    """Open this week's issue, or update it if it is already there. Returns its address.

    Does nothing without GITHUB_TOKEN and GITHUB_REPO.
    """
    if not (config.get("GITHUB_TOKEN") and _repo()):
        return None
    title = issue_title(review["week"])
    listed = _github("GET", f"/issues?labels={LABEL}&state=all&per_page=20").json()
    old = next((i for i in listed if i.get("title") == title), None)
    if old:
        ticked = set(re.findall(r"- \[[xX]\] \*\*(SR-[\d-]+)\*\*", old.get("body") or ""))
        _github("PATCH", f"/issues/{old['number']}", json={"body": issue_body(review, ticked)})
        return old.get("html_url")
    made = _github("POST", "/issues", json={"title": title, "body": issue_body(review),
                                            "labels": [LABEL]}).json()
    return made.get("html_url")


# --- the report --------------------------------------------------------------

ROWS = (   # label, then where the number is in a window
    ("Sold prices recorded", ("sales_recorded",)),
    ("Picked auctions that sold", ("scorecard", "calls")),
    ("…closed at or under the max (%)", ("scorecard", "under_max_rate")),
    ("Going rate off by, typically (%)", ("scorecard", "value_gap_pct")),
    ("Going rate bias (%, + = sold above it)", ("scorecard", "value_bias_pct")),
    ("Missed steals", ("missed_steals", "count")),
    ("Picks", ("picks", "count")),
    ("Picks hidden (%)", ("picks", "skipped_pct")),
    ("Feedback given on picks", ("feedback", "given")),
    ("Auction picks sniped (%)", ("picks", "sniped_pct")),
    ("Your max minus the bot's, average ($)", ("picks", "avg_my_max_minus_bot_max")),
    ("Alerts sent", ("alerts", "sent")),
    ("Alerts dropped as stale", ("alerts", "dropped_as_stale")),
    ("Bedtime bundle size, average", ("alerts", "avg_bedtime_bundle_size")),
    ("Claude cost ($)", ("cost", "usd_total")),
    ("Claude cost per pick ($)", ("cost", "usd_per_pick")),
    ("Gixen sign-outs", ("reliability", "gixen_sign_outs")),
)


def _dig(data: dict, path: tuple) -> str:
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return "–" if data is None else f"{data:g}"


def render(review: dict) -> str:
    """The review as a Markdown report: the numbers, the proposals, the tuning."""
    numbers = review["metrics"]
    week, before = numbers["this_week"], numbers["previous_4_weeks"]
    md = [f"# {profile.get().app_name} self-review: {review['week']}", "",
          review.get("summary") or "", "", "## The numbers", "",
          "| | This week | The 4 weeks before |", "|---|---|---|"]
    md += [f"| {label} | {_dig(week, path)} | {_dig(before, path)} |" for label, path in ROWS]
    for label, key in (("Failed runs", "failures"), ("Runs", "runs")):
        md.append(f"| {label} | {sum(week['reliability'][key].values())} | "
                  f"{sum(before['reliability'][key].values())} |")
    md += ["", "Per price band, picked auctions that sold this week:"]
    md += [f"- {b['band']}: {b['calls']} sold, going rate bias {_dig(b, ('value_bias_pct',))}%"
           for b in week["scorecard"]["bands"]]
    md += ["", "By where the going rate came from:"]
    md += [f"- {name}: {s['calls']} sold, gap {_dig(s, ('value_gap_pct',))}%, "
           f"bias {_dig(s, ('value_bias_pct',))}%"
           for name, s in (week["scorecard"].get("by_source") or {}).items()]
    snipes = week.get("snipes") or {}
    if snipes.get("outcomes"):
        md += ["", f"Snipes on picks ({snipes['on_picks']}, {snipes['unattributed']} "
               "not on a pick): " + ", ".join(f"{n} {o}" for o, n in
                                                snipes["outcomes"].items())]
    md += ["", "Calibration (the bot's own correction of guessed going rates): "
           + calibrate.summary(numbers.get("calibration") or {})]
    md += ["", f"## Proposals ({len(review['proposals'])})"]
    for p in review["proposals"]:
        md += ["", f"### {p['id']} ({p['kind']}): {p['problem']}",
               f"- **Evidence:** {p['evidence']}", f"- **Change:** {p['change']}",
               f"- **Expected:** {p['expected']}",
               f"- **Measure:** {p['measure']}"
               + (f" (`{p['metric']}`, now {tune._number(p['baseline'])})" if p["metric"]
                  else ""),
               f"- **Risk:** {p['risk']}"]
    if not review["proposals"]:
        md.append("- None this week.")
    md += ["", "## Is it getting better?"]
    for f in review["follow_up"]:
        mark = "done" if f["done"] else "not done"
        moved = "" if not f["metric"] else (
            f" · `{f['metric']}` {tune._number(f['then'])} → {tune._number(f['now'])}"
            + (f" ({f['moved']})" if f["moved"] else ""))
        md.append(f"- **{f['id']}** ({mark}): {f['change']}{moved}")
    if not review["follow_up"]:
        md.append("- No earlier proposals to follow up yet.")
    md += ["", "## Self-tuning"]
    tuned = review["tune"]
    if tuned.get("name"):
        md.append(f"- This week: `{tuned['name']}` {tune._number(tuned['old'])} → "
                  f"{tune._number(tuned['new'])} ({tuned['note']}), judged by "
                  f"{tuned['metric']} in two weeks.")
    else:
        md.append(f"- No change this week ({tuned['note']}).")
    md += [f"- Judged this week: `{j['name']}` {tune._number(j['old'])} → "
           f"{tune._number(j['new'])}: {j['status']} ({j['note']})" for j in review["judged"]]
    md += ["", "| Tunable | Now | Default | Range | Step |", "|---|---|---|---|---|"]
    md += [f"| `{name}` | {t['value']:g} | {t['default']:g} | {t['min']:g} to {t['max']:g} | "
           f"{t['step']:g} |" for name, t in numbers["tunables"].items()]
    return "\n".join(md) + "\n"


def has_data(numbers: dict) -> bool:
    """Is there anything to review yet? A bot a day old has no week to look back on."""
    return any(numbers[w]["picks"]["count"] or numbers[w]["sales_recorded"]
               for w in ("this_week", "previous_4_weeks"))


def run(con, now: datetime | None = None, dry_run: bool = False) -> str:
    """The whole weekly review. Returns a one-line summary."""
    now = now or datetime.now(config.TZ)
    week = now.date().isoformat()
    judge = judge_for(con)
    judged = tune.review(con, judge, now.timestamp(), dry_run)   # undo what hurt, first
    numbers = bundle(con, now)
    if not has_data(numbers):
        return "nothing to review yet: no picks or sold prices in the last five weeks"
    followed = follow_up(con, now, done_ids(now), judge)
    # No tools at all: this prompt can lead to a settings change, so Claude gets
    # only the numbers and can do nothing but answer.
    reply = score.extract_json(score.ask_claude(PROMPT.format(
        app=profile.get().app_name, week=week, metrics=", ".join(METRICS),
        counting_since=con.execute("SELECT min(day) FROM stats").fetchone()[0] or "today",
        bundle=json.dumps(numbers, indent=1),
        earlier=json.dumps(followed, indent=1, ensure_ascii=False)), tools=""))
    review = {"week": week, "summary": _text(reply.get("summary")), "metrics": numbers,
              "proposals": clean(reply, week, judge, now), "follow_up": followed,
              "judged": judged,
              "tune": tune.apply(con, reply.get("tune"), judge, now.timestamp(), dry_run)}
    text = render(review)
    summary = (f"{len(review['proposals'])} proposals, "
               + (f"{review['tune']['name']} tuned" if review["tune"]["applied"]
                  else "nothing tuned")
               + (f", {sum(j['status'] == tune.REVERTED for j in judged)} put back"
                  if judged else ""))
    if dry_run:
        print(text)
        return summary + " (dry run)"
    con.execute("INSERT OR REPLACE INTO reflections (week, data, created_at) VALUES (?,?,?)",
                (week, json.dumps(review, ensure_ascii=False), int(time.time())))
    con.commit()
    path = config.REPORTS_DIR / f"reflect-{week}.md"
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    gitsync.commit_and_push([str(path.relative_to(config.DATA_DIR))], f"self-review: {week}")
    try:
        issue = post_issue(review)
    except (requests.RequestException, RuntimeError) as exc:   # the report is written anyway
        print(f"reflect: could not post the issue: {exc}", file=sys.stderr)
        issue = None
    if issue:
        return summary + f", issue {issue}"
    base = config.get("REPORTS_URL")
    if notify.configured():   # no GitHub token: a short note that links the report
        notify.send(f"{profile.get().app_name}: weekly self-review", summary,
                    url=base.rstrip("/") + "/" + path.name if base else None)
    return summary
