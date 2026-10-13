import json
import subprocess

from snipehunt import config, score


def test_scoring_inventory_keeps_only_what_identifies_a_card():
    full = config.INVENTORY.read_text()
    short = score.inventory_for_scoring()
    header = short.splitlines()[0].split(",")
    assert set(header) <= set(score.CARD_COLUMNS) and "set" in header
    assert "price_paid" not in header and "notes" not in header
    assert len(short.splitlines()) == len(full.splitlines())   # no card is dropped
    assert len(short) < len(full)


def test_an_inventory_in_another_layout_is_sent_whole():
    config.INVENTORY.write_text("what,where\nrookie card,binder page 1\n")
    assert score.inventory_for_scoring() == config.INVENTORY.read_text()


def test_claude_is_called_without_its_coding_prompt_or_a_saved_transcript(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"result": "{}", "total_cost_usd": 0.01}), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setitem(config.load(), "CLAUDE_EFFORT", "medium")
    assert score.ask_claude("hello") == "{}"
    cmd = seen["cmd"]
    assert "--no-session-persistence" in cmd
    assert "Today is" in cmd[cmd.index("--system-prompt") + 1]
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--tools") + 1] == "WebFetch,WebSearch"   # still read-only tools


def test_price_lookups_off_means_no_web_tools_and_says_so(monkeypatch):
    from snipehunt import db, settings
    from snipehunt.ebay_search import Listing
    calls = []
    config.PREFERENCES.write_text("Cheap and unique.\n")
    monkeypatch.setattr(score, "ask_claude", lambda prompt, tools=score.WEB_TOOLS:
                        calls.append((prompt, tools)) or '{"picks": []}')
    item = Listing(item_id="1", title="2023 Topps Chrome rookie", price=1.0, currency="USD",
                   shipping=1.0, buying_options=["AUCTION"], bid_count=0,
                   end_time="2026-10-20T00:00:00Z", seller="s", url="u", image=None)
    score.score([item])
    assert calls[-1][1] == score.WEB_TOOLS and "at most 8 lookups" in calls[-1][0]
    settings.update(db.connect(), {"price_lookups": 0})
    score.score([item])
    assert calls[-1][1] == "" and "no web access" in calls[-1][0]
