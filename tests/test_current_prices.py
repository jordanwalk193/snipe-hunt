from datetime import datetime, timedelta

from snipehunt import config, db, snapshot

NOW = datetime.now(config.TZ)


def pick(item_id, **kw):
    base = {"item_id": item_id, "card": "2024 Triple Threads Patch Auto /25", "tier": "fire",
            "price": 0.99, "shipping": 4.99, "total": 5.98, "max": 45.45, "value": 90.0,
            "max_basis": "sales", "is_auction": True, "deal": 5, "deal_pct": 44,
            "end_time": (NOW + timedelta(hours=20)).isoformat(timespec="minutes")}
    return {**base, **kw}


def seen(con, p, price, shipping):
    db.save_pick(con, p)
    db.touch(con, p["item_id"], p["card"], p["is_auction"], None, shipping, price, "s")


def test_picks_show_the_latest_bid_not_the_one_they_were_scored_at():
    con = db.connect()
    seen(con, pick("1"), 42.0, 4.99)          # bid up since it was scored, still under the max
    seen(con, pick("2"), 50.0, 4.99)          # bid past the max: can't be won at it
    seen(con, pick("3"), 0.99, None)          # unchanged; shipping not stated this time
    got = {p["item_id"]: p for p in snapshot.live_picks(con, NOW)}
    assert set(got) == {"1", "3"}
    assert got["1"]["price"] == 42.0 and got["1"]["total"] == 46.99
    assert got["1"]["deal_pct"] == 44         # an auction's stars rest on the max, not the bid
    assert got["3"]["shipping"] == 4.99


def test_a_buy_it_now_cut_since_scoring_is_rated_again():
    con = db.connect()
    p = pick("4", is_auction=False, max=None, price=60.0, shipping=0.0, total=60.0,
             value=80.0, deal=2, deal_pct=25, end_time=None, tier="green")
    seen(con, p, 30.0, 0.0)
    (got,) = snapshot.live_picks(con, NOW)
    assert got["price"] == 30.0 and got["deal_pct"] == 62 and got["deal"] >= 4


def test_picks_say_when_their_price_was_read_and_how_many_bids():
    con = db.connect()
    seen(con, pick("5"), 12.0, 4.99)
    db.note_bids(con, "5", 3)
    (got,) = snapshot.live_picks(con, NOW)
    assert got["bids"] == 3 and got["price_at"][:10] == NOW.date().isoformat()
    assert snapshot._slim(got)["bids"] == 3
