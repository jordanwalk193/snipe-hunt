import json
from datetime import datetime, timedelta, timezone

from snipehunt import config, db, lowball, notify
from snipehunt.ebay_search import Listing

NOW = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)   # noon Eastern


def auction(item_id, title, price, hours, shipping=1.0, bids=0, country="US", image="x.jpg"):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=["AUCTION"], bid_count=bids,
                   end_time=(NOW + timedelta(hours=hours)).astimezone(config.TZ).isoformat(),
                   seller="s", url=f"https://www.ebay.com/itm/{item_id}", image=image,
                   country=country)


def priced(con, item_id, value):
    db.save_pick(con, {"item_id": item_id, "value": value, "max_basis": "sales", "tier": "green"})


def test_dead_hours():
    et = config.TZ
    assert lowball.dead_hour(datetime(2026, 10, 20, 3, 30, tzinfo=et)) == "ends overnight"
    assert lowball.dead_hour(datetime(2026, 11, 26, 20, 0, tzinfo=et)) == "ends on Thanksgiving"
    assert lowball.dead_hour(datetime(2027, 2, 14, 20, 0, tzinfo=et)) == "ends during the Super Bowl"
    assert lowball.dead_hour(datetime(2026, 10, 20, 21, 0, tzinfo=et)) is None
    learned = {"15": {"n": 40, "median": 0.7}}
    assert "sales run cheap" in lowball.dead_hour(datetime(2026, 10, 20, 16, 0, tzinfo=et), learned)


def test_overlooked():
    assert lowball.overlooked(auction("1", "t", 0.99, 5, shipping=None)) == ["shipping not stated"]
    assert lowball.overlooked(auction("1", "t", 0.99, 5, shipping=6.0, country="CA", image=None)) == [
        "$6.00 shipping", "ships from CA", "no photo"]
    assert lowball.overlooked(auction("1", "t", 0.99, 5)) == []


def test_level_is_learned_from_how_auctions_ended():
    assert lowball.level([]) == lowball.LEVEL
    ended = [{"share": s / 20, "hour": 12} for s in range(1, 21)] * 2   # 0.05 .. 1.00, twice
    assert lowball.level(ended) == 0.25            # a fifth ended at or under 25%
    assert lowball.level([{"share": 0.01, "hour": 1}] * 30) == 0.20   # never below the floor


def test_targets_are_cheap_auctions_of_good_cards(monkeypatch):
    con = db.connect()
    title = "2021 Bowman Chrome Jordan Walker Green Refractor /99 #BCP-146"
    items = [auction("1", title, 0.99, 10), auction("2", title, 14.00, 10),   # too dear now
             auction("3", title, 0.99, 72),                                  # ends too late
             auction("4", "2023 Topps Jordan Walker #344 RC", 0.99, 10),     # worth $2
             auction("5", title, 0.99, 6, shipping=None, image=None)]
    for i in ("1", "2", "3", "5"):
        priced(con, i, 30.0)
    priced(con, "4", 2.0)
    db.kv_set(con, "skipped_picks", json.dumps([]))
    found = lowball.find(con, items, [], sniped={"5"}, now=NOW)
    ids = [t["item_id"] for t in found["targets"]]
    assert ids == ["5", "1"]                      # overlooked + same discount sorts first
    one = next(t for t in found["targets"] if t["item_id"] == "1")
    assert one["snipe"] == 9.09                   # 35% of $30 less $1 shipping, odd cents
    assert next(t for t in found["targets"] if t["item_id"] == "5")["sniped"] is True
    assert found["level"] == lowball.LEVEL and not found["learned"]


def test_settle_records_an_auction_that_closed_unsold():
    con = db.connect()
    end = NOW.timestamp() - 7200
    db.kv_set(con, lowball.LOG_KEY, json.dumps({"9": {"value": 20.0, "snipe": 5.05, "end_ts": end}}))
    con.execute("INSERT INTO seen (item_id, title, first_seen, last_seen, price, shipping, closed_at) "
                "VALUES ('9', 't', 0, 0, 0.99, 1.0, 1)")
    lowball._settle(con, NOW.timestamp())
    record = json.loads(db.kv_get(con, lowball.RECORD_KEY))
    assert record[0]["item_id"] == "9" and round(record[0]["share"], 3) == 0.1
    assert json.loads(db.kv_get(con, lowball.LOG_KEY)) == {}


class Client:
    def __init__(self, bids, price):
        self.bids, self.price = bids, price

    def item(self, item_id, lane="item", fresh=False):
        assert fresh, "last call needs this minute's bids, never the day's cached read"
        return {"currentBidPrice": {"value": str(self.price[item_id]), "currency": "USD"},
                "bidCount": self.bids[item_id]}


def test_last_call_sends_the_ones_still_cheap_once(monkeypatch):
    con = db.connect()
    now = NOW.astimezone(config.TZ)
    target = {"title": "Jordan Walker auto", "url": "u", "image": None, "shipping": 1.0,
              "value": 40.0, "snipe": 13.13, "dead": None,
              "end_time": (now + timedelta(hours=2)).isoformat()}
    db.kv_set(con, lowball.KEY, json.dumps({"targets": [
        dict(target, item_id="1"), dict(target, item_id="2"), dict(target, item_id="3"),
        dict(target, item_id="4", end_time=(now + timedelta(hours=8)).isoformat())]}))
    sent = []
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: False)
    monkeypatch.setattr(notify, "send", lambda title, msg, **kw: sent.append((title, msg)))
    client = Client({"1": 0, "2": 4, "3": 1}, {"1": 1.25, "2": 2.0, "3": 15.0})
    hits = lowball.last_call(con, client, now=now)
    assert [h["item_id"] for h in hits] == ["1"]          # 2 has bids, 3 is no longer cheap
    assert sent[0][0] == "🎯 Last call: 1 cheap auction" and "Snipe: 13.13" in sent[0][1]
    assert lowball.last_call(con, client, now=now) == []   # sent once


def test_last_call_is_only_scheduled_when_a_target_ends_soon():
    from snipehunt import scheduler
    con = db.connect()
    now = NOW.astimezone(config.TZ).replace(minute=35)
    assert "lastcall" not in scheduler.due_jobs(con, now)
    db.kv_set(con, lowball.KEY, json.dumps({"targets": [
        {"item_id": "1", "value": 40.0, "end_time": (now + timedelta(hours=2)).isoformat()}]}))
    assert "lastcall" in scheduler.due_jobs(con, now)


def test_snapshot_leaves_out_ended_targets():
    from snipehunt import snapshot
    con = db.connect()
    now = NOW.astimezone(config.TZ)
    db.kv_set(con, lowball.KEY, json.dumps({"level": 0.35, "targets": [
        {"item_id": "1", "end_time": (now - timedelta(minutes=5)).isoformat()},
        {"item_id": "2", "end_time": (now + timedelta(hours=5)).isoformat()}]}))
    assert [t["item_id"] for t in snapshot.lowball_view(con, now)["targets"]] == ["2"]


def test_a_snipe_under_a_dollar_is_no_snipe():
    assert lowball.snipe_for(4.0, 1.0, 0.35) is None
    assert lowball.snipe_for(30.0, 1.0, 0.35) == 9.09
