"""Team mode's hourly run: only the shortlist reaches Claude, within the daily budget."""
import json

import pytest

from snipehunt import config, db, jobs, notify, profile, settings, shortlist, usage
from snipehunt.ebay_search import Listing

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()


@pytest.fixture(autouse=True)
def team(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    monkeypatch.setattr(jobs.gitsync, "pull", lambda: None)
    monkeypatch.setattr(jobs.sales, "collect", lambda con: 0)
    monkeypatch.setattr(notify, "deliver", lambda con, picks, **k: 0)
    con = db.connect()
    db.touch(con, "1", "an older listing, so this isn't the first run")
    con.commit()


def listing(n, title, price=10.0):
    verdict, via, player = profile.get().match(title)
    return Listing(str(800000000000 + n), title, price, "USD", 1.0, ["FIXED_PRICE"], None, None,
                   "shop", "", None, verdict, via=via, player=player)


LISTINGS = [listing(1, "2023 Topps Cardinals Lars Nootbaar #123"),            # plain: dropped
            listing(2, "Masyn Winn Cardinals Topps Chrome auto /25", 40.0),
            listing(3, "Jordan Walker Cardinals Topps Chrome refractor /99", 12.0),
            listing(4, "Cardinals Topps lot of 50 cards")]                     # lot: dropped


def run(monkeypatch, listings=LISTINGS):
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: list(listings))
    asked = []

    def fake_score(items, verbose=False, on_batch=None, con=None):
        asked.append([i.item_id for i in items])
        return [], list(items)
    monkeypatch.setattr(jobs.score, "score", fake_score)
    return jobs.hourly(), asked


def test_only_the_top_of_the_shortlist_goes_to_claude(monkeypatch):
    settings.update(db.connect(), {"team_claude_per_run": 1})
    summary, asked = run(monkeypatch)
    assert asked == [["800000000002"]]           # the /25 auto ranks first
    assert "4 listings, 1 new, 1 scored" in summary
    view = shortlist.load(db.connect())
    assert view["funnel"]["dropped"]["plain"] == 1 and view["funnel"]["dropped"]["lot"] == 1
    assert view["funnel"]["to_claude"] == 1 and view["funnel"]["new"] == 2
    assert [c["item_id"] for c in view["candidates"]] == ["800000000002", "800000000003"]
    tracked = {r["item_id"]: r["tracked"] for r in db.connect().execute("SELECT * FROM seen")}
    assert tracked["800000000001"] == 0 and tracked["800000000002"] == 1


def test_the_daily_budget_stops_claude_until_tomorrow(monkeypatch):
    con = db.connect()
    settings.update(con, {"team_daily_budget": 1.0})
    days = {usage.summary.__globals__["datetime"].now(config.TZ).date().isoformat():
            {"usd": 1.25, "calls": 3}}
    db.kv_set(con, usage.KEY, json.dumps(days))
    con.commit()
    summary, asked = run(monkeypatch)
    assert asked == [] and "0 new" in summary
    funnel = shortlist.load(db.connect())["funnel"]
    assert funnel["budget_reached"] is True and funnel["to_claude"] == 0
    # the free ranking still reaches the dashboard
    assert len(shortlist.load(db.connect())["candidates"]) == 2


def test_a_dry_run_changes_nothing(monkeypatch):
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: list(LISTINGS))
    monkeypatch.setattr(jobs.score, "score", lambda items, **k: ([], list(items)))
    jobs.hourly(dry_run=True)
    assert shortlist.load(db.connect())["funnel"] is None
