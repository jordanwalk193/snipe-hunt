"""The scheduled jobs: hourly steals, daily drops + inventory, weekly report.

Run by the built-in scheduler (scheduler.py), or by hand:
`python -m snipehunt.jobs hourly`.
Everything here only reads eBay and recommends; nothing bids or buys.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

from . import (aspects, bidnow, bids, brag, config, db, ebay_search, filters, gitsync, gixen, hidden, inventory,
               inventory_sync, jackpot, limited_drops, lowball, midtier, notify, offers, ops,
               profile, rarity, reflect, report, sales, score, settings, sheets, shortlist,
               snipecheck, steals, usage, watch, wins)


def hourly(dry_run: bool = False) -> str:
    config.require("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    con = db.connect()
    if not dry_run:
        try:   # the inventory and want list may have changed on another machine
            gitsync.pull()
        except RuntimeError as exc:   # GitHub being down shouldn't stop the hunt
            print(f"hourly: {exc}; using the local copy", file=sys.stderr)
    first_run = con.execute("SELECT count(*) FROM seen").fetchone()[0] == 0
    team = profile.get().mode == "team"
    listings = ebay_search.search_all(verbose=True)
    try:   # the same cards listed without the player's name
        if not team:   # a team has no one name to leave out
            listings += hidden.search(con, {i.item_id for i in listings}, verbose=True)
    except Exception as exc:
        print(f"hidden: {exc}", file=sys.stderr)
    # reprints, customs and "you pick" listings never reach Claude; fake autos are flagged
    listings, junked = filters.prefilter(listings)
    if junked:
        print(f"filters: dropped {sum(junked.values())} ("
              + ", ".join(f"{n} {w}" for w, n in junked.items()) + ")", file=sys.stderr)
    moves = _remember(con, listings)
    if not dry_run and not first_run:
        watch.save(con, moves)
    if not dry_run:
        # the lowest print runs for sale, for the dashboard
        rarity.save(con, listings, inventory.read()[1])
    # steals mode: over the delivered ceiling or high-end noise never reaches Claude
    # (still noted as seen above, so price drops are tracked); a no-op otherwise
    listings, dear = steals.keep(listings)
    if dear:
        print(f"steals: dropped {dear} over the ceiling or high-end", file=sys.stderr)
    # Save now: if scoring fails, an open write here would block the failure
    # from being recorded, and these listings are still on eBay either way.
    con.commit()
    if not dry_run:
        try:   # snipes set on auctions the search never returned still get a sold price
            _note_snipes(con)
        except Exception as exc:
            con.rollback()
            print(f"gixen: {exc}", file=sys.stderr)
        try:   # what closed auctions went for; scoring prices from these
            sold = sales.collect(con)
            missed = sales.save_missed(con)   # cheap sales nobody picked, to learn from
            con.commit()
            print(f"sales: {sold} new sold prices, {sales.count(con)} on record, "
                  f"{missed} missed steals", file=sys.stderr)
        except Exception as exc:   # bookkeeping must not stop the hunt
            con.rollback()
            print(f"sales: {exc}", file=sys.stderr)
        try:   # a going rate for any Gixen snipe nothing on record can price
            _appraise_snipes(con)
        except Exception as exc:
            con.rollback()
            print(f"snipecheck: {exc}", file=sys.stderr)
        try:   # a snipe that slipped through: wrong card, a dupe, over the going rate
            _warn_snipes(con)
        except Exception as exc:
            con.rollback()
            print(f"snipecheck: {exc}", file=sys.stderr)
        if not first_run:   # these price from what earlier runs learned
            _deals(con, listings)

    def save(batch, batch_picks) -> None:
        """Commit each scored batch so a later failure doesn't redo it."""
        if dry_run:
            return
        tiers = {p["item_id"]: p["tier"] for p in batch_picks}
        for item in batch:
            db.mark_scored(con, item.item_id, item.total, tiers.get(item.item_id))
        for p in batch_picks:
            db.save_pick(con, p)
        con.commit()

    lane_ids: set[str] = set()
    if not team and not first_run and (midtier.config_now(con) or {}).get("enabled"):
        try:   # the mid-tier lane takes the listings it flags; the rest score as usual
            lane_picks, lane_sent = midtier.run(con, set(), save, dry_run)
            lane_ids = {p["item_id"] for p in lane_picks}
            print(f"midtier: {len(lane_picks)} picks, {lane_sent} alerted", file=sys.stderr)
        except Exception as exc:   # the main lanes come first
            con.rollback()
            print(f"midtier: {exc}", file=sys.stderr)
    if team:   # far too many listings to score: filter and rank them for free first
        fresh = team_shortlist(con, listings, moves, dry_run)
    else:
        fresh = [i for i in listings if db.needs_scoring(con, i.item_id, i.total)
                 and i.item_id not in lane_ids]

    failed = None
    try:
        picks, scored = (score.score(fresh, verbose=True, on_batch=save, con=con)
                         if fresh else ([], []))
    except score.ClaudeError as exc:   # a usage limit: unscored listings wait an hour,
        if first_run:                  # but held picks and the sheet still go out
            raise
        con.rollback()
        picks, scored, failed = [], [], exc
    if first_run and not dry_run:
        return _welcome(con, listings, picks)
    # held and left-over picks are checked against this run's prices before they go out
    sent = notify.deliver(con, picks, dry_run=dry_run,
                          current={i.item_id: i for i in listings})
    if sheets.configured() and not dry_run:
        try:   # the Google Sheet copy of the hunt (optional)
            counts = sheets.push(con)
            print(f"sheets: pushed {sum(counts.values())} rows", file=sys.stderr)
        except Exception as exc:   # a spreadsheet must never stop the hunt
            con.rollback()
            print(f"sheets: {exc}", file=sys.stderr)
    if failed:
        raise failed
    return (f"{len(listings)} listings, {len(fresh)} new, {len(scored)} scored, "
            f"{len(picks)} picks, {sent} alerted")


def team_budget(con, cfg: dict | None = None) -> dict:
    """Today's Claude spend against the team bot's daily budget (0 = no limit)."""
    cfg = cfg or settings.load(con)
    spent = usage.summary(con)["cost_today"]
    budget = cfg["team_daily_budget"]
    return {"budget": budget, "spent_today": spent,
            "reached": bool(budget) and spent >= budget}


def team_shortlist(con, listings: list, moves: list[dict], dry_run: bool = False) -> list:
    """What a team bot sends to Claude this run: the best-ranked new listings, up
    to `team_claude_per_run`, and nothing once the day's budget is spent."""
    cfg = settings.load(con)
    kept, funnel, bundle_rows = shortlist.build(con, listings, moves, cfg=cfg)
    new = [lk for lk in kept if db.needs_scoring(con, lk.item.item_id, lk.item.total)]
    money = team_budget(con, cfg)
    send = [] if money["reached"] else [lk.item for lk in new[:cfg["team_claude_per_run"]]]
    funnel.update(new=len(new), to_claude=len(send), per_run=cfg["team_claude_per_run"],
                  **{f"budget_{k}": v for k, v in money.items()})
    for lk in new[:10]:
        print(f"  {lk.points:5.1f} ${lk.item.total:>7.2f} {lk.item.title[:70]}  "
              f"[{', '.join(lk.reasons)}]", file=sys.stderr)
    dropped = ", ".join(f"{n} {k}" for k, n in funnel["dropped"].items() if n)
    print(f"shortlist: {funnel['found']} found, dropped {dropped or 'none'}; "
          f"{len(kept)} worth a look, {len(new)} new, {len(send)} to Claude"
          + (f" (budget of ${money['budget']:.2f} reached)" if money["reached"] else ""),
          file=sys.stderr)
    if not dry_run:
        shortlist.mark_tracked(con, kept, listings)
        shortlist.save(con, funnel, new, bundle_rows)
        db.prune_untracked(con)
        con.commit()
    return send


def _gixen_list(con) -> list[dict]:
    """The Gixen snipe list (a cached read), or nothing when Gixen is off."""
    if not (settings.load(con)["gixen_enabled"] and config.get("GIXEN_USER")):
        return []
    return gixen.snipes(con)[0]


def _note_snipes(con, now: float | None = None) -> int:
    """Put every Gixen snipe on the player's cards in `seen`, so sales.collect asks
    eBay what it closed at, and in `snipe_log`, so scoring learns what gets sniped
    and for how much. A snipe on one of the bot's picks also counts as a weak
    "good_pick" in `feedback`. Returns how many were noted."""
    now = now or time.time()
    noted = 0
    for s in _gixen_list(con):
        if not s.get("title") or ebay_search.classify(s["title"]) == "no":
            continue   # a snipe on something else; its price is no use here
        # an ended snipe has no time left on the page: ask eBay on the next run
        end_ts = datetime.fromisoformat(s["end_time"]).timestamp() if s["end_time"] else now
        db.note_snipe(con, s["item_id"], s["title"], end_ts, s.get("seller"))
        if s.get("max_bid"):
            db.log_snipe(con, s)
            if con.execute("SELECT 1 FROM picks WHERE item_id=?", (s["item_id"],)).fetchone():
                # sniping a pick is a quiet "good call", with your amount next to the bot's
                db.add_feedback(con, s["item_id"], "good_pick", source="gixen",
                                my_max=s["max_bid"])
        noted += 1
    con.commit()
    return noted


def _appraise_snipes(con) -> None:
    """Price the scheduled Gixen snipes the bot has no pick or sale for (cached reads only)."""
    if snipecheck.appraise(con, _live_snipes(con)):
        con.commit()


def _deals(con, listings: list) -> None:
    """Offers to send on stale Best Offer listings, and lowball snipes on cheap auctions
    of good cards. Both only recommend; a failure in one never stops the hunt."""
    rows = inventory.read()[1]
    # a team bot's Claude money goes to its picks first
    ask = settings.load(con)["deal_appraisals"] and not (
        profile.get().mode == "team" and team_budget(con)["reached"])
    try:   # Buy It Nows that have sat a month with Best Offer on
        n = offers.save(con, listings, rows, appraise=ask)
        con.commit()
        print(f"offers: {n} worth an offer", file=sys.stderr)
    except Exception as exc:
        con.rollback()
        print(f"offers: {exc}", file=sys.stderr)
    try:   # cheap auctions of $15+ cards, each with a low snipe
        n = lowball.save(con, listings, rows, _sniped(con), appraise=ask)
        con.commit()
        print(f"lowball: {n} targets", file=sys.stderr)
    except Exception as exc:
        con.rollback()
        print(f"lowball: {exc}", file=sys.stderr)


def _sniped(con) -> set[str]:
    """Item numbers already in Gixen, so nothing suggests sniping them again."""
    try:
        return {s["item_id"] for s in _gixen_list(con)}
    except Exception as exc:   # Gixen being down mustn't hide the list
        print(f"gixen: {exc}", file=sys.stderr)
        return set()


def _live_snipes(con) -> list[dict]:
    now = datetime.now(config.TZ).isoformat(timespec="minutes")
    return [s for s in _gixen_list(con) if s["status"] == "SCHEDULED" and s["end_time"]
            and s["end_time"] > now]


def _warn_snipes(con, now: datetime | None = None) -> int:
    """One phone alert for snipes with something wrong besides the price, or priced
    over the going rate; each problem is sent once. In quiet hours only snipes
    ending within the urgent window go out. Returns how many snipes it named."""
    now = now or datetime.now(config.TZ)
    live = _live_snipes(con)
    cfg = settings.load(con)
    if not live or not notify.configured() or not cfg["alerts_enabled"]:
        return 0
    _, rows = inventory.read()
    checks, _ = snipecheck.check(con, live)
    items = snipecheck.to_warn(con, live, checks, snipecheck.doubts(con, live, rows))
    if notify.is_quiet(now):
        soon = now + timedelta(hours=cfg["urgent_hours"])
        items = [i for i in items if datetime.fromisoformat(i[0]["end_time"]) <= soon]
    if not items:
        return 0
    blocks = []
    for s, lines, _ in items:
        ends = datetime.fromisoformat(s["end_time"])
        blocks.append(f"{snipecheck.clean(s['title'])[:80]}\nItem {s['item_id']} · "
                      f"your snipe {s['max_bid']} · ends "
                      f"{ends.strftime('%a %I:%M %p').replace(' 0', ' ')}\n"
                      + "\n".join("• " + line for line in lines))
    notify.send(f"⚠️ Gixen: {len(items)} snipe{'s' if len(items) != 1 else ''} to check",
                "\n\n".join(blocks), url="https://www.gixen.com/main/home_2.php")
    snipecheck.mark_warned(con, set().union(*(keys for _, _, keys in items)))
    con.commit()
    return len(items)


def lastcall(dry_run: bool = False) -> str:
    """Between hourly runs: fresh bids on the good picks ending soon (bids.py), then
    lowball last call: the targets ending within a few hours, looked up again, and
    the ones still far under their going rate sent to the phone. No Claude; one eBay
    read per listing due, none at all when nothing is due."""
    config.require("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    con = db.connect()
    client = ebay_search.EbayClient(con)
    read = 0 if dry_run else bids.refresh(con, client)   # the picks ending soon, bid by bid
    # an auction still at a sliver of its value in its last hours, at today's bid
    if not dry_run and settings.load(con)["alerts_enabled"]:
        from . import snapshot
        jackpot.alert(con, snapshot.live_picks(con, datetime.now(config.TZ)))
        steal_alerts(con, client)
    hits = lowball.last_call(con, client, _sniped(con), send=not dry_run)
    return (f"{read} bids read again, "
            f"{len(hits)} lowball last call{'s' if len(hits) != 1 else ''}")


def steal_alerts(con, client) -> None:
    """Bid now, think later (bidnow.py) and Buy It Nows just cut into steal range
    (steals.drop_alert). Each is off unless its setting is on (steals mode turns
    both on); one failing doesn't stop the other."""
    from . import snapshot
    try:
        bidnow.alert(con, client)
    except Exception as exc:
        print(f"bidnow: {exc}", file=sys.stderr)
    try:
        steals.drop_alert(con, snapshot.live_picks(con, datetime.now(config.TZ)))
    except Exception as exc:
        print(f"drops: {exc}", file=sys.stderr)


def _remember(con, listings: list) -> list[dict]:
    """Note every listing as seen; returns the price drops and relists among them."""
    moves = []
    for item in listings:
        move = watch.spot(con, item)
        if move:
            moves.append(move)
        db.touch(con, item.item_id, item.title, item.is_auction,
                 datetime.fromisoformat(item.end_time).timestamp() if item.end_time else None,
                 item.shipping, item.price, item.seller)
        if item.is_auction:
            db.note_bids(con, item.item_id, item.bid_count)
    return moves


def fast(dry_run: bool = False) -> str:
    """The fast lane: brand-new Buy It Now listings, checked between hourly runs.

    A mispriced Buy It Now can be gone in minutes. This is one eBay search for
    the newest fixed-price listings; only the ones never seen before, and
    worth a look, go to Claude, so most runs cost nothing.
    """
    config.require("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    con = db.connect()
    if con.execute("SELECT count(*) FROM seen").fetchone()[0] == 0:
        return "waiting for the first hourly run"
    client = ebay_search.EbayClient()
    client.lane = "fast"
    if not dry_run and settings.load(con)["alerts_enabled"]:
        steal_alerts(con, client)   # an auction's last hour can fall between hourly runs
    found = []
    if not usage.ebay_allowed("keyword"):
        return "daily eBay call budget reached"
    for raw in client.search(profile.get().search_name, "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
                             "newlyListed", limit=50):
        item = ebay_search.parse_item(raw)
        if item is None or item.currency != "USD":
            continue
        if profile.get().mode == "team":
            item.match, item.via, item.player = profile.get().match(item.title)
        else:
            item.match = ebay_search.classify(item.title)
        if item.match != "no" and not item.is_auction:
            found.append(item)
    found, _ = filters.prefilter(found)
    found, _ = steals.keep(found)   # steals mode's ceiling; a no-op otherwise
    new = [i for i in found if con.execute(
        "SELECT 1 FROM seen WHERE item_id=?", (i.item_id,)).fetchone() is None]
    if profile.get().mode == "team":   # the same free ranking as the hourly run, a higher bar
        worth = [lk.item for lk in shortlist.build(con, new, [])[0]
                 if lk.points >= FAST_TEAM_POINTS][:FAST_MAX]
        if worth and team_budget(con)["reached"]:
            return f"{len(found)} newest, {len(new)} new, {len(worth)} worth scoring, budget reached"
    else:
        worth = [i for i in new if fast_worth(con, i)][:FAST_MAX]
    if dry_run:
        for item in worth:
            print(f"  would score: ${item.price:.2f} {item.title}", file=sys.stderr)
        return f"{len(found)} newest, {len(new)} new, {len(worth)} worth scoring (dry run)"
    watch.save(con, _remember(con, new))
    con.commit()   # seen but not scored: whatever Claude doesn't get now, the hourly run will
    used = _fast_calls_today(con)
    if not worth or used >= FAST_CALLS_PER_DAY:
        return (f"{len(found)} newest, {len(new)} new, {len(worth)} worth scoring"
                + (", daily limit reached" if worth else ""))
    db.kv_set(con, FAST_KEY, json.dumps([time.strftime("%Y-%m-%d"), used + 1]))
    con.commit()
    picks, scored = score.score(worth, verbose=True, con=con)
    tiers = {p["item_id"]: p["tier"] for p in picks}
    for item in scored:
        db.mark_scored(con, item.item_id, item.total, tiers.get(item.item_id))
    for p in picks:
        p["fast"] = True
        db.save_pick(con, p)
    con.commit()
    sent = notify.deliver(con, picks)
    return (f"{len(found)} newest, {len(new)} new, {len(scored)} scored, "
            f"{len(picks)} picks, {sent} alerted")


FAST_MAX = 12              # listings in one fast-lane scoring call
FAST_CALLS_PER_DAY = 30    # Claude calls the fast lane may make in a day
FAST_KEY = "fast_calls"
FAST_TEAM_POINTS = 4.0     # a team's new Buy It Now needs this much steal evidence to jump the queue
# Under the setting fast_min_price, a plain card waits for the hourly run.


def fast_worth(con, item) -> bool:
    """Is a new Buy It Now worth a Claude call right now, or can it wait for the hour?"""
    if rarity.print_run(item.title) is not None and rarity.print_run(item.title) <= 99:
        return True
    if rarity.AUTO.search(item.title) or rarity.GRADED.search(item.title):
        return True
    if item.match == "unclear":      # a misspelled or thin title: where mistakes hide
        return True
    # the same card has sold for clearly more than this one is asking
    return any(sales.delivered(s["price"], s["shipping"]) > item.total * 1.3
               for s in sales.relevant(con, [item]) if not s["hint"]
               ) or item.price >= settings.load(con)["fast_min_price"]


def _fast_calls_today(con) -> int:
    day, used = json.loads(db.kv_get(con, FAST_KEY, '["", 0]'))
    return used if day == time.strftime("%Y-%m-%d") else 0


def _welcome(con, listings: list, picks: list[dict]) -> str:
    """The very first run finds everything at once. Send one note, not a flood."""
    for p in picks:
        db.mark_alerted(con, p["item_id"], p["total"])
    con.commit()
    who = profile.get()
    if notify.configured():
        notify.send(f"{who.app_name} is up and running",
                    f"First search done: {len(listings)} {who.player} listings on eBay, "
                    f"{len(picks)} picks waiting on your dashboard. From here on you'll "
                    "only hear about new ones.")
    return f"first run: {len(listings)} listings, {len(picks)} picks, one welcome alert"


def daily(dry_run: bool = False) -> str:
    """Limited drops, then inventory sync. One failing doesn't skip the other."""
    con = db.connect()
    parts, errors = [], []
    try:
        drops = limited_drops.new_drops(con, limited_drops.check_drops(verbose=True))
        sent = notify.deliver(con, [limited_drops.as_pick(d) for d in drops],
                              dry_run=dry_run)
        parts.append(f"{len(drops)} new drops, {sent} alerted")
    except Exception as exc:
        errors.append(f"drops: {exc}")
    events: list[dict] = []
    if not inventory_sync.configured():
        parts.append("email sync is off (no Gmail login set)")
    else:
        try:
            result = inventory_sync.run(dry_run=dry_run)
            events = result["events"]
            for line in result["changes"]:
                print("  " + line, file=sys.stderr)
            parts.append(f"{result['emails']} emails, {len(result['changes'])} "
                         f"inventory changes{' (dry run)' if dry_run else ''}")
        except Exception as exc:
            errors.append(f"inventory sync: {exc}")
    try:   # your own wins: their sold prices, their binder pages, and combined invoices
        if not dry_run:
            parts.append(f"{sales.from_mail(con)} sold prices from win emails")
        parts.append(f"{wins.announce(con, events, dry_run)} wins announced, "
                     f"{wins.remind(con, dry_run=dry_run)} combined-shipping reminders")
    except Exception as exc:
        errors.append(f"wins: {exc}")
    if (midtier.config_now(con) or {}).get("enabled") and notify.configured() and not dry_run:
        try:
            notify.send(f"{profile.get().app_name}: midtier summary", midtier.summary(con))
        except Exception as exc:
            errors.append(f"midtier summary: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors + parts))
    return "; ".join(parts)


def weekly(dry_run: bool = False, now: datetime | None = None) -> str:
    """Sunday report. On the first Sunday of the month it adds the value check."""
    now = now or datetime.now(config.TZ)
    con = db.connect()
    notes: list[str] = []
    if not dry_run:
        gitsync.pull()
        if inventory_sync.configured():
            try:   # pick up this afternoon's emails so the report is current
                inventory_sync.run(verbose=False)
            except Exception as exc:
                notes.append(f"Inventory sync failed before this report: {exc}")
    releases = monthly = None
    if settings.load(con)["aspect_refresh"] and config.get("EBAY_CLIENT_ID") and not dry_run:
        try:   # eBay's own item-specifics names, for the mislabel and fake-auto checks
            aspects.refresh(con)
        except Exception as exc:
            notes.append(f"Item-specifics refresh failed: {exc}")
    try:
        releases = limited_drops.check_new_releases(verbose=True)
    except Exception as exc:
        notes.append(f"New-release check failed: {exc}")
    if now.day <= 7:
        try:
            monthly = report.monthly_section()
        except Exception as exc:
            notes.append(f"Monthly value check failed: {exc}")
    text, summary = report.build(con, now, releases, monthly, notes)
    if dry_run:
        print(text)
        return summary.replace("\n", " · ")
    path = config.REPORTS_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    gitsync.commit_and_push([str(path.relative_to(config.DATA_DIR))],
                            f"report: {now.strftime('%Y-%m-%d')}")
    base = config.get("REPORTS_URL")   # where the reports can be read on the web, if anywhere
    if notify.configured():
        notify.send(f"{profile.get().app_name}: weekly report", summary,
                    url=base.rstrip("/") + "/" + path.name if base else None)
    if notes:
        raise RuntimeError("report written, with problems: " + "; ".join(notes))
    return summary.replace("\n", " · ")


def self_review(dry_run: bool = False) -> str:
    """The weekly self-review: the bot's own numbers, proposals, and one bounded tuning step.

    It changes settings only, never code; see reflect.py and tune.py.
    """
    con = db.connect()
    if not dry_run:
        gitsync.pull()
    return reflect.run(con, dry_run=dry_run)


JOBS = {"hourly": hourly, "daily": daily, "weekly": weekly, "fast": fast,
        "reflect": self_review, "lastcall": lastcall}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one job now")
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; send, write and push nothing")
    args = ap.parse_args()
    os.environ[usage.JOB_ENV] = args.job   # so each Claude call's cost is filed under its job
    sys.exit(ops.guarded(args.job, lambda: JOBS[args.job](args.dry_run), args.dry_run))


if __name__ == "__main__":
    main()
