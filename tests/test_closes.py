"""Where steals happen: the seller's heat and the ending hour, learned from the ledger."""
import json
import time
from datetime import datetime, timedelta

from snipehunt import closes, config, db, lowball, notify

DAY = 86400


def seed(con, n_small=20, n_big=20, consignor="bigconsign"):
    """Picked auctions that sold: small sellers in the daytime at 0.75 of value,
    the consignor in the evening at 1.4, plus enough consignor sales to count as one."""
    noon = datetime.now(config.TZ).replace(hour=13, minute=0, second=0, microsecond=0)
    nine = noon.replace(hour=21)
    k = 0
    for n in range(n_small):
        k += 1
        _sale(con, str(k), f"seller{k}", noon - timedelta(days=n + 1), 7.5, 10.0)
    for n in range(n_big):
        k += 1
        _sale(con, str(k), consignor, nine - timedelta(days=n + 1), 14.0, 10.0)
    con.commit()


def _sale(con, item, seller, ended, price, value):
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                "VALUES (?,?,?,?,?,?)", (item, "card " + item, price, 0.0, 3, ended.timestamp()))
    db.touch(con, item, "card " + item, is_auction=True, end_ts=ended.timestamp(),
             shipping=0.0, price=price, seller=seller)
    db.save_pick(con, {"item_id": item, "title": "card " + item, "card": "card " + item,
                       "tier": "green", "max": 5.05, "value": value, "is_auction": True,
                       "max_basis": "sales"})


def test_the_ledger_says_who_closes_hot_and_when_it_closes_cheap():
    con = db.connect()
    assert closes.learn(con)["trusted"] is False and closes.seller(closes.learn(con), "x") is None
    seed(con)
    learned = closes.learn(con)
    assert learned["trusted"] and learned["calls"] == 40
    assert learned["groups"]["consignor"]["share"] == 1.4 and learned["groups"]["small"]["share"] == 0.75
    assert learned["hours"]["day"] == {"n": 20, "share": 0.75} and learned["hours"]["evening"]["n"] == 0
    hot = closes.seller(learned, "bigconsign")
    assert hot["kind"] == "hot" and hot["basis"] == "own" and hot["n"] == 20
    new = closes.seller(learned, "never-seen")
    assert new["kind"] == "cold" and new["basis"] == "small" and new["share"] == 0.75
    assert closes.ending(learned, "2026-11-02T14:00:00-05:00")["block"] == "day"
    assert closes.ending(learned, "2026-11-02T21:00:00-05:00") is None   # no evening results yet
    summary = closes.summary(learned)
    assert summary["hot"][0]["seller"] == "bigconsign" and summary["quiet"] == []


def test_an_auction_pick_is_told_how_it_will_likely_close_and_the_alert_says_so():
    con = db.connect()
    seed(con)
    learned = closes.learn(con)
    pick = {"is_auction": True, "seller": "someone", "end_time": "2026-11-02T14:00:00-05:00",
            "value": 20.0, "max": 7.07, "card": "c", "item_id": "9", "price": 1.0, "shipping": 0.0}
    closes.annotate(pick, learned)
    assert pick["closes"]["seller"] == "cold" and pick["closes"]["ending"] == "day"
    assert pick["closes"]["likely"] == 15.0
    assert notify.format_pick(pick).count("🧊 Quiet seller · daytime ending") == 1
    hot = dict(pick, seller="bigconsign")
    closes.annotate(hot, learned)
    assert hot["closes"]["seller"] == "hot" and hot["closes"]["likely"] == 28.0
    assert "🔥 Hot seller" in closes.line(hot) and "a max rarely wins" in closes.line(hot)
    bin_pick = {k: v for k, v in pick.items() if k != "closes"} | {"is_auction": False}
    assert "closes" not in closes.annotate(bin_pick, learned)
    bare = {k: v for k, v in pick.items() if k != "closes"}
    assert closes.line(closes.annotate(bare, None)) == ""


def test_lowball_skips_a_hot_sellers_auctions(monkeypatch):
    from snipehunt.ebay_search import Listing
    con = db.connect()
    seed(con)
    end = (datetime.now(config.TZ) + timedelta(hours=20)).isoformat(timespec="minutes")

    def listing(item_id, seller):
        return Listing(item_id, "2023 Topps Chrome Jordan Walker Gold /50", 2.0, "USD", 0.0,
                       ["AUCTION"], 0, end, seller, "u", None)
    monkeypatch.setattr(lowball.snipecheck, "going_rate",
                        lambda con, pick, rows, cache: {"value": 40.0, "basis": "sales"})
    found = lowball.find(con, [listing("a", "bigconsign"), listing("b", "attic")], [])
    assert [t["item_id"] for t in found["targets"]] == ["b"]
    assert found["targets"][0]["closes"]["seller"] == "cold"


def test_added_to_gixen_marks_a_pick_until_gixens_list_shows_the_snipe(monkeypatch):
    from snipehunt import actions, snapshot
    con = db.connect()
    end = (datetime.now(config.TZ) + timedelta(hours=5)).isoformat(timespec="minutes")
    for item in ("100000000001", "100000000002"):
        db.touch(con, item, "card", is_auction=True, price=1.0, shipping=0.0)
        db.save_pick(con, {"item_id": item, "title": "card", "card": "card", "tier": "green",
                           "max": 5.05, "value": 20.0, "is_auction": True, "end_time": end,
                           "price": 1.0, "shipping": 0.0})
    con.commit()
    assert actions.handle(con, {"action": "sniped", "item_id": "100000000001", "max": "5.05"})[0]
    now = datetime.now(config.TZ)
    snap = snapshot.snapshot(con, now)
    by = {p["item_id"]: p for p in snap["picks"]}
    assert by["100000000001"]["marked"]["max"] == 5.05 and "marked" not in by["100000000002"]
    assert snap["status"]["need_snipe"] == ["100000000002"]        # the marked one needs no snipe
    # Gixen now lists it: the mark gives way to the real snipe
    monkeypatch.setattr(snapshot.gixen, "snipes", lambda con: (
        [{"item_id": "100000000001", "max_bid": 5.05, "current_bid": 1.0, "title": "card",
          "end_time": end, "status": "SCHEDULED"}],
        None, time.time()))
    monkeypatch.setattr(snapshot.config, "get", lambda k, d=None: "u" if k == "GIXEN_USER" else d)
    from snipehunt import settings
    settings.update(con, {"gixen_enabled": True})
    by = {p["item_id"]: p for p in snapshot.snapshot(con, now)["picks"]}
    assert by["100000000001"]["snipe"] == 5.05 and "marked" not in by["100000000001"]
    assert actions.handle(con, {"action": "sniped", "item_id": "100000000001", "undo": True})[0]
    assert snapshot.marked(con) == {}


def test_a_snipe_on_a_hot_seller_gets_a_doubt_the_report_and_review_see_the_table():
    from snipehunt import reflect, report, snipecheck
    con = db.connect()
    seed(con)
    end = (datetime.now(config.TZ) + timedelta(hours=5)).isoformat(timespec="minutes")
    db.touch(con, "100000000009", "2023 Topps Chrome Jordan Walker Gold /50", is_auction=True,
             price=1.0, shipping=0.0, seller="bigconsign")
    db.touch(con, "100000000010", "2023 Topps Chrome Jordan Walker Blue /150", is_auction=True,
             price=1.0, shipping=0.0, seller="attic")
    con.commit()
    snipes = [{"item_id": "100000000009", "title": "2023 Topps Chrome Jordan Walker Gold /50", "max_bid": 5.05,
               "end_time": end, "status": "SCHEDULED"},
              {"item_id": "100000000010", "title": "2023 Topps Chrome Jordan Walker Blue /150", "max_bid": 5.05,
               "end_time": end, "status": "SCHEDULED"}]
    found = snipecheck.doubts(con, snipes, [])
    assert [d["kind"] for d in found[0]] == ["hot_seller"] and "~140%" in found[0][0]["text"]
    assert found[1] == []
    assert "hot_seller" not in snipecheck.SERIOUS            # advice, not a phone alert
    md = "\n".join(report.closes_section(con))
    assert "## Where steals happen" in md and "bigconsign ~140% (20)" in md and "Daytime endings 75%" in md
    bundle = reflect.window(con, time.time() - 7 * DAY, time.time() + 1)
    assert bundle["closes"]["groups"]["consignor"]["share"] == 1.4
    assert "bigconsign" not in json.dumps(bundle)            # no seller names reach the review
