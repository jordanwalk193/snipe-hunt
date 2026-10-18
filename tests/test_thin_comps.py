"""How many recorded sales of the exact card a value has behind it caps its stars."""
import time

from snipehunt import db, score
from snipehunt.ebay_search import Listing

NOW = 1_800_000_000.0
DAY = 86400


def comp(total, days_ago):
    return {"total": total, "sold_at": NOW - days_ago * DAY}


def test_one_sale_caps_at_guess_stars():
    got = score.thin_comps([comp(60, 3)], NOW)
    assert (got["n"], got["value"], got["cap"]) == (1, 60, score.GUESS_STARS)


def test_two_sales_take_the_lower_of_median_and_newest_and_one_star_off():
    got = score.thin_comps([comp(50, 2), comp(70, 9)], NOW)     # newest first
    assert got["value"] == 50 and got["cap"] == 4
    got = score.thin_comps([comp(80, 2), comp(40, 9)], NOW)
    assert got["value"] == 60 and got["cap"] == 4               # median under the newest


def test_three_recent_sales_allow_five_stars():
    got = score.thin_comps([comp(60, 1), comp(62, 5), comp(58, 20)], NOW)
    assert got["value"] == 60 and got["cap"] == 5 and not got["stale"]


def test_sales_all_older_than_180_days_take_another_star_off():
    assert score.thin_comps([comp(60, 200), comp(62, 250), comp(58, 300)], NOW)["cap"] == 4
    assert score.thin_comps([comp(60, 200)], NOW)["cap"] == score.GUESS_STARS - 1
    assert score.thin_comps([comp(60, 10), comp(62, 250), comp(58, 300)], NOW)["cap"] == 5


def test_an_estimate_stays_at_guess_stars():
    pick = {"tier": "fire", "deal": 5, "max_basis": "estimate", "price": 5.0, "shipping": 1.0,
            "is_auction": True, "max": 5.05, "value": 60.0}
    assert score.apply_caps(pick)["deal"] == score.GUESS_STARS


def test_picks_get_the_ledger_value_the_cap_and_the_comps_text():
    con = db.connect()
    now = time.time()
    for n, price in enumerate((40.0, 44.0)):
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at)"
                    " VALUES (?,?,?,0,5,?)", (f"20{n}", "2021 Bowman Chrome Jordan Walker "
                                                       "BCP-236 Atomic /150", price, now - n * DAY))
    item = Listing("300", "2021 Bowman Chrome Jordan Walker BCP-236 Atomic Refractor /150", 4.0,
                   "USD", 0.0, ["AUCTION"], 1, None, "s", "u", None)
    from snipehunt import sales
    comps = sales.ledger_comps(con, item)
    pick = score._pick_dict(item, {"tier": "fire", "max": 9.0, "value": 100.0, "comps": "web"},
                            comps=comps)
    assert pick["value"] == 40.0 and pick["value_claude"] == 100.0   # the ledger comes first
    assert pick["comps_n"] == 2 and pick["deal"] <= 4
    assert pick["comps"].startswith("2 sales, newest ") and pick["comps"].endswith("; web")
    assert pick["max_basis"] == "sales"


def test_the_lane_cannot_beat_the_caps():
    from snipehunt import midtier
    cfg = midtier.config_now()
    item = Listing("1", "2021 Bowman Jordon Walker", 5.0, "USD", 0.0, ["AUCTION"], 0, None,
                   "s", "u", None,
                   signals=[{"name": "typo_name", "weight": 3, "positive": True, "why": "x"}])
    base = {"item_id": "1", "price": 5.0, "shipping": 0.0, "is_auction": True, "max": 1.0,
            "tier": "fire", "value": 200.0, "max_basis": "estimate"}
    guessed = midtier.decide(dict(base), item, cfg)
    assert guessed["deal"] <= score.GUESS_STARS and guessed["tier"] == "green"
    one = midtier.decide(dict(base), item, cfg, [comp(200, 1)], NOW)
    assert one["deal"] <= score.GUESS_STARS
    two = midtier.decide(dict(base), item, cfg, [comp(200, 1), comp(210, 2)], NOW)
    assert two["deal"] <= 4
    three = midtier.decide(dict(base), item, cfg, [comp(200, 1), comp(210, 2), comp(190, 3)],
                           NOW)
    assert three["deal"] == 5
