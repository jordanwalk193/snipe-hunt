"""Team mode on the dashboard and the phone: steals first, per player."""
import json
from datetime import datetime

import pytest

from snipehunt import config, db, notify, profile, snapshot

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=config.TZ)


@pytest.fixture
def con(monkeypatch):
    monkeypatch.setitem(config.load(), "GIXEN_USER", "")
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))
    return db.connect()


def add(con, item_id, **kw):
    pick = {"item_id": item_id, "card": f"Card {item_id}", "tier": "green", "price": 5.0,
            "shipping": 1.0, "total": 6.0, "max": 8.08, "is_auction": False, "end_time": None,
            "seller": "shop", "url": "", "value": 20.0, "deal": 4, "deal_pct": 40,
            "player": "Masyn Winn"}
    pick.update(kw)
    db.touch(con, item_id, pick["card"])
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (int(NOW.timestamp()) - 60, item_id))
    db.save_pick(con, pick)


def test_the_team_board_ranks_by_discount_and_skips_non_steals(con):
    add(con, "1", deal_pct=40)
    add(con, "2", deal_pct=70, deal=5, player="Jordan Walker")
    add(con, "3", deal=1, deal_pct=5)                       # a fair price: not on the board
    add(con, "4", deal_pct=80, deal=5, unverified=True)     # a guessed price: not on the board
    snap = snapshot.snapshot(con, NOW)
    team = snap["team"]
    assert [p["item_id"] for p in team["board"]] == ["2", "1"]
    assert team["board"][0]["player"] == "Jordan Walker"
    players = {g["player"]: g for g in team["players"]}
    assert players["Masyn Winn"]["picks"] == 3 and players["Jordan Walker"]["best"] == "2"
    assert players["Jordan Walker"]["saved"] == 14.0
    assert team["budget"] == {"budget": 5.0, "spent_today": 0.0, "reached": False}
    assert snap["profile"]["mode"] == "team" and "funnel" in team and "bundles" in team
    json.dumps(snap)


def test_a_player_bot_has_no_team_section(monkeypatch):
    monkeypatch.setitem(config.load(), "GIXEN_USER", "")
    snap = snapshot.snapshot(db.connect(), NOW)
    assert "team" not in snap and "mode" not in snap["profile"]


def test_team_alerts_name_the_player_and_lead_with_the_deepest_discount(con):
    a = {"item_id": "1", "card": "2024 Topps Chrome RC", "tier": "green", "price": 5.0,
         "shipping": 1.0, "deal_pct": 20, "player": "Masyn Winn", "max": 6.0}
    b = {**a, "item_id": "2", "deal_pct": 60, "player": "Jordan Walker", "tier": "yellow"}
    title, body, _, _ = notify.format_bundle([a, b])
    assert title == "Snipe Hunt: 2 Cardinals steals"
    assert body.index("Jordan Walker · 2024") < body.index("Masyn Winn · 2024")
