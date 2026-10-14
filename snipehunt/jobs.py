"""The scheduled jobs: hourly steals, daily drops + inventory, weekly report.

Run by the built-in scheduler (scheduler.py), or by hand:
`python -m snipehunt.jobs hourly`.
Everything here only reads eBay and recommends; nothing bids or buys.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from . import (config, db, ebay_search, gitsync, gixen, hidden, inventory, inventory_sync,
               limited_drops, notify, ops, profile, rarity, report, sales, score, settings,
               snipecheck, watch)


def hourly(dry_run: bool = False) -> str:
    config.require("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    con = db.connect()
    if not dry_run:
        try:   # the inventory and want list may have changed on another machine
            gitsync.pull()
        except RuntimeError as exc:   # GitHub being down shouldn't stop the hunt
            print(f"hourly: {exc}; using the local copy", file=sys.stderr)
    first_run = con.execute("SELECT count(*) FROM seen").fetchone()[0] == 0
    listings = ebay_search.search_all(verbose=True)
    try:   # the same cards listed without the player's name
        listings += hidden.search(con, {i.item_id for i in listings}, verbose=True)
    except Exception as exc:
        print(f"hidden: {exc}", file=sys.stderr)
    moves = _remember(con, listings)
    if not dry_run and not first_run:
        watch.save(con, moves)
    if not dry_run:
        # the lowest print runs for sale, for the dashboard
        rarity.save(con, listings, inventory.read()[1])
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
            print(f"sales: {sold} new sold prices, {sales.count(con)} on record",
                  file=sys.stderr)
        except Exception as exc:   # bookkeeping must not stop the hunt
            con.rollback()
            print(f"sales: {exc}", file=sys.stderr)
        try:   # a going rate for any Gixen snipe nothing on record can price
            _appraise_snipes(con)
        except Exception as exc:
            con.rollback()
            print(f"snipecheck: {exc}", file=sys.stderr)
    fresh = [i for i in listings if db.needs_scoring(con, i.item_id, i.total)]

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

    picks, scored = (score.score(fresh, verbose=True, on_batch=save, con=con)
                     if fresh else ([], []))
    if first_run and not dry_run:
        return _welcome(con, listings, picks)
    # held and left-over picks are checked against this run's prices before they go out
    sent = notify.deliver(con, picks, dry_run=dry_run,
                          current={i.item_id: i for i in listings})
    return (f"{len(listings)} listings, {len(fresh)} new, {len(scored)} scored, "
            f"{len(picks)} picks, {sent} alerted")


def _gixen_list(con) -> list[dict]:
    """The Gixen snipe list (a cached read), or nothing when Gixen is off."""
    if not (settings.load(con)["gixen_enabled"] and config.get("GIXEN_USER")):
        return []
    return gixen.snipes(con)[0]


def _note_snipes(con, now: float | None = None) -> int:
    """Put every Gixen snipe on the player's cards in `seen`, so sales.collect asks
    eBay what it closed at. Returns how many were noted."""
    now = now or time.time()
    noted = 0
    for s in _gixen_list(con):
        if not s.get("title") or ebay_search.classify(s["title"]) == "no":
            continue   # a snipe on something else; its price is no use here
        # an ended snipe has no time left on the page: ask eBay on the next run
        end_ts = datetime.fromisoformat(s["end_time"]).timestamp() if s["end_time"] else now
        db.note_snipe(con, s["item_id"], s["title"], end_ts, s.get("seller"))
        noted += 1
    con.commit()
    return noted


def _appraise_snipes(con) -> None:
    """Price the scheduled Gixen snipes the bot has no pick or sale for (cached reads only)."""
    sniped = _gixen_list(con)
    now = datetime.now(config.TZ).isoformat(timespec="minutes")
    live = [s for s in sniped if s["status"] == "SCHEDULED" and s["end_time"]
            and s["end_time"] > now]
    if snipecheck.appraise(con, live):
        con.commit()


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
    found = []
    for raw in client.search(profile.get().search_name, "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
                             "newlyListed", limit=50):
        item = ebay_search.parse_item(raw)
        if item is None or item.currency != "USD":
            continue
        item.match = ebay_search.classify(item.title)
        if item.match != "no" and not item.is_auction:
            found.append(item)
    new = [i for i in found if con.execute(
        "SELECT 1 FROM seen WHERE item_id=?", (i.item_id,)).fetchone() is None]
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
FAST_MIN_PRICE = 15.0      # under this, a plain card waits for the hourly run


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
               ) or item.price >= FAST_MIN_PRICE


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
    if not inventory_sync.configured():
        parts.append("email sync is off (no Gmail login set)")
    else:
        try:
            result = inventory_sync.run(dry_run=dry_run)
            for line in result["changes"]:
                print("  " + line, file=sys.stderr)
            parts.append(f"{result['emails']} emails, {len(result['changes'])} "
                         f"inventory changes{' (dry run)' if dry_run else ''}")
        except Exception as exc:
            errors.append(f"inventory sync: {exc}")
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


JOBS = {"hourly": hourly, "daily": daily, "weekly": weekly, "fast": fast}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one job now")
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; send, write and push nothing")
    args = ap.parse_args()
    sys.exit(ops.guarded(args.job, lambda: JOBS[args.job](args.dry_run), args.dry_run))


if __name__ == "__main__":
    main()
