"""A cheap card's value follows the listing's own shipping (pricing.for_listing)."""
from snipehunt import pricing, sales, score
from snipehunt.ebay_search import Listing


def auction(item_id, price, shipping):
    return Listing(item_id, "t", price, "USD", shipping, ["AUCTION"], 0, None, "s", "u", None)


def test_the_value_moves_with_shipping_under_50_only(shipping_aware):
    assert pricing.for_listing(10.0, 2.00) == 10.0          # the typical shipping
    assert pricing.for_listing(10.0, 0.0) == 8.0            # free shipping: sells for less
    assert pricing.for_listing(10.0, 5.0) == 13.0           # $5 shipping: costs more delivered
    assert pricing.for_listing(3.0, 0.0) == 1.5             # never under half the going rate
    assert pricing.for_listing(60.0, 0.0) == 60.0           # dearer cards are left alone
    assert pricing.for_listing(10.0, None) == 10.0          # unknown shipping: left alone
    assert pricing.for_listing(None, 1.0) is None


def test_a_pick_keeps_claudes_number_and_its_max_never_pays_over_the_listing(shipping_aware):
    p = score._pick_dict(auction("1", 1.00, 0.0), {"tier": "green", "max": 9.09,
                                                   "value": 10.0, "max_basis": "estimate"})
    assert p["value"] == 8.0 and p["value_typical"] == 10.0 and p["value_raw"] == 10.0
    assert p["max"] == 7.07               # odd cents under 8.00 delivered, not Claude's 9.09
    p = score._pick_dict(auction("2", 1.00, 5.0), {"tier": "green", "max": 6.06,
                                                   "value": 10.0, "max_basis": "estimate"})
    assert p["value"] == 13.0 and p["max"] == 6.06      # 6.06 + 5 is under 13: kept
    assert score._pick_dict(auction("3", 9.00, 0.0), {"tier": "green", "max": 9.09,
                                                      "value": 10.0}) is None   # bid past 8


def test_a_shipping_move_alone_is_not_counted_as_calibration(shipping_aware):
    p = score._pick_dict(auction("1", 1.00, 0.0), {"tier": "green", "max": 5.05,
                                                   "value": 10.0, "max_basis": "estimate"})
    call = {"basis": "estimate", "value": p["value"], "value_raw": p["value_raw"],
            "calibrated": p.get("calibrated")}
    assert sales.value_source(call) == "guessed"
