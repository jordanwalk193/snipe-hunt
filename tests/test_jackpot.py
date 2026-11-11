import json
from datetime import datetime, timedelta

from snipehunt import config, db, jackpot, notify, snapshot

NOW = datetime.now(config.TZ)


def pick(item_id="1", **kw):
    return {"item_id": item_id, "card": "2023 Chrome Rookie Auto /99", "tier": "green",
            "price": 0.99, "shipping": 1.00, "total": 1.99, "value": 30.0, "max_basis": "sales",
            "is_auction": False, "best_offer": False, "end_time": None,
            "url": f"https://www.ebay.com/itm/{item_id}", **kw}


def test_what_counts_as_a_jackpot():
    assert jackpot.check(pick()) == "sales"                          # $30 card for $1.99
    assert jackpot.check(pick(value=18.0)) is None                    # not a $20 card
    assert jackpot.check(pick(price=5.50)) is None                    # $6.50 is 22% of $30
    assert jackpot.check(pick(max_basis="estimate")) == "guess"       # 7% of a guess still counts
    assert jackpot.check(pick(max_basis="estimate", price=3.00)) is None   # 13%: not for a guess
    assert jackpot.check(pick(shipping=None, price=0.99)) == "sales"  # $5 assumed: 20% exactly
    for bad in ({"new_seller": True}, {"scam_risk": True}, {"unverified": True},
                {"flags": ["fake_auto"]}, {"tier": "drop"}):
        assert jackpot.check(pick(**bad)) is None


def test_an_auction_counts_only_near_its_end_and_under_the_max():
    soon = (NOW + timedelta(minutes=50)).isoformat(timespec="minutes")
    later = (NOW + timedelta(hours=9)).isoformat(timespec="minutes")
    assert jackpot.check(pick(is_auction=True, end_time=soon, max=15.15), NOW) == "sales"
    assert jackpot.check(pick(is_auction=True, end_time=later, max=15.15), NOW) is None
    assert jackpot.check(pick(is_auction=True, end_time=soon, max=0.50), NOW) is None


def test_one_loud_alert_each_even_at_night_and_with_alert_settings_off(monkeypatch):
    con = db.connect()
    sent = []
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: True)
    monkeypatch.setattr(notify, "send", lambda title, msg, url=None, image=None, urgent=False, actions=None, critical=False:
                        sent.append((title, msg, urgent)))
    db.kv_set(con, "settings", json.dumps({"alert_green": False, "alert_min_stars": 5}))
    p = pick(deal=5)
    db.touch(con, p["item_id"], p["card"], False, None, 1.0, 0.99, "s")
    assert notify.deliver(con, [p], now=NOW) == 0                   # nothing in the normal bundle
    assert len(sent) == 1 and sent[0][2] is True
    assert sent[0][0] == "🚨 JACKPOT: $30 card for $1.99" and "Buy it now" in sent[0][1]
    notify.deliver(con, [p], now=NOW)
    assert len(sent) == 1                                             # never twice


def test_the_switch_turns_them_off(monkeypatch):
    con = db.connect()
    db.kv_set(con, "settings", json.dumps({"jackpot_alerts": False}))
    assert jackpot.alert(con, [pick()], NOW) == []


def test_the_dashboard_lists_and_flags_them():
    con = db.connect()
    for p in (pick("1"), pick("2", price=12.0)):
        db.save_pick(con, p)
        db.touch(con, p["item_id"], p["card"], False, None, p["shipping"], p["price"], "s")
    snap = snapshot.snapshot(con, NOW)
    assert [p["item_id"] for p in snap["jackpots"]] == ["1"]
    flags = {p["item_id"]: p["jackpot"] for p in snap["picks"]}
    assert flags == {"1": "sales", "2": None}


# ------------------------------------------------------------- the once-a-month kind
def card() -> str:
    from snipehunt import profile
    return f"2023 Topps Chrome {profile.get().player} #150 Refractor RC"


def _sale(con, item, total, days_ago):
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES (?,?,?,?,?,?)",
                (item, card(), total - 1.0, 1.0, 5, NOW.timestamp() - days_ago * 86400))


def test_mega_is_a_40_dollar_card_for_a_tenth():
    assert jackpot.level(pick(value=60.0, price=3.0, shipping=1.0)) == "mega"     # $4 for a $60 card
    assert jackpot.level(pick(value=30.0, price=0.99, shipping=1.0)) == "jackpot"  # under $40
    assert jackpot.level(pick(value=60.0, price=6.0, shipping=1.0)) == "jackpot"   # 12%: not mega
    flagged = jackpot.mark([pick(value=60.0, price=3.0)], NOW)
    assert flagged[0]["jackpot_level"] == "mega"
    title, _ = jackpot.message(flagged[0])
    assert title.startswith("🚨🚨 HOLY SH*T: $60 card for $4.00")
    assert jackpot.message(jackpot.mark([pick()], NOW)[0])[0].startswith("🚨 JACKPOT")


def test_the_ledger_finds_one_claude_never_picked_and_flags_a_card_you_own():
    con = db.connect()
    for i, (total, days) in enumerate([(58.0, 5), (62.0, 12), (60.0, 20)]):
        _sale(con, f"s{i}", total, days)
    db.touch(con, "900", card(), False, None, 1.0, 3.0, "seller1")                    # $4 Buy It Now
    db.touch(con, "901", card(), False, None, 1.0, 30.0, "seller2")                   # half price: no
    db.touch(con, "902", card(), True, NOW.timestamp() + 3 * 86400, 1.0, 2.0, "s3")  # days left: no
    db.touch(con, "903", "2023 Topps Chrome Someone Else Entirely #150 Refractor RC",
             False, None, 1.0, 3.0, "s4")                                           # another player
    con.commit()
    own = [{"year": "2023", "set": "Topps Chrome", "card_no": "150", "parallel": "Refractor",
            "status": "In binder"}]
    found = jackpot.scan(con, set(), NOW, rows=own)
    assert [p["item_id"] for p in found] == ["900"]
    p = found[0]
    assert p["value"] == 60.0 and p["comps"] == 3 and p["ledger_only"] and p["owned"]
    assert not jackpot.scan(con, set(), NOW, rows=[])[0]["owned"]
    assert jackpot.scan(con, {"900"}, NOW, rows=own) == []        # already a pick: judged as one
    flagged = jackpot.mark(found, NOW)
    assert flagged[0]["jackpot_level"] == "mega"
    _, body = jackpot.message(flagged[0])
    assert "not scored by Claude" in body and "flip or trade" in body


class Client:
    """eBay's item read, made up: 901 has sold out, 902 is from a brand-new seller."""
    def __init__(self):
        self.reads = []

    def item(self, item_id, **kw):
        self.reads.append((item_id, kw))
        base = {"itemId": f"v1|{item_id}|0", "legacyItemId": item_id, "title": card(),
                "buyingOptions": ["FIXED_PRICE"], "price": {"value": "3.50", "currency": "USD"},
                "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
                "image": {"imageUrl": "https://i.ebayimg.example/x.jpg"},
                "shippingOptions": [{"shippingCost": {"value": "1.00", "currency": "USD"}}],
                "seller": {"username": "seller1", "feedbackScore": 250, "feedbackPercentage": "99.8"}}
        if item_id == "901":
            base["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]
        if item_id == "902":
            base["seller"] = {"username": "brandnew", "feedbackScore": 0}
        return base


def test_a_ledger_jackpot_is_read_on_ebay_first_and_dropped_when_gone():
    con = db.connect()
    for i, (total, days) in enumerate([(58.0, 5), (62.0, 12)]):
        _sale(con, f"s{i}", total, days)
    for n, price in (("900", 3.0), ("901", 2.0), ("902", 2.5)):
        db.touch(con, n, card(), False, None, 1.0, price, "seller")
    con.commit()
    client = Client()
    live = jackpot.ledger_run(con, client, set(), NOW)
    assert [p["item_id"] for p in live] == ["900"]          # 901 sold out, 902 a brand-new seller
    assert all(kw == {"fresh": True} for _, kw in client.reads)   # this minute's listing, not a cache
    assert live[0]["price"] == 3.5 and live[0]["image"].endswith("x.jpg")
    assert [p["item_id"] for p in jackpot.stored(con, NOW)] == ["900"]
    snap = snapshot.snapshot(con, NOW)
    assert [p["item_id"] for p in snap["jackpots"]] == ["900"]
    got = snap["jackpots"][0]
    assert got["jackpot_level"] == "mega" and got["ledger_only"] and got["comps"] == 2


def test_mega_rings_through_silent_and_fires_a_home_assistant_event(monkeypatch):
    con = db.connect()
    sent, events = [], []
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(k))
    monkeypatch.setattr(notify, "fire_event", lambda e, d: events.append((e, d)))
    jackpot.alert(con, [pick(value=60.0, price=3.0)], NOW)
    jackpot.alert(con, [pick("2")], NOW)
    assert sent[0]["critical"] is True and sent[0]["urgent"] is True
    assert sent[1]["critical"] is False and sent[1]["urgent"] is True
    assert events[0][0] == "snipe_hunt_jackpot" and events[0][1]["level"] == "mega"
    assert events[1][1]["level"] == "jackpot" and events[1][1]["item_id"] == "2"


def test_a_critical_push_and_the_event_reach_home_assistant(monkeypatch):
    import requests
    from snipehunt import config
    posts = []

    class Resp:
        status_code, text = 200, ""
    monkeypatch.setattr(requests, "post", lambda url, **kw: posts.append((url, kw)) or Resp())
    for key, value in (("HA_URL", "http://ha.example.invalid:8123"), ("HA_TOKEN", "made-up"),
                       ("HA_NOTIFY_SERVICE", "mobile_app_phone")):
        monkeypatch.setitem(config.load(), key, value)
    notify.send("t", "m", urgent=True, critical=True)
    data = posts[-1][1]["json"]["data"]
    assert data["push"]["interruption-level"] == "critical" and data["channel"] == "alarm_stream"
    notify.fire_event("snipe_hunt_jackpot", {"item_id": "1"})
    assert posts[-1][0] == "http://ha.example.invalid:8123/api/events/snipe_hunt_jackpot"
    monkeypatch.setattr(requests, "post", lambda url, **kw: (_ for _ in ()).throw(OSError("down")))
    notify.fire_event("snipe_hunt_jackpot", {})        # never raises
