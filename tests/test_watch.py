import json

import pytest

from snipehunt import config, db, hidden, jobs, notify, profile, score, watch
from snipehunt.ebay_search import Listing

NOW = 1_800_000_000.0


def listing(item_id, title, price=20.0, auction=False, seller="someone", end=None):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=1.0,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                   end_time=end, seller=seller, url="u", image=None)


def seen(con, item, last_seen=NOW):
    db.touch(con, item.item_id, item.title, item.is_auction, None, item.shipping, item.price,
             item.seller)
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (last_seen, item.item_id))


def test_a_cut_fixed_price_is_spotted_and_a_small_one_is_not():
    con = db.connect()
    seen(con, listing("1", "2023 Chrome Sam Example Gold /50", price=100.0))
    assert watch.spot(con, listing("1", "2023 Chrome Sam Example Gold /50", price=95.0)) is None
    move = watch.spot(con, listing("1", "2023 Chrome Sam Example Gold /50", price=70.0), NOW)
    assert (move["kind"], move["old"], move["price"], move["cut_pct"]) == ("drop", 100.0, 70.0, 30)
    seen(con, listing("2", "2023 Chrome Sam Example Red /5", price=9.0, auction=True))
    assert watch.spot(con, listing("2", "2023 Chrome Sam Example Red /5", price=1.0,
                                   auction=True)) is None   # an auction's price isn't a cut


def test_a_card_that_ended_unsold_and_came_back_is_a_relist():
    con = db.connect()
    seen(con, listing("1", "2023 Chrome Sam Example Gold /50", price=100.0), NOW - 86400)
    seen(con, listing("2", "2023 Chrome Sam Example Red /5", price=50.0), NOW - 86400)
    con.execute("INSERT INTO sales (item_id, title, price, sold_at) VALUES ('2','x',50,1)")
    back = watch.spot(con, listing("9", "2023 Chrome Sam Example Gold /50", price=80.0), NOW)
    assert (back["kind"], back["old"], back["cut_pct"]) == ("relist", 100.0, 20)
    assert watch.spot(con, listing("8", "2023 Chrome Sam Example Red /5", price=40.0),
                      NOW) is None                            # that one sold: a new copy
    assert watch.spot(con, listing("7", "2023 Chrome Sam Example Gold /50", price=80.0,
                                   seller="another"), NOW) is None
    watch.save(con, [back], NOW)
    watch.save(con, [], NOW + 3600)
    assert [m["item_id"] for m in watch.load(con, "2026-01-01T00:00")] == ["9"]
    watch.save(con, [], NOW + 80 * 3600)
    assert watch.load(con, "2026-01-01T00:00") == []          # old news is dropped


class FakeEbay:
    def __init__(self, results):
        self.results, self.asked, self.calls = results, [], 0

    def search(self, q, flt, sort, limit=100):
        self.asked.append(q)
        return self.results.get(q, [])


def raw(item_id, title, price="20.00", options=("FIXED_PRICE",)):
    return {"legacyItemId": item_id, "title": title, "buyingOptions": list(options),
            "price": {"value": price, "currency": "USD"}, "seller": {"username": "someone"}}


def test_card_numbers_are_learned_and_searched_without_the_name():
    con = db.connect()
    for n in range(3):
        db.touch(con, str(n), "2020 Bowman Draft Jordan Walker 1st Bowman #BD-57 Cardinals")
    db.touch(con, "3", "2021 Bowman Jordan Walker BP-12 Cardinals")       # seen once: not trusted
    assert hidden.card_numbers(con) == [("2020", "BD-57")]
    ebay = FakeEbay({"2020 BD-57": [
        raw("10", "2020 Bowman Draft 1st Bowman BD-57 Cardinals prospect"),
        raw("11", "2020 Bowman Draft Jordan Walker 1st Bowman BD-57"),   # has the name already
        raw("12", "2021 Bowman Draft BD-57 Cardinals"),                     # another year's card
        raw("13", "2020 Bowman Draft BD-5 Cardinals")]})                    # another number
    found = hidden.search(con, known=set(), client=ebay)
    assert ebay.asked == ["2020 BD-57"]
    assert [(i.item_id, i.match) for i in found] == [("10", "unclear")]
    assert "leaves the player's name out" in score._compact(found[0])
    assert hidden.search(con, known={"10"}, client=ebay) == []


def test_the_fast_lane_only_pays_for_new_listings_worth_a_look(monkeypatch):
    con = db.connect()
    db.touch(con, "1", "2023 Chrome Jordan Walker base", False, None, 1.0, 2.0, "someone")
    con.commit()
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    ebay = FakeEbay({profile.get().search_name: [
        raw("1", "2023 Topps Chrome Jordan Walker base #45 Cardinals", "2.00"),          # seen before
        raw("2", "2023 Topps Chrome Jordan Walker Gold Refractor /50 Cardinals", "9.00"),
        raw("3", "2023 Topps Jordan Walker base #45 Cardinals", "1.50"),                 # can wait
        raw("4", "2023 Topps Chrome Jordan Walker Red /5 Cardinals", "30.00", ("AUCTION",))]})
    monkeypatch.setattr(jobs.ebay_search, "EbayClient", lambda: ebay)
    asked = []

    def fake_score(items, verbose=False, con=None):
        asked.append([i.item_id for i in items])
        return [{"item_id": "2", "tier": "green", "max": 9.0, "total": 10.0, "price": 9.0,
                 "card": "Gold /50", "is_auction": False}], items
    monkeypatch.setattr(jobs.score, "score", fake_score)
    monkeypatch.setattr(notify, "send", lambda *a, **k: pytest.fail("no phone is set up"))
    assert jobs.fast() == "3 newest, 2 new, 1 scored, 1 picks, 0 alerted"
    assert asked == [["2"]]
    pick = json.loads(con.execute("SELECT data FROM picks").fetchone()["data"])
    assert pick["fast"] is True
    rows = {r["item_id"]: r["scored_total"] for r in con.execute("SELECT * FROM seen")}
    assert rows["2"] is not None and rows["3"] is None     # the plain card waits for the hour
    assert jobs.fast() == "3 newest, 0 new, 0 worth scoring" and len(asked) == 1


def test_the_fast_lane_runs_between_the_hourly_runs_only_when_turned_on(monkeypatch):
    from datetime import datetime
    from snipehunt import scheduler
    con = db.connect()
    for job in ("hourly", "daily", "weekly"):
        db.kv_set(con, f"started:{job}", str(NOW * 2))        # nothing else is due
    at = datetime(2026, 10, 13, 14, 21, tzinfo=config.TZ)
    assert scheduler.due_jobs(con, at) == []                  # off unless asked for
    monkeypatch.setitem(config.load(), "FAST_LANE_MINUTES", "10")
    assert scheduler.due_jobs(con, at) == ["fast"]
    db.kv_set(con, "started:fast", str(at.timestamp()))
    assert scheduler.due_jobs(con, at.replace(minute=29)) == []
    assert scheduler.due_jobs(con, at.replace(minute=31)) == ["fast"]
    assert scheduler.due_jobs(con, at.replace(hour=15, minute=1)) == []   # the hourly run's turn
