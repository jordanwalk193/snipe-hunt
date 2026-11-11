import json
import subprocess
from datetime import datetime, timedelta, timezone

from snipehunt import authcheck, config, db, notify

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)


def cli_says(monkeypatch, data: dict | None):
    """The `claude auth status` reply, or a CLI that isn't there at all."""
    def fake_run(cmd, **kwargs):
        assert cmd[1:] == ["auth", "status"]
        if data is None:
            raise OSError("no such file")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
    monkeypatch.setattr(subprocess, "run", fake_run)


def alerts_on(monkeypatch, sent: list):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example.invalid/test")
    monkeypatch.setattr(notify, "send", lambda title, msg, **kw: sent.append((title, msg)))


def test_days_left_come_from_the_cli(monkeypatch):
    ends = (NOW + timedelta(days=27, hours=12)).isoformat().replace("+00:00", "Z")
    cli_says(monkeypatch, {"loggedIn": True, "loginExpiresAt": ends})
    st = authcheck.status(NOW)
    assert st["logged_in"] is True and st["days_left"] == 27.5 and not st["token"]
    assert authcheck.describe(st) == "signed in, 28 days left"


def test_a_long_lived_token_or_an_api_key_needs_no_check(monkeypatch):
    def never(*a, **k):
        raise AssertionError("asked the CLI")
    monkeypatch.setattr(subprocess, "run", never)
    for key in (authcheck.TOKEN_ENV, "ANTHROPIC_API_KEY"):
        monkeypatch.setitem(config.load(), key, "made-up-value")
        st = authcheck.status(NOW)
        assert st["token"] and st["logged_in"]
        assert authcheck.describe(st) == "long-lived token or API key"
        monkeypatch.delitem(config.load(), key)


def test_a_missing_cli_is_reported_not_raised(monkeypatch):
    cli_says(monkeypatch, None)
    st = authcheck.status(NOW)
    assert st["logged_in"] is None and st["error"] == "OSError"
    assert authcheck.describe(st).startswith("could not check")


def test_the_daily_check_warns_once_a_day_when_the_login_is_about_to_end(monkeypatch):
    sent = []
    alerts_on(monkeypatch, sent)
    ends = (NOW + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    cli_says(monkeypatch, {"loggedIn": True, "loginExpiresAt": ends})
    con = db.connect()
    assert authcheck.check(con, NOW) == "signed in, 3 days left"
    assert authcheck.check(con, NOW + timedelta(hours=2)) == "signed in, 3 days left"
    assert len(sent) == 1
    assert "ends in 3 day(s)" in sent[0][0] and "setup-token" in sent[0][1]
    authcheck.check(con, NOW + timedelta(days=1))   # the next day: again
    assert len(sent) == 2
    assert authcheck.cached(con)["days_left"] == 2.0   # and the dashboard sees the last reading


def test_a_lapsed_login_says_so_and_a_healthy_one_is_quiet(monkeypatch):
    sent = []
    alerts_on(monkeypatch, sent)
    con = db.connect()
    cli_says(monkeypatch, {"loggedIn": False})
    assert authcheck.check(con, NOW) == "not signed in"
    assert [t for t, _ in sent] == ["Snipe Hunt: Claude login has lapsed, nothing is being scored"]
    ends = (NOW + timedelta(days=20)).isoformat().replace("+00:00", "Z")
    cli_says(monkeypatch, {"loggedIn": True, "loginExpiresAt": ends})
    assert authcheck.check(con, NOW + timedelta(days=1)) == "signed in, 19 days left"
    assert len(sent) == 1


def test_a_dry_run_or_a_bot_with_no_alerts_sends_nothing(monkeypatch):
    def never(*a, **k):
        raise AssertionError("sent")
    monkeypatch.setattr(notify, "send", never)
    cli_says(monkeypatch, {"loggedIn": False})
    assert authcheck.check(db.connect(), NOW) == "not signed in"   # nowhere to send it
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example.invalid/test")
    assert authcheck.check(db.connect(), NOW, dry_run=True) == "not signed in"


def test_the_dashboard_shows_the_last_reading(monkeypatch):
    from snipehunt import snapshot
    con = db.connect()
    assert snapshot.snapshot(con)["status"]["claude_login"] is None   # before the first read
    cli_says(monkeypatch, {"loggedIn": True})
    authcheck.refresh(con)
    con.commit()
    assert snapshot.snapshot(con)["status"]["claude_login"]["logged_in"] is True
