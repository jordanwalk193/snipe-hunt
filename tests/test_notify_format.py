"""What every alert shows: ET and PT, the item number alone, BIN ok, offer idea, Watch-outs."""
from datetime import datetime

from snipehunt import config, notify, pricing


def pick(**kw):
    base = {"item_id": "123456789012", "card": "2021 Bowman Chrome Atomic /150", "tier": "green",
            "max": 25.25, "max_basis": "sales", "price": 12.5, "shipping": 1.25, "total": 13.75,
            "is_auction": True, "end_time": "2026-10-20T03:12-04:00",
            "url": "https://www.ebay.com/itm/123456789012", "image": None, "why": "Sells for $45"}
    base.update(kw)
    return base


def test_end_times_in_eastern_and_pacific():
    end = datetime.fromisoformat("2026-10-20T03:12-04:00")
    assert notify.ends_text(end, zones=False) == "Tue 3:12 AM ET / 12:12 AM PT"
    assert notify.ends_text(end) == "Tue 3:12 AM EDT / 12:12 AM PDT"
    winter = datetime.fromisoformat("2026-12-01T21:00-05:00")
    assert notify.ends_text(winter) == "Tue 9:00 PM EST / 6:00 PM PST"
    assert "ends Tue 3:12 AM EDT / 12:12 AM PDT" in notify.format_pick(pick())


def test_the_bot_time_zone_does_not_change_the_eastern_time(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "TZ", ZoneInfo("America/Chicago"))
    end = datetime.fromisoformat("2026-10-20T02:12-05:00")   # what a Central bot stores
    assert notify.ends_text(end, zones=False) == "Tue 3:12 AM ET / 12:12 AM PT"


def test_item_number_alone_on_its_own_line_in_every_alert():
    for p in (pick(), pick(is_auction=False, max=14.14), pick(new_seller=True,
                                                               seller_feedback=7)):
        lines = notify.format_pick(p).split("\n")
        assert lines[-1] == "123456789012"
        assert sum(line.strip() == "123456789012" for line in lines) == 1


def test_bin_ok_and_offer_idea():
    text = notify.format_pick(pick(is_auction=False, max=14.14, best_offer=True))
    assert "BIN ok ≤ $14.14 · offer idea: $11.11 (you'd send it, not the bot)" in text
    assert "BIN ok" not in notify.format_pick(pick())
    assert "offer idea" not in notify.format_pick(pick(is_auction=False, max=14.14))


def test_watch_outs_line():
    assert "Watch-outs: none" in notify.format_pick(pick()).split("\n")
    flagged = notify.format_pick(pick(fake_auto=True, new_seller=True, seller_feedback=7,
                                      flags=["fake_auto", "thin_listing"]))
    assert ("Watch-outs: printed signature, not an auto, new seller (7 feedback), "
            "thin listing") in flagged


def test_odd_cent_helper_is_the_one_every_lane_uses():
    assert pricing.odd_cents(25.99) == 25.25 and pricing.odd_cents(35.40) == 35.35
    assert pricing.odd_cents(35.0) == 34.34                # never above the max
    from snipehunt import midtier, score
    assert midtier.pricing is pricing and score.pricing is pricing
