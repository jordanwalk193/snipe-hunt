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
    monkeypatch.setattr(notify, "send", lambda title, msg, url=None, image=None, urgent=False:
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
