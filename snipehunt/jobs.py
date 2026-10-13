"""The scheduled jobs: hourly steals, daily drops + inventory, weekly report.

Run by the built-in scheduler (scheduler.py), or by hand:
`python -m snipehunt.jobs hourly`.
Everything here only reads eBay and recommends; nothing bids or buys.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from . import (config, db, ebay_search, gitsync, inventory_sync, limited_drops,
               notify, ops, profile, report, score)


def hourly(dry_run: bool = False) -> str:
    config.require("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    con = db.connect()
    if not dry_run:
        try:   # the inventory and want list may have changed on another machine
            gitsync.pull()
        except RuntimeError as exc:   # GitHub being down shouldn't stop the hunt
            print(f"hourly: {exc}; using the local copy", file=sys.stderr)
    listings = ebay_search.search_all(verbose=True)
    for item in listings:
        db.touch(con, item.item_id, item.title)
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

    picks, scored = (score.score(fresh, verbose=True, on_batch=save)
                     if fresh else ([], []))
    sent = notify.deliver(con, picks, dry_run=dry_run)
    return (f"{len(listings)} listings, {len(fresh)} new, {len(scored)} scored, "
            f"{len(picks)} picks, {sent} alerted")


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
    notify.send(f"{profile.get().app_name}: weekly report", summary,
                url=base.rstrip("/") + "/" + path.name if base else None)
    if notes:
        raise RuntimeError("report written, with problems: " + "; ".join(notes))
    return summary.replace("\n", " · ")


JOBS = {"hourly": hourly, "daily": daily, "weekly": weekly}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one job now")
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would happen; send, write and push nothing")
    args = ap.parse_args()
    sys.exit(ops.guarded(args.job, lambda: JOBS[args.job](args.dry_run), args.dry_run))


if __name__ == "__main__":
    main()
