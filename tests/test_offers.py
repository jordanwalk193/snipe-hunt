from datetime import datetime, timezone

from snipehunt import db, offers
from snipehunt.ebay_search import Listing

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def listing(item_id, title, price, days, options=("FIXED_PRICE", "BEST_OFFER"), shipping=1.0):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=list(options), bid_count=None, end_time=None, seller="s",
                   url=f"https://www.ebay.com/itm/{item_id}", image=None,
                   listed_at=f"2026-{10 - days // 30:02d}-17T00:00:00.000Z" if days < 270
                   else "2020-01-01T00:00:00.000Z")


def test_an_offer_is_70_percent_of_the_ask_but_under_the_going_rate():
    assert offers.suggest(20.0, 1.0, None) == 13.13            # no going rate: 70% of $20, rounded down
    assert offers.suggest(20.0, 1.0, 16.0) == 11.11            # 80% of $16 less shipping
    assert offers.suggest(50.0, 11.5, 20.0) is None            # would be 9% of the ask: declined
    assert offers.suggest(3.0, 1.36, 4.0) is None              # $1.84 rounds to 1.01: a third


def test_only_stale_best_offer_listings_not_owned_or_rejected(monkeypatch):
    con = db.connect()
    db.add_feedback(con, "5", "overvalued")
    items = [
        listing("1", "2021 Bowman Prospects Jordan Walker #BP-146", 9.99, 60),
        listing("2", "2021 Bowman Prospects Jordan Walker #BP-146", 9.99, 10),           # too new
        listing("3", "2021 Bowman Prospects Jordan Walker #BP-146", 9.99, 60, ("FIXED_PRICE",)),
        listing("4", "2021 Bowman Prospects Jordan Walker #BP-146", 2.50, 60),           # too cheap
        listing("5", "2021 Bowman Prospects Jordan Walker #BP-146", 9.99, 60),           # rejected
        listing("6", "2023 Topps Chrome Jordan Walker #209 RC", 9.99, 60),               # owned
    ]
    rows = [{"id": "53", "year": "2023", "set": "Topps Chrome", "card": "Base", "parallel": "Base",
             "card_no": "209", "serial": "", "graded": "", "status": "In binder"}]
    found = offers.find(con, items, rows, now=NOW)
    assert [o["item_id"] for o in found] == ["1"]
    assert found[0]["offer"] == 6.06 and found[0]["days"] >= 30


def test_a_listing_with_no_start_date_or_a_bad_one_is_skipped():
    now = NOW
    assert offers.days_up(None, now) is None and offers.days_up("soon", now) is None
    assert offers.days_up("2026-09-01T00:00:00.000Z", now) == 46


def test_a_must_have_is_named_and_comes_first(monkeypatch):
    monkeypatch.setattr(offers.musthave, "read", lambda: [{
        "year": "2021", "card": "Bowman BP-146", "search": "jordan walker 2021 bowman bp-146"}])
    items = [listing("1", "2022 Bowman Jordan Walker #BP-12", 9.99, 60),
             listing("2", "2021 Bowman Prospects Jordan Walker #BP-146", 12.99, 60)]
    found = offers.find(db.connect(), items, [], now=NOW)
    assert [(o["item_id"], o["must_have"]) for o in found] == [
        ("2", "Bowman BP-146"), ("1", None)]
