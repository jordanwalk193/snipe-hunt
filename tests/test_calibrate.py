import time

from snipehunt import calibrate, db, sales, score, settings
from snipehunt.ebay_search import Listing

NOW = time.time()


def sold(con, item_id, price, value, basis="estimate", value_raw=None, ago=3600):
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                "VALUES (?,?,?,?,?,?)", (item_id, f"card {item_id}", price, 0.0, 3, NOW - ago))
    pick = {"item_id": item_id, "tier": "green", "max": 5.05, "value": value,
            "max_basis": basis}
    if value_raw is not None:
        pick["value_raw"] = value_raw
    db.save_pick(con, pick)


def auction(price=10.0):
    return Listing(item_id="1", title="2023 Topps Chrome Sam Example Refractor #45",
                   price=price, currency="USD", shipping=1.0, buying_options=["AUCTION"],
                   bid_count=1, end_time="2026-10-20T00:00:00Z", seller="s", url="u",
                   image=None)


def test_nothing_is_corrected_until_a_band_has_enough_results():
    con = db.connect()
    for n in range(calibrate.MIN_CALLS - 1):
        sold(con, str(n), 30.0, 20.0, ago=n)
    assert calibrate.factors(con) == {}


def test_guesses_that_run_low_are_raised_and_shrunk_toward_one():
    con = db.connect()
    for n in range(10):   # every $20 guess sold for $30: 50% low
        sold(con, str(n), 30.0, 20.0, ago=n)
    got = calibrate.factors(con)["$10 to $50"]
    assert got["calls"] == 10 and got["bias_pct"] == 50
    assert got["factor"] == 1.25            # 1 + 0.5 * 10/20
    assert "x1.25" in calibrate.summary({"$10 to $50": got})


def test_the_factor_is_held_inside_its_range():
    con = db.connect()
    for n in range(30):   # sold for five times the guess
        sold(con, str(n), 100.0, 20.0, ago=n)
    assert calibrate.factors(con)["$10 to $50"]["factor"] == calibrate.FACTOR_RANGE[1]


def test_values_from_real_sales_and_close_guesses_are_left_alone():
    con = db.connect()
    for n in range(10):
        sold(con, f"s{n}", 30.0, 20.0, basis="sales", ago=n)
        sold(con, f"e{n}", 20.5, 20.0, ago=n)      # 2.5% off: under track_off
    assert calibrate.factors(con) == {}
    assert calibrate.adjust(20.0, "sales", {"$10 to $50": {"factor": 1.3}}) == 1.0


def test_it_learns_from_the_raw_guess_not_from_its_own_correction():
    con = db.connect()
    for n in range(10):   # Claude said $20, the bot showed $25, it sold for $30
        sold(con, str(n), 30.0, 25.0, value_raw=20.0, ago=n)
    assert calibrate.factors(con)["$10 to $50"]["bias_pct"] == 50
    row = sales.with_calls(con)[0]
    assert round(row["off"], 2) == 0.2 and round(row["off_raw"], 2) == 0.5


def test_the_setting_turns_it_off():
    con = db.connect()
    for n in range(10):
        sold(con, str(n), 30.0, 20.0, ago=n)
    settings.set_value(con, "calibrate", False)
    assert calibrate.factors(con) == {}


def test_a_pick_carries_the_corrected_value_and_max_and_keeps_the_raw_one():
    learned = {"$10 to $50": {"factor": 1.25, "calls": 10, "bias_pct": 50}}
    raw = {"item_id": "1", "tier": "green", "max": 12.12, "value": 20.0}
    pick = score._pick_dict(auction(), raw, learned=learned)
    assert pick["value"] == 25.0 and pick["value_raw"] == 20.0 and pick["calibrated"] == 1.25
    assert pick["max"] == 15.15             # 12.12 * 1.25, odd-cent
    plain = score._pick_dict(auction(), raw)
    assert plain["value"] == 20.0 and "value_raw" not in plain and plain["max"] == 12.12
    backed = score._pick_dict(auction(), dict(raw, max_basis="sales"), learned=learned)
    assert backed["value"] == 20.0 and "calibrated" not in backed
