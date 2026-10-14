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


def test_an_edge_is_kept_only_when_the_numbers_back_it():
    from snipehunt.ebay_search import Listing

    def judged(edge, value, auction=False, price=10.0):
        item = Listing(item_id="1", title="2023 Topps Chrome rookie auto", price=price,
                       currency="USD", shipping=1.0,
                       buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                       end_time="2026-10-20T00:00:00Z" if auction else None, seller="s",
                       url="u", image=None)
        return score._pick_dict(item, {"item_id": "1", "tier": "fire", "max": 12.12,
                                       "value": value, "edge": edge})["edge"]

    assert judged("mispriced", 40.0) == "mispriced"          # $11 delivered on a $40 card
    assert judged("mispriced", 13.0) is None                 # about the going rate: no edge
    assert judged("hidden", 40.0) == "hidden"
    assert judged("sleeper", 40.0, auction=True, price=1.0) == "sleeper"
    assert judged("mispriced", 40.0, auction=True, price=1.0) is None   # only a fixed price
    assert judged("bargain", 40.0) is None and judged("", 40.0) is None
    assert judged("hidden", None) is None                    # no going rate, no claim
    assert '"edge"' in score.PROMPT and "slipped through the cracks" in score.PROMPT


def test_scoring_learns_taste_from_what_was_hidden_and_what_was_sniped(monkeypatch):
    from snipehunt import actions, db, jobs
    from snipehunt.ebay_search import Listing
    con = db.connect()
    item = Listing("9", "2023 Topps Chrome Jordan Walker Refractor #45", 1.0, "USD", 1.0,
                   ["AUCTION"], 0, None, "s", "u", None)
    plain = score.build_prompt([item])
    assert "passed on" not in plain and "sniped" not in plain        # nothing to say: no cost
    db.save_pick(con, {"item_id": "111111111111", "card": "2024 Topps Holiday Relic",
                       "title": "2024 Topps Holiday Jordan Walker Relic", "tier": "yellow",
                       "max": 2.02, "price": 0.99})
    db.save_pick(con, {"item_id": "222222222222", "card": "2023 Bowman's Best Auto /99",
                       "tier": "fire", "max": 20.2, "value": 45.0})
    assert actions.skip(con, {"item_id": "111111111111"}).startswith("Hidden")
    assert db.passed(con)[0] == {
        "item_id": "111111111111", "title": "2024 Topps Holiday Jordan Walker Relic",
        "card": "2024 Topps Holiday Relic", "tier": "yellow", "price": 0.99,
        "at": db.passed(con)[0]["at"]}
    snipes = [{"item_id": "222222222222", "title": "2023 Bowmans Best Jordan Walker Auto 12/99",
               "seller": "x", "end_time": None, "max_bid": 31.31, "current_bid": 5.0,
               "status": "SCHEDULED"},
              {"item_id": "333333333333", "title": "2022 Bowman Chrome Jordan Walker Mojo",
               "seller": "x", "end_time": None, "max_bid": 4.04, "current_bid": 1.0,
               "status": "SCHEDULED"}]
    monkeypatch.setitem(config.load(), "GIXEN_USER", "me")
    monkeypatch.setattr(jobs.gixen, "snipes", lambda con: (snipes, None, 0))
    jobs._note_snipes(con)
    snipes[0]["max_bid"] = 35.35                                      # the amount was raised
    jobs._note_snipes(con)
    logged = {r["item_id"]: r for r in db.recent_snipes(con, 10)}
    assert len(logged) == 2 and (logged["222222222222"]["my_max"], logged["222222222222"][
        "bot_max"], logged["222222222222"]["bot_value"]) == (35.35, 20.2, 45.0)
    rules, sections = score.learned(con)
    assert "lean that way" in rules
    assert "(don't pick more like these) ===\n2024 Topps Holiday Relic | $0.99\n" in sections
    assert "2023 Bowman's Best Auto /99 | 35.35 vs 20.20" in sections
    assert "2022 Bowman Chrome Jordan Walker Mojo | 4.04 vs no pick" in sections
    prompt = score.build_prompt([item], learned=(rules, sections))
    assert prompt.index("lean that way") < prompt.index("Reply with ONLY")
    assert prompt.index("recently sniped") < prompt.index("=== listings")
    actions.unskip_all(con, {})
    assert db.passed(con) == []                                       # restored: not passed on
