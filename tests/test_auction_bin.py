"""An auction that also has a Buy It Now: the bot sees the BIN until the first bid."""
from datetime import datetime, timedelta

from snipehunt import config, jackpot, notify, score
from snipehunt.ebay_search import Listing, parse_item


def raw(bid, bin_price, options=("AUCTION", "FIXED_PRICE")):
    return {"legacyItemId": "1", "title": "t", "buyingOptions": list(options),
            "currentBidPrice": {"value": str(bid), "currency": "USD"},
            "price": {"value": str(bin_price), "currency": "USD"},
            "itemEndDate": "2026-11-02T20:00:00.000Z", "bidCount": 0}


def test_the_buy_it_now_on_an_auction_is_parsed_only_while_it_is_one():
    item = parse_item(raw(0.99, 15.0))
    assert (item.price, item.bin_price, item.is_auction) == (0.99, 15.0, True)
    assert parse_item(raw(0.99, 15.0, ("AUCTION",))).bin_price is None     # no BIN option
    assert parse_item(raw(20.0, 15.0)).bin_price is None                   # bid past it: not real
    plain = parse_item({"legacyItemId": "2", "title": "t", "buyingOptions": ["FIXED_PRICE"],
                        "price": {"value": "15", "currency": "USD"}})
    assert plain.bin_price is None and plain.price == 15.0


def test_a_pick_carries_the_bin_and_the_alert_names_it():
    end = (datetime.now(config.TZ) + timedelta(days=2)).isoformat(timespec="minutes")
    item = Listing("1", "t", 0.99, "USD", 1.0, ["AUCTION", "FIXED_PRICE"], 0, end, "s", "u",
                   None, bin_price=4.0)
    pick = score._pick_dict(item, {"tier": "green", "max": 4.04, "value": 20.0,
                                   "max_basis": "sales"})
    assert pick["bin_price"] == 4.0
    assert "Or Buy It Now $4.00 (about the max: buy it, the first bid removes that option)" \
        in notify.format_pick(pick)
    dear = score._pick_dict(Listing("2", "t", 0.99, "USD", 1.0, ["AUCTION", "FIXED_PRICE"], 0,
                                    end, "s", "u", None, bin_price=9.0),
                            {"tier": "green", "max": 4.04, "value": 20.0, "max_basis": "sales"})
    assert "Or Buy It Now $9.00 (over the max; the first bid removes it)" in notify.format_pick(dear)


def test_a_cheap_bin_on_an_auction_is_a_jackpot_at_once_not_only_in_the_last_hours():
    end = (datetime.now(config.TZ) + timedelta(days=3)).isoformat(timespec="minutes")
    pick = {"item_id": "1", "card": "c", "tier": "green", "value": 50.0, "max": 9.09,
            "max_basis": "sales", "is_auction": True, "price": 0.99, "shipping": 1.0,
            "end_time": end, "bin_price": 5.0, "bids": 0}
    assert jackpot.check(pick) == "sales" and pick["jackpot_bin"] is True
    assert jackpot.pay(pick) == 6.0
    title, body = jackpot.message(pick)
    assert "$50 card for $6.00" in title and "Buy It Now of $5.00" in body and "first bid" in body
    bid = dict(pick, bids=2)     # the first bid removed the BIN: back to the usual rule
    assert jackpot.check(bid) is None and "jackpot_bin" not in bid
    dear = dict(pick, bin_price=20.0)
    assert jackpot.check(dear) is None
