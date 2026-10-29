import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from snipehunt import api, config, db, gixen, notify, settings, snapshot

NOW = datetime(2026, 10, 12, 18, 0, tzinfo=config.TZ)


@pytest.fixture
def con(monkeypatch):
    monkeypatch.setitem(config.load(), "GIXEN_USER", "")   # never call Gixen from tests
    return db.connect()


def add(con, item_id, seen_ago=60, **kw):
    pick = {"item_id": item_id, "card": f"Card {item_id}", "tier": "green",
            "price": 5.0, "shipping": 1.0, "total": 6.0, "max": 8.08,
            "is_auction": True, "end_time": "2026-10-12T21:00-04:00",
            "seller": "bigseller", "url": f"https://www.ebay.com/itm/{item_id}"}
    pick.update(kw)
    db.touch(con, item_id, pick["card"])
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?",
                (int(NOW.timestamp()) - seen_ago, item_id))
    db.save_pick(con, pick)
    return pick


def test_live_picks_drop_ended_auctions_and_vanished_listings(con):
    add(con, "1")
    add(con, "2", end_time="2026-10-12T17:00-04:00")       # already ended
    add(con, "3", seen_ago=3 * 3600)                        # gone from eBay
    add(con, "4", is_auction=False, end_time=None)
    assert sorted(p["item_id"] for p in snapshot.live_picks(con, NOW)) == ["1", "4"]


def test_snapshot_lists_every_pick_best_first(con):
    add(con, "1", end_time="2026-10-13T09:00-04:00")
    add(con, "2", tier="fire")
    add(con, "3", is_auction=False, end_time=None, tier="yellow")
    snap = snapshot.snapshot(con, NOW)
    assert [p["item_id"] for p in snap["picks"]] == ["2", "1", "3"]
    assert (snap["status"]["auctions"], snap["status"]["buy_now"]) == (2, 1)
    assert snap["gixen"] is None and snap["settings"]["quiet_start"] == "23:00"
    json.dumps(snap)


def test_need_snipe_follows_the_window_setting(con):
    add(con, "1", end_time="2026-10-12T21:00-04:00")
    add(con, "2", end_time="2026-10-14T09:00-04:00")
    add(con, "3", end_time="2026-10-12T20:00-04:00", tier="yellow")
    assert snapshot.snapshot(con, NOW)["status"]["need_snipe"] == ["1"]
    settings.update(con, {"need_snipe_hours": 48})
    assert snapshot.snapshot(con, NOW)["status"]["need_snipe"] == ["1", "2"]
    picks = snapshot.live_picks(con, NOW)
    picks[0]["snipe"] = 5.05   # covered in Gixen
    assert snapshot.need_snipe(picks, NOW, 48, True) == ["2"]
    assert snapshot.need_snipe(picks, NOW, 48, False) == ["1", "2"]


def test_status_reports_failing_job(con):
    db.kv_set(con, "failing:daily", "1")
    state = snapshot.status(con, [], NOW)
    assert state["state"] == "failing" and state["failing"] == ["daily"]


GIXEN_PAGE = """<div class="main-table-mobile"><table>
<tr class=d3> <td><a target="_blank" href="http://cgi.ebay.com/ws/eBayISAPI.dll?ViewItem&amp;item=123456789011">123456789011</a></td> <td>Offset (main): 6</td></tr>
<tr class=d3> <td colspan=2>2026 CHROME SAPPHIRE BLACK /10 <i>(by <a target=_blank href="http://www.ebay.com/usr/consignco/">consignco</a>)</i><br /></tr>
<tr class=d3> <td colspan=2>Time to end: 1 h, 52 m, 19 s</td></tr>
<tr class=d3> <td>Max bid: 20.20 USD</td> <td>Current bid: 44.00 USD</td> </tr>
<tr class=d3> <td>Status (main): </td><td>SCHEDULED</td> </tr>
<tr class=d3> <td><a target="_blank" href="http://cgi.ebay.com/ws/eBayISAPI.dll?ViewItem&amp;item=123456789022">123456789022</a></td></tr>
<tr class=d3> <td colspan=2>2023 Chrome Gold /50 PSA 10 <i>(by <a href="x">someshop</a>)</i></tr>
<tr class=d3> <td colspan=2>Time to end: ENDED</td></tr>
<tr class=d3> <td>Max bid: 15.15 USD</td> <td>Current bid: 39.00 USD</td> </tr>
<tr class=d3> <td>Status (main): </td><td>BID UNDER ASKING PRICE</td> </tr>
</table></div><div class="main-table-desktop">ViewItem&amp;item=123456789011">again</div>"""


def test_gixen_page_parses_scheduled_and_ended_snipes():
    live, ended = gixen.parse(GIXEN_PAGE, NOW)
    assert live["item_id"] == "123456789011" and live["seller"] == "consignco"
    assert (live["max_bid"], live["current_bid"]) == (20.20, 44.00)
    assert live["status"] == "SCHEDULED"
    assert live["end_time"] == "2026-10-12T19:52-04:00"
    assert ended["end_time"] is None and ended["status"] == "BID UNDER ASKING PRICE"
    assert gixen.parse("<html>please log in</html>", NOW) == []


def test_gixen_view_flags_outbid_snipes(con):
    covered = add(con, "123456789011", tier="fire")
    covered["snipe"] = 20.20
    view = snapshot.gixen_view(gixen.parse(GIXEN_PAGE, NOW), None, NOW.timestamp(),
                               [covered], NOW)
    assert view["available"] and view["outbid_count"] == 1 and view["committed"] == 0
    assert view["snipes"][0]["outbid"] and view["snipes"][0]["bot_max"] == 8.08
    assert snapshot.gixen_view([], "Signed out", 0, [], NOW)["available"] is False


def test_settings_validate_and_survive_bad_stored_values(con):
    saved = settings.update(con, {"quiet_start": "22:30", "sellers": "a_b, c.d ,a_b",
                                  "yellow_max_total": 3, "alert_green": False})
    assert saved["quiet_start"] == "22:30" and saved["sellers"] == ["a_b", "c.d"]
    assert saved["yellow_max_total"] == 3.0 and saved["alert_green"] is False
    for bad in ({"quiet_end": "25:00"}, {"sellers": ["x}|{y"]}, {"nope": 1},
                {"monthly_budget": -1}, {"alerts_enabled": "yes"}, {}):
        with pytest.raises(settings.SettingsError):
            settings.update(con, bad)
    with pytest.raises(settings.SettingsError):   # one bad value saves nothing
        settings.update(con, {"urgent_hours": 5, "need_snipe_hours": 0})
    assert settings.load(con)["urgent_hours"] == 2.0
    db.kv_set(con, settings.KEY, json.dumps({"quiet_end": "nonsense", "urgent_hours": 4}))
    assert settings.load(con)["quiet_end"] == "07:00"
    assert settings.load(con)["urgent_hours"] == 4.0


def test_alert_rules_follow_settings(con):
    night = datetime(2026, 10, 11, 22, 45, tzinfo=config.TZ)
    green = {"tier": "green", "total": 40}
    assert notify.worth_alerting(green) and not notify.is_quiet(night)
    settings.update(con, {"alert_green": False, "quiet_start": "22:30"})
    assert not notify.worth_alerting(green) and notify.is_quiet(night)
    settings.update(con, {"quiet_hours": False})
    assert not notify.is_quiet(night)
    settings.update(con, {"alerts_enabled": False})
    assert notify.deliver(con, [{"item_id": "1", "tier": "fire", "total": 5}]) == 0


def test_searches_follow_settings(con):
    from snipehunt import ebay_search
    settings.update(con, {"sellers": ["only_one"], "extra_searches": ["walker 1st bowman"]})
    todo = ebay_search.queries()
    assert todo[-1][2].endswith("sellers:{only_one}")
    assert ("extra-walker-1st-bowman", "walker 1st bowman") == todo[-2][:2]
    settings.update(con, {"sellers": []})
    assert all(label != "sellers" for label, *_ in ebay_search.queries())


@pytest.fixture
def server(con, monkeypatch):
    monkeypatch.setattr(api.Handler, "token", "s3cret")
    monkeypatch.setitem(api._cache, "at", 0.0)
    httpd = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, body=None, token="s3cret"):
    req = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_api_needs_the_token_and_returns_a_fresh_snapshot(con, server):
    add(con, "111111111111")
    con.commit()
    assert call(server + "/api/snapshot", token="wrong")[0] == 401
    assert call(server + "/api/action", {"action": "skip"}, token="")[0] == 401
    code, snap = call(server + "/api/snapshot")
    assert code == 200 and len(snap["picks"]) >= 0 and snap["api"] == 1
    code, out = call(server + "/api/action", {"action": "skip", "item_id": "111111111111"})
    assert code == 200 and out["ok"] and out["snapshot"]["status"]["skipped"] == 1
    code, out = call(server + "/api/action", {"action": "buy"})
    assert code == 200 and out["ok"] is False
    code, out = call(server + "/api/settings", {"monthly_budget": 150})
    assert code == 200 and out["snapshot"]["settings"]["monthly_budget"] == 150.0
    code, out = call(server + "/api/settings", {"monthly_budget": "lots"})
    assert code == 400 and "number" in out["message"]
    assert call(server + "/api/snapshot")[1]["last_action"]["ok"] is False
    assert call(server + "/api/nothing")[0] == 404


def test_only_exposed_settings_are_shown_and_changeable(con, monkeypatch):
    monkeypatch.setitem(config.load(), "API_SETTINGS", "monthly_budget, need_snipe_hours")
    assert sorted(snapshot.snapshot(con, NOW)["settings"]) == ["monthly_budget", "need_snipe_hours"]
    settings.update(con, {"monthly_budget": 100})
    with pytest.raises(settings.SettingsError):
        settings.update(con, {"alerts_enabled": False})


def test_buttons_hide_picks_and_file_cards(con):
    from snipehunt import actions
    add(con, "111111111111")
    add(con, "222222222222")
    assert actions.handle(con, {"action": "skip", "item_id": "111111111111"})[0]
    assert [p["item_id"] for p in snapshot.live_picks(con, NOW)] == ["222222222222"]
    assert actions.handle(con, {"action": "skip", "item_id": "1; rm -rf"})[0] is False
    assert actions.handle(con, {"action": "buy"})[0] is False
    actions.handle(con, {"action": "unskip_all"})
    assert len(snapshot.live_picks(con, NOW)) == 2
    ok, message = actions.handle(con, {"action": "filed", "ids": ["1", "2"]})
    assert ok and message.startswith("Filed: 2020 Bowman Draft Chrome")   # only #2 was Delivered
    assert ",In binder,12.12," in config.INVENTORY.read_text()


def test_scheduler_knows_what_is_due(con):
    from snipehunt import scheduler
    now = datetime(2026, 10, 11, 18, 50, tzinfo=config.TZ)   # a Sunday
    assert scheduler.last_due("hourly", now).hour == 18
    assert scheduler.last_due("daily", now).strftime("%d %H:%M") == "11 09:00"
    assert scheduler.last_due("weekly", now).strftime("%a %H:%M") == "Sun 18:45"
    assert scheduler.last_due("weekly", now.replace(hour=18, minute=0)).day == 4
    assert scheduler.due_jobs(con, now) == ["hourly", "weekly"]   # daily is 9h50m late
    for job in ("hourly", "weekly"):
        db.kv_set(con, f"started:{job}", str(now.timestamp()))
    assert scheduler.due_jobs(con, now) == []
    assert scheduler.trigger("hourly") and not scheduler.trigger("hourly")
    scheduler._wanted.clear()


def test_profile_defaults_come_from_the_name():
    from snipehunt import profile
    p = profile.parse('[player]\nname = "Paul Skenes"\n')
    assert "skens" in p.last_names and "pual" in p.first_names and len(p.typo_searches) == 6
    assert p.classify("2024 Topps Chrome Paul Skenes RC") == "unclear"   # no team words set
    assert p.classify("2024 Topps Pual Skenes baseball") == "yes"
    assert p.classify("Paul Goldschmidt 2024 Topps") == "no"
    assert p.binder_page("2024") == "unsorted"
    with pytest.raises(profile.ProfileError):
        profile.parse('[player]\nname = " "\n')
    # one name is enough for a Pokémon, or a player known by one name
    assert profile.parse('[player]\nname = "Cher"\n').classify("Cher card baseball") == "yes"


def test_scheduler_tick_runs_one_job_at_a_time(con, monkeypatch):
    from snipehunt import scheduler
    started = []

    class FakeJob:
        done = False

        def __init__(self, cmd):
            started.append(cmd[-1])

        def poll(self):
            return 0 if FakeJob.done else None
    monkeypatch.setattr(scheduler.subprocess, "Popen", FakeJob)
    monkeypatch.setattr(scheduler, "_proc", None)
    now = datetime(2026, 10, 11, 18, 50, tzinfo=config.TZ)        # hourly and weekly are due
    assert scheduler.tick(con, now) == "hourly" and scheduler.running("hourly")
    assert scheduler.tick(con, now) is None                       # busy: nothing else starts
    assert snapshot.snapshot(con, NOW)["status"]["searching"] is True
    FakeJob.done = True
    assert scheduler.tick(con, now) == "weekly"                   # the next due job
    assert scheduler.trigger("hourly") is True                    # "Search now" button
    assert scheduler.tick(con, now) == "hourly"                   # requested job goes first
    assert scheduler.tick(con, now) is None and started == ["hourly", "weekly", "hourly"]


def test_demo_mode_serves_made_up_data_and_changes_nothing(con, server, monkeypatch):
    from snipehunt import demo
    real = set(snapshot.snapshot(con, NOW))
    fake = demo.build(NOW)
    assert real <= set(fake) and set(snapshot.snapshot(con, NOW)["status"]) <= set(fake["status"])
    add(con, "111111111111")
    con.commit()
    real_pick = snapshot.snapshot(con, NOW)["picks"][0]
    assert set(real_pick) == set(fake["picks"][0])
    monkeypatch.setitem(config.load(), "SNIPEHUNT_DEMO", "1")
    code, snap = call(server + "/api/snapshot")
    assert code == 200 and snap["demo"] is True and len(snap["picks"]) == 10   # 9 plus the demo jackpot
    code, out = call(server + "/api/action", {"action": "skip", "item_id": "111111111111"})
    assert out["ok"] and "Demo mode" in out["message"]
    assert snapshot.skipped_ids(con) == []


def test_last_report_is_the_newest_dated_weekly_report(tmp_path, monkeypatch):
    for name in ("2026-10-18.md", "2026-10-25.md", "reflect-2026-10-26.md",
                 "notes-2026-10-11.md"):
        (tmp_path / name).write_text("x")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    assert snapshot.latest_report() == "2026-10-25.md"
    for p in tmp_path.glob("2026-*.md"):
        p.unlink()
    assert snapshot.latest_report() is None


def test_snapshot_says_whether_the_bot_has_a_gixen_account(con, monkeypatch):
    assert snapshot.snapshot(con, NOW)["gixen_account"] is False   # the fixture blanks it
    monkeypatch.setitem(config.load(), "GIXEN_USER", "someone")
    monkeypatch.setattr(gixen, "snipes", lambda con: ([], None, NOW.timestamp()))
    snap = snapshot.snapshot(con, NOW)
    assert snap["gixen_account"] is True and snap["gixen"]["available"] is True


def test_api_server_keeps_quiet_when_a_client_hangs_up(capsys):
    """Home Assistant restarting mid-answer is a broken pipe, not an error worth a traceback."""
    server = api.QuietServer.__new__(api.QuietServer)   # no socket needed for handle_error
    try:
        raise BrokenPipeError(32, "Broken pipe")
    except BrokenPipeError:
        server.handle_error(None, ("127.0.0.1", 1))
    try:
        raise ValueError("a real bug")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 1))
    err = capsys.readouterr().err
    assert "Broken pipe" not in err and "a real bug" in err
