"""The mid-tier lane's eBay searches, down to the filter strings."""
import re
from datetime import datetime, timezone

from snipehunt import midtier

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def lane():
    return midtier.config_now()


def test_the_example_profile_turns_the_lane_on_with_the_documented_defaults():
    cfg = lane()
    assert cfg["enabled"] and (cfg["min_pay"], cfg["max_pay"], cfg["deal_ratio"]) == (20, 80, 0.6)
    assert cfg["category_id"] == "261328" and cfg["auction_window_hours"] == 36


def test_filter_strings_are_exact():
    cfg = lane()
    assert midtier.auction_filter(cfg, NOW) == (
        "price:[..80],priceCurrency:USD,buyingOptions:{AUCTION},"
        "itemEndDate:[2026-10-18T12:00:00Z..2026-10-20T00:00:00Z]")
    assert midtier.bin_filter(cfg) == (
        "price:[20..80],priceCurrency:USD,buyingOptions:{FIXED_PRICE|BEST_OFFER}")


def test_two_searches_per_word_typos_first_and_sorted_right():
    todo = midtier.queries(lane(), NOW)
    assert len(todo) == 2 * len(midtier.words())
    assert todo[0][1] == "jordon walker"            # a misspelling leads
    sorts = {label.split("-")[1]: sort for label, _, _, sort in todo}
    assert sorts == {"auction": "endingSoonest", "bin": "newlyListed"}
    assert len({label for label, *_ in todo}) == len(todo)   # every label is its own


def test_price_never_goes_without_its_currency_and_end_dates_are_future_utc():
    for _, _, flt, _ in midtier.queries(lane(), NOW):
        assert "price:[" in flt and "price:[" in flt.split("priceCurrency:USD")[0]
        ends = re.search(r"itemEndDate:\[(\S+?)\.\.(\S+?)\]", flt)
        if ends:
            start, stop = ends.groups()
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", start)
            assert start >= NOW.strftime("%Y-%m-%dT%H:%M:%SZ") and stop > start


def test_a_run_spreads_the_daily_search_budget_over_the_hours():
    cfg = dict(lane(), search_daily_budget=48)          # two searches an hour
    first = midtier.this_run(cfg, NOW)
    assert len(first) == 2
    cfg["search_daily_budget"] = 400
    every = midtier.this_run(cfg, NOW)
    names = {q for _, q, _, _ in every}
    assert {"jordon walker", "jordan walker"} <= names   # misspellings and the name every run


def test_the_category_is_sent_as_category_ids(monkeypatch):
    from snipehunt import config, ebay_search
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    client = ebay_search.EbayClient()
    sent = {}
    monkeypatch.setattr(client, "_headers", lambda: {})

    class Answer:
        def json(self):
            return {"itemSummaries": []}
    monkeypatch.setattr(client, "_request", lambda m, u, **kw: sent.update(kw["params"]) or Answer())
    client.search("jordon walker", midtier.bin_filter(lane()), "newlyListed", limit=200,
                  category="261328")
    assert sent["category_ids"] == "261328" and sent["limit"] == 200
    assert sent["fieldgroups"] == "EXTENDED" and sent["sort"] == "newlyListed"
