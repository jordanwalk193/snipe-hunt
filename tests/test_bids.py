import time
from datetime import datetime, timedelta

from snipehunt import bids, config, db, scheduler, snapshot

NOW = datetime.now(config.TZ)


def auction(con, item_id, hours, tier="green"):
    db.save_pick(con, {"item_id": item_id, "card": "card", "tier": tier, "price": 1.0,
                       "shipping": 1.0, "max": 20.20, "value": 40.0, "is_auction": True,
                       "end_time": (NOW + timedelta(hours=hours)).isoformat(timespec="minutes")})
    db.touch(con, item_id, "card", True, None, 1.0, 1.0, "s")
    # the last search was 40 minutes ago
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (int(time.time()) - 2400, item_id))


class Client:
    def __init__(self):
        self.read = []

    def item(self, item_id, lane="item", fresh=False):
        assert fresh and lane == "bids"
        self.read.append(item_id)
        return {"currentBidPrice": {"value": "7.50", "currency": "USD"}, "bidCount": 4}


def test_only_good_picks_ending_soon_are_read_and_only_once_in_a_while():
    con = db.connect()
    auction(con, "1", 2)                 # due
    auction(con, "2", 9)                 # ends too late
    auction(con, "3", 1, tier="yellow")  # not a good pick
    c = Client()
    assert bids.refresh(con, c, NOW) == 1 and c.read == ["1"]
    (p,) = [p for p in snapshot.live_picks(con, NOW) if p["item_id"] == "1"]
    assert p["price"] == 7.5 and p["bids"] == 4
    assert bids.refresh(con, c, NOW) == 0          # read a moment ago: not again yet


def test_the_half_hour_job_runs_when_a_good_pick_ends_soon():
    con = db.connect()
    at = NOW.replace(minute=35)
    assert "lastcall" not in scheduler.due_jobs(con, at)
    auction(con, "1", 2)
    assert "lastcall" in scheduler.due_jobs(con, at)
