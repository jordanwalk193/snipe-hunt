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


def test_deal_rating_counts_shipping_and_uses_the_max_for_auctions():
    from snipehunt import score
    from snipehunt.ebay_search import Listing
    assert score.rate(5, 10) == (5, 50) and score.rate(8.5, 10) == (2, 15)
    assert score.rate(12, 10) == (1, 0) and score.rate(5, None) == (None, None)
    auction = Listing("1", "t", 0.99, "USD", 1.0, ["AUCTION"], 0, None, "s", "u", None)
    p = score._pick_dict(auction, {"tier": "green", "max": 12.12, "value": 25})
    assert (p["deal"], p["deal_pct"], p["value"]) == (4, 48, 25.0)   # 13.12 against 25
    buy_now = Listing("2", "t", 8.0, "USD", None, ["FIXED_PRICE"], None, None, "s", "u", None)
    p = score._pick_dict(buy_now, {"tier": "yellow", "max": 9.09, "value": "20"})
    assert (p["deal"], p["deal_pct"]) == (4, 35)                     # 8 + assumed 5 shipping
    assert score._pick_dict(buy_now, {"tier": "yellow", "max": 9.09})["deal"] is None


def test_alert_shows_the_deal_rating():
    lines = notify.format_pick(pick(value=45.0, deal=4, deal_pct=42)).split("\n")
    assert "Deal: ★★★★☆ 42% under $45 value at max" in lines
    assert lines[-1] == "123456789012"
    assert not any(l.startswith("Deal") for l in notify.format_pick(pick()).split("\n"))


def test_minimum_stars_setting_filters_rated_picks_only():
    from snipehunt import db, settings
    con = db.connect()
    assert notify.worth_alerting(pick(deal=1))
    settings.update(con, {"alert_min_stars": 3})
    assert not notify.worth_alerting(pick(deal=2))
    assert notify.worth_alerting(pick(deal=3))
    assert notify.worth_alerting(pick())                   # never rated: still alerts
    assert notify.worth_alerting(pick(tier="drop", deal=1))


def test_claude_cost_adds_up_by_day_and_month():
    from datetime import datetime
    from snipehunt import config, db, usage
    usage.record(0.25)
    usage.record("0.5")
    usage.record(None)                                     # a bad figure is ignored
    con = db.connect()
    today = usage.summary(con)
    assert (today["cost_today"], today["calls_today"], today["cost_month"]) == (0.75, 2, 0.75)
    later = usage.summary(con, datetime(2031, 1, 1, tzinfo=config.TZ))
    assert (later["cost_today"], later["cost_month"]) == (0.0, 0.0)


def test_a_card_that_slipped_through_says_so_and_leads_the_alert():
    plain, steal = pick(item_id="1"), pick(item_id="2", tier="yellow", edge="mispriced")
    assert notify.format_pick(steal).startswith("💎 PRICE MISTAKE\n")
    assert "PRICE MISTAKE" not in notify.format_pick(plain)
    body = notify.format_bundle([plain, steal])[1]
    assert body.startswith("💎 PRICE MISTAKE")


def _phone(monkeypatch):
    """Alerts go to a list instead of a phone."""
    sent = []
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setattr(notify, "send", lambda title, body, *a, **kw: sent.append((title, body)))
    return sent


def _listing(item_id, price, auction=True, shipping=1.25, bids=3):
    from snipehunt.ebay_search import Listing
    return Listing(item_id, "t", price, "USD", shipping,
                   ["AUCTION"] if auction else ["FIXED_PRICE"], bids, None, "s", "u", None)


def test_a_held_pick_is_checked_against_todays_price_before_it_is_sent(monkeypatch):
    import json
    from snipehunt import db
    con, sent = db.connect(), _phone(monkeypatch)
    held = [pick(item_id="1"),                                   # bid past the 25.25 max
            pick(item_id="2"),                                   # still under it
            pick(item_id="3", is_auction=False, end_time=None, price=8.0, total=9.25,
                 max=9.09, value=20.0),                          # Buy It Now, since sold
            pick(item_id="4", is_auction=False, end_time=None, price=8.0, total=9.25,
                 max=9.09, value=20.0)]                          # Buy It Now, since cut
    db.kv_set(con, notify.PENDING_KEY, json.dumps(held))
    for p in held:
        db.touch(con, p["item_id"], "t")
    con.execute("UPDATE seen SET last_seen=? WHERE item_id='3'",
                (int(NOW_DAY.timestamp()) - 3 * 3600,))
    current = {"1": _listing("1", 31.0), "2": _listing("2", 14.0, bids=6),
               "4": _listing("4", 5.0, auction=False, shipping=0.0)}
    assert notify.deliver(con, [], now=NOW_DAY, current=current) == 2
    (title, body), = sent
    assert {"2", "4"} <= set(body.split("\n")) and "$14.00 + $1.25 ship" in body
    assert "$5.00 + free ship" in body and "75% under $20 value" in body    # rated again
    assert "$12.50" not in body and "$8.00" not in body
    assert json.loads(db.kv_get(con, notify.PENDING_KEY)) == []
