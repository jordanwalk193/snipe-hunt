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


def test_a_seller_with_an_open_order_who_combines_shipping_costs_nothing_to_add_to():
    from snipehunt import db, inventory, notify, settings
    from snipehunt.ebay_search import Listing
    con = db.connect()
    assert score.open_order_sellers(con) == set() and "open orders" not in score.learned(con)[1]
    fields, rows = inventory.read()
    rows.append(dict.fromkeys(fields, "") | {"id": "90", "status": "Won", "seller": "CombineShop"})
    rows.append(dict.fromkeys(fields, "") | {"id": "91", "status": "In transit",
                                             "seller": "shippedalready"})
    inventory.write(fields, rows)
    today = score.datetime.now(config.TZ).strftime("%Y-%m-%d")
    db.log_mail_events(con, [
        {"date": today, "type": "won", "is_player": True, "item_id": "1", "seller": "slowpay"},
        {"date": today, "type": "won", "is_player": True, "item_id": "2", "seller": "paidup"},
        {"date": today, "type": "paid", "is_player": True, "item_id": "2", "seller": "paidup"}])
    assert score.open_order_sellers(con) == {"combineshop", "slowpay"}
    assert ("treat added shipping as about $0 if the seller combines): combineshop, slowpay"
            in score.learned(con)[1])
    # only a seller named in the setting gets the $0, whatever orders are open
    settings.update(con, {"combined_ship_sellers": ["combineshop"]})   # none by default
    assert score.combined_sellers(con) == {"combineshop"}
    raw = {"tier": "green", "max": 6.06, "value": 20}
    card = Listing("1", "t", 0.99, "USD", 4.0, ["AUCTION"], 0, None, "combineshop", "u", None)
    plain = score._pick_dict(card, raw)
    riding = score._pick_dict(card, raw, score.combined_sellers(con))
    assert (plain["deal_pct"], plain["combined_ship"]) == (50, False)      # 10.06 against 20
    assert (riding["deal_pct"], riding["combined_ship"]) == (70, True)     # 6.06 against 20
    assert score.rerate(dict(riding, shipping=5.0))["deal_pct"] == 70      # still rides along
    other = Listing("2", "t", 0.99, "USD", 4.0, ["AUCTION"], 0, None, "slowpay", "u", None)
    assert score._pick_dict(other, raw, score.combined_sellers(con))["combined_ship"] is False
    line = notify.format_pick(dict(riding, card="c", url="u")).split("\n")[1]
    assert line.startswith("$0.99 + $4.00 ship (combined ship w/ open order)")
    settings.update(con, {"combined_ship_sellers": "slowpay, combineshop"})
    assert score.combined_sellers(con) == {"combineshop", "slowpay"}


def test_the_prompt_knows_a_slabbed_signature_is_not_a_pack_auto():
    text = " ".join(score.PROMPT.split())
    assert ('A signed paper base card in a BAS/JSA/PSA-DNA "Authentic Autograph" slab is an '
            "in-person signature, not a pack-pulled Chrome/Bowman auto, and sells for much "
            "less.") in text
    assert '"Auto 10" or "Beckett 10" on such a label grades the signature only' in text
    assert "don't let a listing's condition field override what the title says" in text
    # it is a judging rule: it comes before the tiers and the reply format
    assert text.index("Authentic Autograph") < text.index("Tiers:") < text.index("Reply with")


def test_the_example_preferences_carry_the_caution_lines():
    text = (config.EXAMPLES / "preferences.example.md").read_text()
    readme = (config.EXAMPLES.parent / "README.md").read_text()
    for line in ("A BAS/JSA slabbed signature on a paper base card is worth maybe $20–30 at most."
                 ' Never tier one "fire".',
                 "A Buy It Now over $25 needs real comps; if the value is an estimate, say so and"
                 " give it 3 stars at most.",
                 "Be skeptical of sellers under about 100 feedback on anything over $30."):
        assert line in text and line in readme
    assert "Never tier one \"fire\"" in score.build_prompt([])   # a new setup starts with them


def test_a_failed_call_says_why_not_how_many_tokens(monkeypatch):
    reply = {"usage": {"input_tokens": 0, "server_tool_use": {"web_search_requests": 0}},
             "total_cost_usd": 0, "is_error": True, "result": "Claude usage limit reached"}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, json.dumps({"duration_api_ms": 0, **reply}), ""))
    try:
        score.ask_claude("hello")
    except score.ClaudeError as exc:
        assert "usage limit reached" in str(exc)
    else:
        raise AssertionError("no ClaudeError")
