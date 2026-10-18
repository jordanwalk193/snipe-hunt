"""eBay's daily allowance: lanes stop in priority order, rate limits end a lane's run,
and a live listing is never read twice in a day."""
from datetime import datetime

import pytest
import requests

from snipehunt import config, db, ebay_search, item_cache, usage


def used_today(con, n):
    db.bump(con, "ebay_calls:hourly", n, datetime.now(config.TZ).date().isoformat())
    con.commit()
    usage._today.clear()


@pytest.fixture
def con():
    return db.connect()


def test_lanes_stop_in_priority_order(con):
    cases = {0: {"midtier", "extra", "hidden", "typo", "keyword", "core", "item"},
             4100: {"hidden", "typo", "keyword", "core", "item"},      # 82%: midtier, extra off
             4300: {"typo", "keyword", "core", "item"},                # 86%: hidden off too
             4600: {"keyword", "core", "item"},                        # 92%: typo off
             4800: {"core", "item"},                                   # 96%: only the core
             5000: set()}                                              # the cap: nothing
    for used, allowed in cases.items():
        used_today(con, used - usage.ebay_used(con=con))
        got = {lane for lane in ("midtier", "extra", "hidden", "typo", "keyword", "core", "item")
               if usage.ebay_allowed(lane, con)}
        assert got == allowed, used


def test_the_cap_is_a_setting(con):
    from snipehunt import settings
    settings.update(con, {"ebay_daily_cap": 1000})
    used_today(con, 850)
    assert not usage.ebay_allowed("midtier", con) and usage.ebay_allowed("core", con)


def test_search_all_skips_the_cut_lanes(con, capsys):
    used_today(con, 4300)                     # midtier, extra and hidden are off
    asked = []

    class Fake:
        def search(self, q, flt, sort, limit=100, offset=0):
            asked.append(self.lane)
            return []
    ebay_search.search_all(Fake())
    assert "auctions-ending" in asked and "newly-listed" in asked
    assert any(a.startswith("typo-") for a in asked)
    assert not any(a.startswith("extra-") for a in asked)
    err = capsys.readouterr().err
    assert err.count("eBay calls used today") == 1   # the 80% line is logged once
    assert "skipped" in err and "extra-" in err


def test_lane_calls_are_counted_and_saved(con, monkeypatch):
    ebay_search._count_call("typo")
    ebay_search._count_call("typo")
    ebay_search._count_call("core")
    from snipehunt import ops
    ops._count(con, "hourly")
    day = datetime.now(config.TZ).date().isoformat()
    totals = db.stat_totals(con, day, day)
    assert totals["ebay_lane:typo"] == 2 and totals["ebay_lane:core"] == 1
    assert totals["ebay_calls:hourly"] == 3


def test_lane_names():
    assert usage.lane_of("auctions-ending") == "core" == usage.lane_of("newly-listed")
    assert usage.lane_of("typo-jordon-walker") == "typo"
    assert usage.lane_of("extra-j-walker") == "extra" == usage.lane_of("sellers")
    assert usage.lane_of("midtier-bin-jordon-walker") == "midtier"
    assert usage.lane_of("midtier-item") == "midtier-item"
    assert usage.lane_of("auto") == "keyword" and usage.lane_of("hidden") == "hidden"


class Answer:
    def __init__(self, status, body=None, text=""):
        self.status_code, self.body, self.text, self.headers = status, body or {}, text, {}

    def json(self):
        return self.body


def client_with(monkeypatch, answers):
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    client = ebay_search.EbayClient()
    monkeypatch.setattr(client, "_headers", lambda: {})
    sent = []

    def request(self, method, url, **kwargs):
        sent.append(url)
        return answers(url, len(sent))
    monkeypatch.setattr(requests.Session, "request", request)
    return client, sent


def test_429_backs_off_twice_then_gives_up(monkeypatch):
    client, sent = client_with(monkeypatch, lambda url, n: Answer(429, text="Too many"))
    with pytest.raises(ebay_search.RateLimited):
        client.search("jordan walker", "buyingOptions:{AUCTION}", None)
    assert len(sent) == 3                     # one try and two retries


def test_call_limit_error_counts_as_a_rate_limit(monkeypatch):
    client, sent = client_with(monkeypatch, lambda url, n: Answer(
        500, text='{"errors":[{"message":"Call limit exceeded"}]}'))
    with pytest.raises(ebay_search.RateLimited):
        client.search("jordan walker", "buyingOptions:{AUCTION}", None)
    assert len(sent) == 3


def test_a_rate_limited_lane_is_skipped_and_the_rest_run(monkeypatch):
    asked = []

    class Fake:
        def search(self, q, flt, sort, limit=100, offset=0):
            asked.append(self.lane)
            if self.lane == "auctions-ending":
                raise ebay_search.RateLimited("429")
            return []
    ebay_search.search_all(Fake())
    assert asked[0] == "auctions-ending" and "newly-listed" in asked


LIVE = {"itemId": "v1|900000000001|0", "itemEndDate": "2099-01-01T00:00:00Z",
        "localizedAspects": [{"name": "Autographed", "value": "Yes"}], "description": "<p>x</p>"}


def test_the_item_cache_reads_a_live_listing_once_a_day(monkeypatch):
    client, sent = client_with(monkeypatch, lambda url, n: Answer(200, LIVE))
    first = client.item("900000000001")
    second = client.item("900000000001")
    assert len(sent) == 1 and first["localizedAspects"] == second["localizedAspects"]
    assert "description" not in second        # the seller's HTML isn't kept
    assert ebay_search.LANE_CALLS["item"] == 1


def test_the_cache_is_not_used_after_the_listing_ends_or_the_next_day(con):
    ended = dict(LIVE, itemEndDate="2020-01-01T00:00:00Z")
    item_cache.put(con, "1", ended)
    assert item_cache.get(con, "1") is None   # sales.py needs eBay's final answer
    item_cache.put(con, "2", LIVE, now=1_000_000.0)
    assert item_cache.get(con, "2", now=1_000_000.0 + 60) is not None
    assert item_cache.get(con, "2", now=1_000_000.0 + 2 * 86400) is None
