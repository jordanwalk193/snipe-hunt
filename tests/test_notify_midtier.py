"""The mid-tier alert block, and its quiet hours."""
import json
from datetime import datetime

from snipehunt import config, db, midtier, notify

DAY = datetime(2026, 10, 18, 15, 0, tzinfo=config.TZ)
NIGHT = datetime(2026, 10, 18, 23, 30, tzinfo=config.TZ)
SIGNALS = [{"name": "typo_name", "weight": 3, "positive": True, "why": '"Jordon"'},
           {"name": "hidden_parallel", "weight": 2, "positive": True,
            "why": "/150 in specifics, not title"},
           {"name": "odd_end_time", "weight": 1, "positive": True, "why": "Tue 3 AM ET"}]


def lane_pick(item_id="900000000001", **kw):
    p = {"item_id": item_id, "card": "2021 Bowman Chrome Jordon Walker Atomic Refractor",
         "title": "2021 Bowman Chrome Jordon Walker Atomic Refractor", "tier": "green",
         "max": 35.35, "deal": 4, "price": 12.0, "shipping": 4.99, "total": 16.99,
         "is_auction": True, "end_time": "2026-10-20T03:12-04:00",
         "url": "https://www.ebay.com/itm/900000000001", "image": None,
         "midtier": {"alert": True, "signals": SIGNALS, "value": 62.0, "ship": 4.99,
                     "comps_n": 3, "rec_max": 32.21}}
    p.update(kw)
    return p


EXPECTED = """🎯 MIDTIER ★★★★ 2021 Bowman Chrome Jordon Walker Atomic Refractor
900000000001
Max: 35.35  (value ~$62 from 3 sold comps; ship $4.99)
Ends: Tue 3:12 AM ET / 12:12 AM PT
Why: typo_name("Jordon"), hidden_parallel(/150 in specifics, not title), odd_end_time(Tue 3 AM ET)
Watch-outs: none
https://www.ebay.com/itm/900000000001"""


def test_the_block_reads_exactly_as_documented():
    assert notify.format_midtier(lane_pick()) == EXPECTED


def test_item_number_is_alone_on_line_two_and_bin_shows_its_price():
    lines = notify.format_midtier(lane_pick(
        is_auction=False, price=28.0, max=40.0, best_offer=True,
        midtier={"alert": True, "signals": SIGNALS[:1], "value": 70.0, "ship": 4.99,
                 "comps_n": 0, "bin_ok": 37.01, "offer_idea": 31.31})).split("\n")
    assert lines[1] == "900000000001"
    assert lines[2] == "BIN ok ≤ $37.01  (value ~$70 estimate, no sold comps; ship $4.99)"
    assert lines[3].startswith("BIN $28.00 + $4.99 ship  · offer idea: $31.31")


def test_negative_signals_are_watch_outs():
    bad = lane_pick(midtier={"alert": True, "value": 62, "ship": 1, "comps_n": 1, "signals": SIGNALS + [
        {"name": "facsimile_guard", "weight": -5, "positive": False,
         "why": "Platinum Anniversary base cards carry a printed signature"}]})
    assert ("Watch-outs: Platinum Anniversary base cards carry a printed signature"
            in notify.format_midtier(bad))
    assert "facsimile_guard" not in notify.format_midtier(bad).split("Why:")[1].split("\n")[0]


def test_quiet_hours_hold_lane_alerts_until_morning(monkeypatch):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    con = db.connect()
    p = lane_pick(end_time="2026-10-21T21:00-04:00")
    db.touch(con, p["item_id"], p["title"], True, None, 4.99, 12.0, "made-up")   # as run() does
    assert midtier.deliver(con, [p], NIGHT) == 0 and sent == []
    assert [h["item_id"] for h in json.loads(db.kv_get(con, midtier.PENDING_KEY))] == [p["item_id"]]
    assert midtier.deliver(con, [], DAY) == 1           # morning: the held one goes out
    title, body = sent[0][:2]
    assert title == "Snipe Hunt: 1 midtier steal" and body.split("\n")[1] == "900000000001"
    assert midtier.deliver(con, [p], DAY) == 0           # never twice


def test_several_alerts_are_blocks_with_a_blank_line_between(monkeypatch):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    midtier.deliver(db.connect(), [lane_pick(), lane_pick("900000000002")], DAY)
    body = sent[0][1]
    assert body.count("🎯 MIDTIER") == 2 and "\n\n🎯 MIDTIER" in body


def test_only_picks_the_lane_decided_to_alert_go_out(monkeypatch):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)
    no = lane_pick(midtier={"alert": False, "signals": SIGNALS, "reason": "signals not enough"})
    assert midtier.deliver(db.connect(), [no], DAY) == 0


def test_daily_summary_counts():
    con = db.connect()
    day = "2026-10-17"
    for key, n in {"midtier_searched": 412, "midtier_enriched": 37, "midtier_flagged": 6,
                   "midtier_alerted": 2, "ebay_lane:midtier": 384,
                   "ebay_lane:midtier-item": 37}.items():
        db.bump(con, key, n, day)
    assert midtier.summary(con, day) == (
        "Midtier 2026-10-17: 412 listings searched, 37 read in full, 6 flagged, 2 alerted · "
        "eBay calls 384 searches + 37 item reads")
