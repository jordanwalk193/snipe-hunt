from datetime import datetime

import pytest

from snipehunt import config, db, jobs, notify, ops, report

NOW = datetime(2026, 10, 11, 18, 45, tzinfo=config.TZ)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SEEN_DB", tmp_path / "seen.db")
    monkeypatch.setattr(config, "HEALTHZ", tmp_path / "healthz")
    monkeypatch.setitem(config.load(), "KUMA_PUSH_URL", "")
    return db.connect()


def pick(**kw):
    base = {"item_id": "111111111111", "card": "2022 Bowman Chrome Auto", "tier": "fire",
            "max": 25.25, "max_basis": "sales", "price": 9.5, "total": 10.75,
            "is_auction": True, "end_time": "2026-10-13T21:00-04:00",
            "seller": "bigseller", "url": "https://www.ebay.com/itm/1"}
    base.update(kw)
    return base


def test_binder_page_follows_card_year():
    assert report.binder_page({"year": "2023", "front_page": ""}) == "pp8–11"
    assert report.binder_page({"year": "2023", "front_page": "Y"}) == "front page"
    assert report.binder_page({"year": "2031"}) == "p20 spare"


def test_outbid_then_won_is_not_a_loss():
    events = [{"type": "lost", "item_id": "1", "title": "a"},
              {"type": "won", "item_id": "1", "title": "a"},
              {"type": "lost", "item_id": "2", "title": "b"},
              {"type": "lost", "item_id": "2", "title": "b"}]
    wins, losses = report.wins_and_losses(events)
    assert [e["item_id"] for e in wins] == ["1"]
    assert [e["item_id"] for e in losses] == ["2"]


def test_unpaid_advice():
    rows = [{"id": "1", "status": "Won", "seller": "bigseller"},
            {"id": "2", "status": "Won", "seller": "solo"},
            {"id": "3", "status": "Awaiting payment", "seller": "pair"},
            {"id": "4", "status": "Won", "seller": "pair"},
            {"id": "5", "status": "In binder", "seller": "pair"}]
    groups = {g["seller"]: g for g in report.unpaid_by_seller(rows, [pick()])}
    assert groups["bigseller"]["advice"].startswith("hold")
    assert groups["pair"]["advice"].startswith("request a combined invoice")
    assert len(groups["pair"]["cards"]) == 2
    assert groups["solo"]["advice"] == "pay now"


def test_upcoming_drops_ended_auctions():
    picks = [pick(), pick(item_id="2", end_time="2026-10-10T21:00-04:00"),
             pick(item_id="3", tier="yellow", is_auction=False, end_time=None)]
    assert [p["item_id"] for p in report.upcoming(picks, NOW)] == ["111111111111", "3"]


def test_build_report(con):
    db.save_pick(con, pick())
    db.log_mail_events(con, [
        {"type": "won", "is_player": True, "item_id": "9", "title": "Walker auto",
         "price": 6.06, "seller": "x", "date": "2026-10-09", "source": "gixen"},
        {"type": "lost", "is_player": True, "item_id": "8", "title": "Walker patch",
         "date": "2026-10-08"},
        {"type": "won", "is_player": False, "item_id": "7", "title": "other",
         "date": "2026-10-08"}])
    db.save_pick(con, pick())   # same listing twice stays one row
    text, summary = report.build(con, NOW, releases=[])
    assert "1 won, 1 lost" in text and "Walker auto · $6.06 · x · Gixen" in text
    assert "**Max: 25.25** (sales) · item `111111111111`" in text
    assert "No new Walker cards found" in text
    assert summary.startswith("1 won, 1 lost")


def test_failed_run_alerts_once(con, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg, **kw: sent.append(title))
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")

    def boom():
        raise RuntimeError("eBay 500")

    assert ops.guarded("hourly", boom) == 1
    assert ops.guarded("hourly", boom) == 1
    assert sent == ["Snipe Hunt: hourly run failed"]
    assert ops.guarded("hourly", lambda: "fine") == 0
    assert config.HEALTHZ.read_text().split()[1:] == ["hourly", "ok"]
    assert ops.guarded("hourly", boom) == 1
    assert len(sent) == 2


def test_hourly_needs_ebay_keys(con, monkeypatch):
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "")
    with pytest.raises(config.ConfigError):
        jobs.hourly(dry_run=True)


def test_optional_pieces_can_be_left_out(con, monkeypatch):
    """No phone and no Gmail: jobs still succeed and nothing tries to send."""
    monkeypatch.setattr(notify, "send", lambda *a, **k: pytest.fail("nothing to send to"))
    assert not notify.configured()
    assert notify.deliver(con, [pick()]) == 0
    assert ops.guarded("hourly", lambda: 1 / 0) == 1      # fails quietly, no alert attempt
    monkeypatch.setattr(jobs.limited_drops, "check_drops", lambda verbose=False: [])
    assert "email sync is off" in jobs.daily()
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    assert notify.configured()


def test_a_run_that_fails_while_scoring_is_still_recorded(con, monkeypatch):
    """The job's own unfinished database write must not block the failure note."""
    from snipehunt.ebay_search import Listing
    item = Listing("123456789012", "2023 Topps Jordan Walker RC", 1.0, "USD", 1.0,
                   ["AUCTION"], 0, "2026-10-13T21:00-04:00", "s", "u", None)
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: [item])

    def no_claude(*args, **kwargs):
        raise RuntimeError("Claude is down")
    monkeypatch.setattr(jobs.score, "score", no_claude)
    assert ops.guarded("hourly", jobs.hourly) == 1
    assert db.kv_get(con, "failing:hourly") == "1"
    assert con.execute("SELECT count(*) FROM seen").fetchone()[0] == 1   # still tracked
