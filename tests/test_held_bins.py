"""A Buy It Now held overnight is dropped only once eBay says it sold, not because
the (rotating) deep sweep didn't happen to return it this run."""
from datetime import datetime, timedelta

from snipehunt import config, db, notify
from snipehunt.ebay_search import Listing

NOW = datetime.now(config.TZ)


def bin_pick(item_id="9", **kw):
    return {"item_id": item_id, "card": "2023 Topps Chrome Refractor #72", "tier": "fire", "price": 4.8,
            "shipping": 1.05, "total": 5.85, "max": None, "value": 11.05, "max_basis": "sales",
            "is_auction": False, "deal": 4, "deal_pct": 47, "end_time": None, **kw}


def out_of_results(con, p, hours):
    db.touch(con, p["item_id"], p["card"], False, None, p["shipping"], p["price"], "s")
    ts = int((NOW - timedelta(hours=hours)).timestamp())
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (ts, p["item_id"]))


def live(price=4.8, shipping=1.05):
    return Listing(item_id="9", title="2023 Topps Chrome Refractor #72", price=price, currency="USD",
                   shipping=shipping, buying_options=["FIXED_PRICE"], bid_count=None,
                   end_time=None, seller="s", url="https://ebay.com/itm/9", image=None)


def test_still_listed_is_kept_with_its_price_read_again():
    con = db.connect()
    p = bin_pick()
    out_of_results(con, p, 10)
    asked = []
    got = notify.refresh(con, p, {}, NOW, look=lambda i: asked.append(i) or live(4.0))
    assert asked == ["9"] and got and got["price"] == 4.0
    last = con.execute("SELECT last_seen FROM seen WHERE item_id='9'").fetchone()[0]
    assert last >= NOW.timestamp() - 5


def test_sold_is_dropped():
    con = db.connect()
    p = bin_pick()
    out_of_results(con, p, 10)
    assert notify.refresh(con, p, {}, NOW, look=lambda i: None) is None


def test_recently_seen_needs_no_look_up():
    con = db.connect()
    p = bin_pick()
    out_of_results(con, p, 1)
    def look(i):
        raise AssertionError("no call needed")
    assert notify.refresh(con, p, {}, NOW, look=look) == p


def test_no_look_up_possible_keeps_it_two_days_then_drops_it():
    con = db.connect()
    def cant(i):
        raise notify.CantLook("spent")
    p = bin_pick()
    out_of_results(con, p, 10)
    assert notify.refresh(con, p, {}, NOW, look=cant) == p
    out_of_results(con, p, 50)
    assert notify.refresh(con, p, {}, NOW, look=cant) is None


def test_looker_reads_sold_out_and_ended_as_gone(monkeypatch):
    from snipehunt import ebay_search, usage
    monkeypatch.setattr(usage, "ebay_allowed", lambda lane: True)
    answers = iter([None,
                    {"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]},
                    {"itemEndDate": "2020-01-01T00:00:00Z"}])

    class Client:
        def item(self, i, fresh=False):
            return next(answers)
    monkeypatch.setattr(ebay_search, "EbayClient", Client)
    look = notify.looker(limit=3)
    assert [look("1"), look("2"), look("3")] == [None, None, None]
    try:
        look("4")
        assert False, "budget should be spent"
    except notify.CantLook:
        pass
