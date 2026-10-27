"""Steals mode, bid now, the radar, bundles, savings, price drops and the brag board.

Made-up items, sellers and prices only.
"""
import json
from datetime import datetime, timedelta

import pytest

from snipehunt import (bidnow, brag, config, db, jackpot, profile, report, scheduler, score,
                       settings, snapshot, steals)
from snipehunt.ebay_search import Listing

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=config.TZ)
EXAMPLE = config.EXAMPLES / "profile.example.toml"


def L(item_id, title, price, ship, kind="AUCTION"):
    return Listing(item_id, title, price, "USD", ship, [kind], 0 if kind == "AUCTION" else None,
                   None, "exampleshop", "u", None)


def steals_mode(monkeypatch, extra: str = "") -> None:
    monkeypatch.setattr(profile, "_cache",
                        profile.parse(EXAMPLE.read_text() + "\n[steals]\n" + extra))


@pytest.fixture
def con(monkeypatch):
    monkeypatch.setitem(config.load(), "GIXEN_USER", "")
    return db.connect()


def add(con, item_id, seen_ago=60, **kw):
    pick = {"item_id": item_id, "card": f"Card {item_id}", "tier": "green",
            "price": 1.0, "shipping": 1.0, "total": 2.0, "max": 8.08, "value": 40.0,
            "max_basis": "sales", "is_auction": True,
            "end_time": (NOW + timedelta(minutes=30)).isoformat(timespec="minutes"),
            "seller": "exampleshop", "url": f"https://www.ebay.com/itm/{item_id}"}
    pick.update(kw)
    db.touch(con, item_id, pick["card"])
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?",
                (int(NOW.timestamp()) - seen_ago, item_id))
    db.save_pick(con, pick)
    return pick


# ------------------------------------------------------------ off by default
def test_player_mode_has_steals_mode_off_and_unchanged():
    assert not steals.on() and steals.settings_defaults() == {}
    cfg = settings.load()
    assert (cfg["max_share"], cfg["bid_now_alerts"], cfg["drop_alerts"]) == (1.0, False, False)
    pick = {"value": 10.0, "price": 9.0, "shipping": 1.0, "is_auction": False}
    assert steals.fit(pick) is pick                       # nothing is held back
    items = [L("1", "Superfractor 1/1", 400.0, 5.0)]
    assert steals.keep(items) == (items, 0)               # nothing is filtered
    assert steals.prompt_rule() == ""


def test_combined_ship_sellers_default_is_empty():
    assert settings.DEFAULTS["combined_ship_sellers"] == []


# ------------------------------------------------------------ steals mode on
def test_a_steals_section_turns_it_on_with_its_defaults(monkeypatch):
    steals_mode(monkeypatch)
    cfg = settings.load()
    assert steals.on() and (cfg["max_share"], cfg["bid_now_alerts"], cfg["drop_alerts"]) == (
        0.5, True, True)
    assert "50% of its going rate" in steals.prompt_rule()
    steals_mode(monkeypatch, "max_share = 0.35\nbid_now_alerts = false\n")
    assert settings.load()["max_share"] == 0.35 and not settings.load()["bid_now_alerts"]
    steals_mode(monkeypatch, "enabled = false\n")
    assert not steals.on() and settings.load()["max_share"] == 1.0


def test_home_assistant_can_override_steals_settings(monkeypatch, con):
    steals_mode(monkeypatch)
    settings.update(con, {"max_share": 0.4})
    assert settings.load(con)["max_share"] == 0.4
    with pytest.raises(settings.SettingsError):
        settings.update(con, {"max_share": 0.1})          # under the floor


def test_fit_holds_picks_to_half_their_value(monkeypatch):
    steals_mode(monkeypatch)
    # a Buy It Now over half of value delivered is no pick; under it, kept as is
    assert steals.fit({"value": 20.0, "price": 9.5, "shipping": 1.0, "is_auction": False}) is None
    ok = {"value": 20.0, "price": 8.0, "shipping": 1.0, "is_auction": False}
    assert steals.fit(ok) is ok
    # an auction's max is cut to an odd-cent snipe that wins at half of value ($12 of max)
    cut = steals.fit({"value": 30.0, "price": 2.0, "shipping": 3.0, "is_auction": True,
                      "max": 20.2})
    assert cut["max"] == 11.11 and cut["max_claude"] == 20.2
    # already bid past the line: no pick; want-list cards keep their own ceilings
    assert steals.fit({"value": 30.0, "price": 13.0, "shipping": 3.0, "is_auction": True,
                       "max": 20.2}) is None
    want = {"value": 20.0, "price": 19.0, "shipping": 1.0, "is_auction": False, "want": True}
    assert steals.fit(want) is want


def test_scoring_in_steals_mode_cuts_the_max_and_rerates(monkeypatch):
    raw = {"tier": "green", "max": 20.2, "value": 30, "max_basis": "sales"}
    card = Listing("1", "t", 2.0, "USD", 3.0, ["AUCTION"], 0, None, "exampleshop", "u", None)
    plain = score._pick_dict(card, raw)
    steals_mode(monkeypatch)
    held = score._pick_dict(card, raw)
    assert plain["max"] == 20.2 and held["max"] == 11.11
    assert held["deal_pct"] > plain["deal_pct"]           # stars worked out at the new max
    dear = Listing("2", "t", 25.0, "USD", 0.0, ["FIXED_PRICE"], None, None, "x", "u", None)
    assert score._pick_dict(dear, {"tier": "green", "max": 25, "value": 30}) is None


def test_the_cheap_filter_drops_dear_and_high_end_listings(monkeypatch):
    steals_mode(monkeypatch)
    items = [L("1", "2023 Chrome RC", 8.0, 1.0, "FIXED_PRICE"),
             L("2", "2023 Chrome RC Refractor", 30.0, 1.0, "FIXED_PRICE"),
             L("3", "2023 Chrome RC auto /99", 60.0, 4.0),
             L("4", "2023 Chrome Superfractor", 2.0, 1.0),
             L("5", "Printing Plate 1/1", 3.0, 1.0)]
    kept, dropped = steals.keep(items)
    assert [i.item_id for i in kept] == ["1", "3"] and dropped == 3   # autos get $75


# ------------------------------------------------------------ bid now
def test_bid_now_lists_auctions_at_a_tenth_of_value_soonest_first(con):
    add(con, "late", end_time=(NOW + timedelta(minutes=50)).isoformat(timespec="minutes"))
    add(con, "soon", end_time=(NOW + timedelta(minutes=10)).isoformat(timespec="minutes"))
    add(con, "dear", price=9.0)                                     # $10 of $40: not 10%
    add(con, "far", end_time=(NOW + timedelta(hours=3)).isoformat(timespec="minutes"))
    add(con, "now", end_time=(NOW + timedelta(minutes=1)).isoformat(timespec="minutes"))
    add(con, "fake", flags=["fake_auto"])
    found = bidnow.find(con, NOW)
    assert [p["item_id"] for p in found] == ["soon", "late"]
    assert found[0]["pay"] == 2.0 and found[0]["off_pct"] == 95
    title, body = bidnow.message(found[0], NOW)
    assert "BID NOW" in title and "ends in 10 min" in title and "Bid now, think later" in body


class FakeClient:
    def __init__(self, price):
        self.price = price

    def item(self, item_id):
        return {"currentBidPrice": {"value": str(self.price), "currency": "USD"}, "bidCount": 2}


def test_bid_now_alert_is_off_until_turned_on_then_sends_once(monkeypatch, con):
    sent = []
    monkeypatch.setattr("snipehunt.notify.configured", lambda: True)
    monkeypatch.setattr("snipehunt.notify.send", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(bidnow, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW),
                                                         "fromisoformat": datetime.fromisoformat}))
    add(con, "1")
    assert bidnow.alert(con, FakeClient(1.0), NOW) == [] and sent == []   # player mode: off
    settings.update(con, {"bid_now_alerts": True})
    assert [p["item_id"] for p in bidnow.alert(con, FakeClient(1.0), NOW)] == ["1"]
    assert len(sent) == 1 and sent[0][1]["urgent"] is True
    assert bidnow.alert(con, FakeClient(1.0), NOW) == [] and len(sent) == 1   # once
    add(con, "2")
    assert bidnow.alert(con, FakeClient(9.0), NOW) == []                   # bid up since


def test_the_scheduler_starts_last_call_for_a_bid_now(monkeypatch, con):
    add(con, "1")
    assert not scheduler._bid_now_wanted(con, NOW)          # alerts are off
    settings.update(con, {"bid_now_alerts": True})
    assert scheduler._bid_now_wanted(con, NOW)


# ------------------------------------------------------------ radar, bundles
def test_radar_is_sorted_by_ending_time_buy_it_nows_last(con):
    add(con, "b", end_time=(NOW + timedelta(minutes=40)).isoformat(timespec="minutes"))
    add(con, "a", end_time=(NOW + timedelta(minutes=20)).isoformat(timespec="minutes"),
        price=3.0)
    add(con, "bin", is_auction=False, end_time=None, price=5.0, value=40.0)
    rows = steals.radar(con, NOW)
    assert [r["item_id"] for r in rows] == ["a", "b", "bin"]
    assert rows[1]["hot"] and rows[1]["kind"] in ("bid_now", "jackpot")


def test_bundles_add_up_a_sellers_picks_and_ship_once_when_combined():
    picks = [{"item_id": "1", "card": "A", "seller": "combineshop", "value": 20.0,
              "price": 2.0, "max": 5.05, "is_auction": True, "shipping": 4.0,
              "end_time": "2026-10-13T20:00-04:00"},
             {"item_id": "2", "card": "B", "seller": "combineshop", "value": 10.0,
              "price": 3.0, "is_auction": False, "shipping": 4.0},
             {"item_id": "3", "card": "C", "seller": "loneshop", "value": 10.0, "price": 1.0}]
    plain = steals.bundles(picks, [])
    joined = steals.bundles(picks, ["CombineShop"])[0]
    assert len(plain) == 1 and plain[0]["shipping"] == 8.0
    assert (joined["n"], joined["cost"], joined["shipping"], joined["total"]) == (2, 8.05, 4.0, 12.05)
    assert joined["shipping_saved"] == 4.0 and joined["gixen"] == "1 5.05"


# ------------------------------------------------------------ savings and the brag board
def bought(con, rows):
    from snipehunt import inventory
    fields, inv = inventory.read()
    for n, (paid, value, day) in enumerate(rows):
        item = f"55500000000{n}"
        db.save_pick(con, {"item_id": item, "card": f"Bought card {n}", "tier": "green",
                           "price": paid, "value": value, "is_auction": False})
        inv.append(dict.fromkeys(fields, "") | {"id": str(100 + n), "price_paid": str(paid),
                                                "order_date": day, "ebay_item_id": item,
                                                "seller": "secretshop"})
    inventory.write(fields, inv)
    con.commit()


def test_savings_and_the_brag_board(con):
    bought(con, [(0.99, 12.0, "2026-10-10"), (5.0, 20.0, "2026-10-03"),
                 (15.0, 20.0, "2026-09-20")])
    s = steals.savings(con, NOW)
    assert s["all"] == {"n": 3, "paid": 20.99, "value": 52.0, "saved": 31.01, "pct": 40}
    assert s["month"]["n"] == 2 and s["best"]["saved"] == 15.0
    b = brag.board(con, NOW)
    counts = {x["key"]: x["count"] for x in b["badges"]}
    assert counts == {"ninety": 1, "dollar": 1, "half": 2}
    assert [t["paid"] for t in b["top"]] == [0.99, 5.0, 15.0]
    assert b["steal_of_month"]["paid"] == 0.99 and b["streak_weeks"] == 2
    assert b["this_week"] == {"n": 0, "saved": 0}
    # the brag card is safe to share: no seller, item number or order date
    assert "Saved $31.01 on 3 cards" in b["card"] and "92% off" in b["card"]
    for secret in ("secretshop", "555000000000", "2026-10-10"):
        assert secret not in b["card"]


def test_streak_keeps_going_while_this_week_has_nothing_yet():
    rows = [{"pct": 20, "date": d} for d in ("2026-10-08", "2026-10-01", "2026-09-17")]
    assert brag.streak(rows, NOW.date()) == 2             # this week, last, then a gap
    assert brag.streak([], NOW.date()) == 0


def test_brag_is_in_the_weekly_report_only_once_something_is_bought(con):
    assert brag.report_lines(con, NOW) == []
    bought(con, [(2.0, 20.0, "2026-10-12")])   # NOW is a Monday: this week
    lines = brag.report_lines(con, NOW)
    assert lines[0] == "## Brag board" and "This week: 1 bought" in lines[2]


# ------------------------------------------------------------ price drops
def test_a_price_drop_into_steal_range_pushes_once(monkeypatch, con):
    sent = []
    monkeypatch.setattr("snipehunt.notify.configured", lambda: True)
    monkeypatch.setattr("snipehunt.notify.send", lambda *a, **k: sent.append(a))
    p = add(con, "1", is_auction=False, end_time=None, price=8.0, shipping=1.0, value=40.0)
    db.kv_set(con, "moves", json.dumps([{"item_id": "1", "old": 30.0, "cut_pct": 73,
                                         "at": int(NOW.timestamp()) - 600}]))
    assert steals.drop_alert(con, [p], NOW) == []         # player mode: off
    settings.update(con, {"drop_alerts": True})
    assert [d["item_id"] for d in steals.drop_alert(con, [p], NOW)] == ["1"]
    assert "JUST DROPPED" in sent[0][0] and "Was $30.00, now $8.00 (73% cut)" in sent[0][1]
    assert steals.drop_alert(con, [p], NOW) == []         # once


# ------------------------------------------------------------ the snapshot
def test_snapshot_has_the_steals_lists(con):
    add(con, "1")
    snap = snapshot.snapshot(con, NOW)
    for key in ("steals", "bid_now", "radar", "bundles", "savings", "dropped", "brag"):
        assert key in snap
    assert snap["steals"] == {"on": False, "max_share": 1.0}
    assert [p["item_id"] for p in snap["bid_now"]] == ["1"]
