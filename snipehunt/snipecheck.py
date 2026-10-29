"""A second opinion on the amounts you put into Gixen.

Read-only, like everything that touches Gixen: this only looks at the snipes
you set and says how each amount compares with what the card goes for. The
going rate comes from the bot's own pick for that auction, then from its record
of what the same card sold for, and for a card it knows nothing about, from
one Claude appraisal that is kept so it is only ever paid for once.
"""
from __future__ import annotations

import json
import re
import sys
import time
from types import SimpleNamespace

from . import closes, db, ebay_search, profile, rarity, sales, settings

KEY = "snipe_values"
MAX_APPRAISED = 20       # snipes in one appraisal call
KEEP_DAYS = 30
# How far under (or over) the going rate you would pay, shipping included.
OVER, FAIR, LONG_SHOT = -5, 15, 45
# "Over" only reaches the phone when it costs real money: a $2.22 snipe on a $3
# card is "over" once shipping is counted, but cheap-card going rates are too
# rough to wake anyone for a dollar. The dashboard still shows the verdict.
OVER_PUSH_MIN = 3.00
VERDICTS = {
    "outbid": "Already bid past your snipe",
    "over": "More than it sells for",
    "fair": "About the going rate: likely to win",
    "steal": "A real discount if it holds",
    "long_shot": "Far under what it sells for: rarely wins",
    "unknown": "No price to judge it by yet",
}
PROMPT = """You are {app}, a card-collecting assistant. {collector} collects \
{player} and {doing} the eBay listings below. For each one, say what \
that exact card (same parallel, serial number and grade) has recently sold for, \
shipping included: its going rate. You only read and recommend; never bid.
{lookup_rule}
{sold}Reply with ONLY one JSON object, no prose, no code fence:
{{"values": [{{"item_id": "…", "value": 25.00, "basis": "sales|estimate", \
"note": "one short line: the sales you found, or why it is an estimate"}}]}}

=== snipes (JSON lines) ===
{snipes}
"""
LOOKUPS = ("Check recent sold prices with web search (130point.com, eBay sold listings) for "
           "at most {lookups} of them, the dearest first, and ask for them all in one step. "
           "Do not open ebay.com pages. Use \"estimate\" where you found no sale.")
NO_LOOKUPS = "You have no web access in this run: give your honest best estimate for each."


def _cache(con) -> dict:
    return json.loads(db.kv_get(con, KEY, "{}"))


def going_rate(con, snipe: dict, rows: list[dict], cache: dict) -> dict | None:
    """What this card goes for and how we know: {"value", "basis", "note"}."""
    row = con.execute("SELECT data FROM picks WHERE item_id=?", (snipe["item_id"],)).fetchone()
    pick = json.loads(row["data"]) if row else {}
    if pick.get("value"):
        return {"value": pick["value"], "note": pick.get("comps") or "",
                "basis": "sales" if pick.get("max_basis") == "sales" else "estimate"}
    last = sales.last_sold(rows, snipe.get("title") or "", snipe["item_id"])
    if last:
        return {"value": last["total"], "basis": "sold",
                "note": f"the same card sold for ${last['total']:.2f} delivered"
                        + (" (shipping assumed)" if last["estimated"] else "")
                        + f" on {last['at']}"
                        + (f" ({last['n']} sales on record)" if last["n"] > 1 else "")}
    return cache.get(snipe["item_id"])


def _shipping(con, item_id: str) -> float | None:
    row = con.execute("SELECT shipping FROM seen WHERE item_id=?", (item_id,)).fetchone()
    return row["shipping"] if row else None


def judge(snipe: dict, rate: dict | None, shipping: float | None) -> dict:
    """One snipe's verdict: how its amount sits against the going rate."""
    amount, bid = snipe.get("max_bid"), snipe.get("current_bid") or 0
    ship = sales.UNKNOWN_SHIPPING if shipping is None else shipping
    out = {"verdict": "unknown", "value": None, "basis": None, "note": "", "pct": None,
           "win": None}
    if rate and rate.get("value") and amount:
        value = float(rate["value"])
        pct = round(100 * (1 - (amount + ship) / value))
        out.update(value=round(value, 2), basis=rate.get("basis"), pct=pct,
                   note=str(rate.get("note") or "")[:200],
                   win=sales.win_bid(value, shipping, amount),
                   verdict="over" if pct < OVER else "fair" if pct < FAIR
                   else "steal" if pct < LONG_SHOT else "long_shot")
    if amount and bid >= amount:
        out["verdict"] = "outbid"
        # still under the going rate at today's bid: worth knowing before it ends
        out["room"] = bool(out["value"]) and bid + ship < out["value"] * 0.9
    out["label"] = VERDICTS[out["verdict"]]
    return out


def check(con, snipes: list[dict]) -> tuple[list[dict], dict]:
    """Every snipe's verdict, in order, and a summary of the lot."""
    rows, cache = sales.with_calls(con), _cache(con)
    checks = [judge(s, going_rate(con, s, rows, cache), _shipping(con, s["item_id"]))
              for s in snipes]
    live = [(s, c) for s, c in zip(snipes, checks) if c["verdict"] != "outbid"]
    valued = [(s, c) for s, c in live if c["value"]]
    counts = {name: sum(c["verdict"] == name for c in checks) for name in VERDICTS}
    return checks, {
        **counts, "snipes": len(snipes),
        # what the snipes still in play would cost at their maxes, and what those cards go for
        "committed": round(sum(s["max_bid"] or 0 for s, _ in valued), 2),
        "worth": round(sum(c["value"] for _, c in valued), 2),
    }


def appraise(con, snipes: list[dict], now: float | None = None,
             doing: str = "has set snipes on") -> int:
    """Ask Claude once for the going rate of snipes (or other listings) nothing else
    can price; the answers are kept KEEP_DAYS. Returns how many."""
    from . import score   # score imports sales; kept out of module import to avoid a loop
    now = now or time.time()
    rows, cache = sales.with_calls(con), _cache(con)
    cache = {k: v for k, v in cache.items() if v.get("at", 0) > now - KEEP_DAYS * 86400}
    todo = [s for s in snipes if s.get("title")
            and going_rate(con, s, rows, cache) is None][:MAX_APPRAISED]
    if not todo:
        return 0
    who, lookups = profile.get(), settings.load()["price_lookups"]
    sold = sales.relevant(con, [SimpleNamespace(item_id=s["item_id"], title=s["title"])
                                for s in todo])
    prompt = PROMPT.format(
        app=who.app_name, collector=who.collector, player=who.described, doing=doing,
        lookup_rule=LOOKUPS.format(lookups=lookups) if lookups else NO_LOOKUPS,
        sold=score.SOLD.format(lines=sales.lines(sold)) if sold else "",
        snipes="\n".join(json.dumps({
            "item_id": s["item_id"], "title": s["title"], "bid_now": s.get("current_bid"),
            "ends": s.get("end_time")}, ensure_ascii=False) for s in todo))
    # A Claude call takes a minute: don't hold the write lock through it (the Gixen
    # cache read above writes), or the dashboard and the cost record wait on it.
    con.commit()
    reply = score.extract_json(score.ask_claude(
        prompt, tools=score.WEB_TOOLS if lookups else ""))
    asked, done = {s["item_id"] for s in todo}, 0
    for raw in reply.get("values") or []:
        try:
            item_id, value = str(raw["item_id"]), round(float(raw["value"]), 2)
        except (KeyError, TypeError, ValueError):
            continue
        if item_id in asked and value > 0:
            cache[item_id] = {"value": value, "at": int(now),
                              "basis": "sales" if raw.get("basis") == "sales" else "estimate",
                              "note": str(raw.get("note") or "")[:200]}
            done += 1
    db.kv_set(con, KEY, json.dumps(cache, ensure_ascii=False))
    print(f"snipecheck: {done} of {len(todo)} snipes appraised", file=sys.stderr)
    return done


# ------------------------------------------------------------ bad snipes
# Price isn't the only way a snipe goes wrong. These catch the ones that slipped
# through: the wrong player, a card already in the binder, the same card sniped
# twice, a grader most collectors skip, an auction the bot passed on, a new seller.
DOUBTS = {
    "not_player": "Not {player}, going by the title",
    "owned": "Already in the collection",
    "twice": "The same card is sniped twice",
    "grader": "A CCG slab (a grader most collectors skip)",
    "passed": "The bot looked at this auction and didn't rate it a steal",
    "seller": "A seller with little feedback",
    "hot_seller": "Hot seller: their auctions close ~{pct}% of the going rate ({n} sold), "
                  "a cheapskate max rarely wins here",
}
SERIOUS = ("not_player", "owned", "twice", "grader")   # these get a phone alert
# Gixen writes emoji as escape codes ("uD83D uDD25Jordan"), which hides the name.
GARBLED = re.compile(r"u[Dd][89A-Fa-f][0-9A-Fa-f]{2}")
SKIP_GRADER = re.compile(r"\bccg\b", re.I)
YEAR = re.compile(r"\b(19|20)\d\d\b")
# Words that make one version of a card differ from another: a Sepia Refractor
# #209 is not the base #209. Both sides must carry the same ones to match.
PARALLEL = frozenset("""refractor refractors xfractor fractor sepia pink blue green gold orange red
purple black silver yellow aqua teal white mojo sparkle shimmer sapphire wave raywave lava
foil foilboard holo prizm atomic speckle geometric geo chrome edition variation sp ssp auto
autograph patch relic jersey""".split())
GRADED = re.compile(r"\b(psa|bgs|sgc|cgc|beckett|gma|hga|ccg|tag|isa|ksa)\s?\d", re.I)
CARD_NO = re.compile(r"#?\s*\b([a-z]{0,6}-?\d{1,4}[a-z]?)\b", re.I)
WARNED_KEY = "snipe_warned"


def clean(title: str | None) -> str:
    return " ".join(GARBLED.sub(" ", title or "").split())


def _numbers(title: str) -> set[str]:
    return {re.sub(r"[^a-z0-9]", "", m.lower()) for m in CARD_NO.findall(title)}


def _graded(title: str) -> bool:
    return bool(GRADED.search(title) or rarity.GRADED.search(title))


def owned_copy(title: str, rows: list[dict]) -> dict | None:
    """The inventory row this listing would duplicate. Careful on purpose, because a
    false alarm on a good snipe is worse than a miss: same year, the row's card
    number in the title, the same parallel words on both sides, the same print
    run, and graded or raw the same way (a graded copy of a raw card is an upgrade)."""
    title = clean(title)
    year = YEAR.search(title)
    if not year:
        return None
    mine, run, graded = sales.words(title), rarity.print_run(title), _graded(title)
    numbers = _numbers(title)
    for r in rows:
        number = re.sub(r"[^a-z0-9]", "", (r.get("card_no") or "").lower())
        if (r.get("status") == "Sold-Traded" or r.get("year") != year.group(0)
                or not number or number not in numbers):
            continue
        theirs = sales.words(" ".join(str(r.get(k) or "") for k in ("set", "card", "parallel")))
        if mine & PARALLEL != theirs & PARALLEL:
            continue
        serial = r.get("serial") or ""
        if run == (rarity.print_run(serial) if serial else None) and graded == bool(r.get("graded")):
            return r
    return None


def _twins(snipes: list[dict]) -> dict[str, str]:
    """Snipes that look like the same card as another snipe: item id -> the other's."""
    out = {}
    info = [(s["item_id"], sales.words(clean(s.get("title"))),
             rarity.print_run(clean(s.get("title"))),
             _graded(clean(s.get("title")))) for s in snipes]
    for i, (a, wa, ra, ga) in enumerate(info):
        for b, wb, rb, gb in info[i + 1:]:
            if wa and wb and len(wa & wb) / len(wa | wb) >= 0.8 and ra == rb and ga == gb:
                out.setdefault(a, b)
                out.setdefault(b, a)
    return out


def doubts(con, snipes: list[dict], rows: list[dict]) -> list[list[dict]]:
    """For each snipe, in order, what might be wrong with it besides the price."""
    from . import report   # report imports snipecheck's neighbours; keep it local
    twins, who = _twins(snipes), profile.get()
    learned = closes.learn(con)
    out = []
    for s in snipes:
        title, found = clean(s.get("title")), []
        # a team bot's snipes name many players: only the player bot can say "not them"
        if title and who.mode != "team" and ebay_search.classify(title) == "no":
            found.append({"kind": "not_player",
                          "text": DOUBTS["not_player"].format(player=who.player)})
        mine = owned_copy(title, rows) if title else None
        if mine:
            found.append({"kind": "owned", "text": f"You already have this: #{mine.get('id')} "
                          f"{report.card_name(mine)} ({mine.get('status')})"})
        if s["item_id"] in twins:
            found.append({"kind": "twice", "text": f"Also sniped as item {twins[s['item_id']]}: "
                          "you could win both"})
        if SKIP_GRADER.search(title):
            found.append({"kind": "grader", "text": DOUBTS["grader"]})
        seen = con.execute("SELECT scored_at FROM seen WHERE item_id=?",
                           (s["item_id"],)).fetchone()
        pick = con.execute("SELECT data FROM picks WHERE item_id=?", (s["item_id"],)).fetchone()
        if seen and seen["scored_at"] and not pick:
            found.append({"kind": "passed", "text": DOUBTS["passed"]})
        row = con.execute("SELECT seller FROM seen WHERE item_id=?", (s["item_id"],)).fetchone()
        heat = closes.seller(learned, row["seller"] if row else None)
        if heat and heat["kind"] == "hot" and heat["basis"] == "own":
            found.append({"kind": "hot_seller", "text": DOUBTS["hot_seller"].format(
                pct=round(100 * heat["share"]), n=heat["n"])})
        if pick and json.loads(pick["data"]).get("new_seller"):
            fb = json.loads(pick["data"]).get("seller_feedback")
            found.append({"kind": "seller", "text": f"New seller ({fb} feedback)"})
        out.append(found)
    return out


def to_warn(con, snipes: list[dict], checks: list[dict], found: list[list[dict]]
            ) -> list[tuple[dict, list[str], set[str]]]:
    """Snipes with a serious doubt, or priced over the going rate, not warned about
    yet: (snipe, what is wrong, the keys to remember once the alert has gone)."""
    warned = set(json.loads(db.kv_get(con, WARNED_KEY, "[]")))
    out = []
    for s, check, doubt in zip(snipes, checks, found):
        new = [d for d in doubt if d["kind"] in SERIOUS
               and f"{s['item_id']}:{d['kind']}" not in warned]
        lines, keys = [d["text"] for d in new], {f"{s['item_id']}:{d['kind']}" for d in new}
        over_by = -check["pct"] / 100 * check["value"] if check.get("verdict") == "over" else 0
        if over_by >= OVER_PUSH_MIN and f"{s['item_id']}:over" not in warned:
            lines.append(f"Over the going rate: about ${check['value']:.2f} delivered, "
                         f"your snipe would pay {-check['pct']}% more")
            keys.add(f"{s['item_id']}:over")
        if lines:
            out.append((s, lines, keys))
    return out


def mark_warned(con, keys: set[str]) -> None:
    warned = json.loads(db.kv_get(con, WARNED_KEY, "[]"))
    warned += sorted(keys - set(warned))
    db.kv_set(con, WARNED_KEY, json.dumps(warned[-500:]))
