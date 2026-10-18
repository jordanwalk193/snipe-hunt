"""The odd-cent cheapskate max, one helper for every lane (pricing.py)."""
from snipehunt import midtier, pricing, score
from snipehunt.ebay_search import Listing


def test_d_dollars_and_d_cents():
    assert pricing.odd_cents(25.60) == 25.25
    assert pricing.odd_cents(35.35) == 35.35
    assert pricing.odd_cents(47.99) == 47.47
    assert pricing.odd_cents(3.10) == 3.03


def test_never_above_the_limit():
    assert pricing.odd_cents(25.0) == 24.24     # 25.25 would be over: D-1
    assert pricing.odd_cents(80.0) == 79.79
    for cents in range(100, 10000, 7):
        limit = cents / 100
        assert pricing.odd_cents(limit) <= limit


def test_the_lane_caps_at_max_pay_and_rejects_under_the_band():
    cfg = midtier.config_now()
    item = Listing("1", "2021 Bowman Jordon Walker", 5.0, "USD", 0.0, ["AUCTION"], 1,
                   "2026-10-20T03:30-04:00", "s", "u", None,
                   signals=[{"name": "typo_name", "weight": 3, "positive": True, "why": "x"}])
    rich = midtier.decide({"item_id": "1", "price": 5.0, "shipping": 0.0, "is_auction": True,
                           "value": 500.0, "max": 1.0, "tier": "green", "max_basis": "sales"},
                          item, cfg)
    assert rich["max"] == 79.79                  # capped by max_pay 80, odd cents under it
    cheap = midtier.decide({"item_id": "1", "price": 5.0, "shipping": 0.0, "is_auction": True,
                            "value": 30.0, "max": 1.0, "tier": "green"}, item, cfg)
    assert not cheap["midtier"]["alert"]         # 60% of $30 is under the $20 band
    assert cheap["midtier"]["reason"] == "under the band"


def test_every_auction_pick_gets_odd_cents_and_a_bin_keeps_its_max():
    auction = Listing("1", "t", 2.0, "USD", 1.0, ["AUCTION"], 0, None, "s", "u", None)
    bin_ = Listing("2", "t", 2.0, "USD", 1.0, ["FIXED_PRICE"], None, None, "s", "u", None)
    raw = {"tier": "green", "max": 12.0, "value": 30}
    assert score._pick_dict(auction, raw)["max"] == 11.11
    assert score._pick_dict(bin_, raw)["max"] == 12.0


def test_offer_idea_and_bid_steps():
    assert pricing.offer_idea(40.0) == 33.33     # 85% is 34.00, and 34.34 would be over it
    assert pricing.offer_idea(50.0) == 42.42     # 85% is 42.50
    assert pricing.increment(30.0) == 1.00 and pricing.increment(10.0) == 0.50
    assert pricing.next_bid(30.0, 3) == 31.0 and pricing.next_bid(30.0, 0) == 30.0
