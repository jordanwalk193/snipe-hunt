from datetime import datetime

from snipehunt import config, notify

NOW_DAY = datetime(2026, 10, 11, 15, 0, tzinfo=config.TZ)
NOW_NIGHT = datetime(2026, 10, 11, 23, 30, tzinfo=config.TZ)


def pick(**kw):
    base = {"item_id": "123456789012", "card": "2020 Bowman Chrome Auto /99",
            "tier": "fire", "max": 25.25, "max_basis": "sales", "price": 12.5,
            "shipping": 1.25, "total": 13.75, "is_auction": True,
            "end_time": "2026-10-12T21:14-04:00", "url": "https://www.ebay.com/itm/1",
            "image": None, "why": "Sells for $45"}
    base.update(kw)
    return base


def test_format_ends_with_item_number():
    lines = notify.format_pick(pick()).split("\n")
    assert lines[0].startswith("🔥")
    assert "Max: 25.25 (sales)" in lines
    assert lines[-1] == "123456789012"
    assert "ends Mon 9:14 PM EDT" in lines[1]


def test_yellow_threshold():
    assert notify.worth_alerting(pick(tier="yellow", total=1.99))
    assert not notify.worth_alerting(pick(tier="yellow", total=2.50))
    assert notify.worth_alerting(pick(tier="green", total=40))


def test_quiet_hours():
    far = pick()
    soon = pick(item_id="2", end_time="2026-10-12T00:45-04:00")
    drop = pick(item_id="3", tier="drop", is_auction=False, end_time=None)
    send, hold = notify.split_for_quiet_hours([far, soon, drop], NOW_NIGHT)
    assert [p["item_id"] for p in send] == ["2", "3"]
    assert [p["item_id"] for p in hold] == ["123456789012"]
    send, hold = notify.split_for_quiet_hours([far, soon], NOW_DAY)
    assert len(send) == 2 and not hold


def test_expired_auction_dropped():
    old = pick(end_time="2026-10-11T10:00-04:00")
    send, hold = notify.split_for_quiet_hours([old], NOW_DAY)
    assert not send and not hold


def test_unknown_shipping_is_not_a_cheap_yellow():
    assert not notify.worth_alerting(pick(tier="yellow", shipping=None, total=0.55))


def test_messages_are_phone_sized_and_urgent_goes_first():
    picks = [pick(item_id=str(n), tier="green") for n in range(20)]
    picks.append(pick(item_id="soon", tier="yellow", end_time="2026-10-11T16:00-04:00"))
    messages, overflow = notify.plan_messages(picks, NOW_DAY)
    assert [len(m) for m in messages] == [notify.PER_MESSAGE] * notify.MAX_MESSAGES
    assert messages[0][0]["item_id"] == "soon"
    assert len(overflow) == 21 - notify.PER_MESSAGE * notify.MAX_MESSAGES
