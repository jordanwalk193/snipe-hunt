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
    assert calls[-1][1] == "WebSearch" and "at most 8 lookups" in calls[-1][0]
    settings.update(db.connect(), {"lookup_fetch": True})   # whole pages only when asked for
    score.score([item])
    assert calls[-1][1] == score.WEB_TOOLS
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


# --- the ledger pre-filter: in steals mode, listings priced past a steal never reach Claude

CARD = "2023 Topps Chrome Example Player Refractor #209 RC"
_SOLD = [0]


def _listing(item_id, title, price, shipping=0.0, auction=True, seller="s"):
    from snipehunt.ebay_search import Listing
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                   end_time="2026-11-20T00:00:00-05:00", seller=seller, url="u", image=None)


def _sold(con, title, totals):
    import time
    now = int(time.time())
    for i, total in enumerate(totals):
        _SOLD[0] += 1
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"90000000{_SOLD[0]:04d}", title, total, 0.0, 3, now - i * 86400))
    con.commit()


def test_a_listing_asking_more_than_its_recorded_sales_is_priced_out():
    from snipehunt import db, sales
    con = db.connect()
    _sold(con, CARD, [12.0, 10.0, 11.0])         # the card goes for about $11 delivered
    dear = _listing("1", CARD, price=9.0, shipping=4.0)      # $13 delivered: no steal possible
    cheap = _listing("2", CARD, price=3.0, shipping=1.0)
    assert score.priced_out(dear, sales.ledger_comps(con, dear))
    assert not score.priced_out(cheap, sales.ledger_comps(con, cheap))
    # shipping unknown is costed the usual way; a combined-shipping seller pays none
    assert score.priced_out(_listing("3", CARD, price=8.0, shipping=None),
                            sales.ledger_comps(con, dear))
    assert not score.priced_out(_listing("4", CARD, price=9.0, shipping=4.0, seller="Combo"),
                                sales.ledger_comps(con, dear), combined=frozenset({"combo"}))


def test_nothing_priced_by_other_rules_is_priced_out():
    from snipehunt import db, sales
    con = db.connect()
    for title in ("2023 Topps Chrome Example Player Auto #CA-EP",
                  "2023 Topps Chrome Example Player #209 PSA 10",
                  "2023 Topps Chrome Example Player Gold Refractor /50 #209"):
        _sold(con, title, [20.0, 22.0, 21.0])
        item = _listing("7", title, price=60.0, shipping=5.0)
        assert not score.priced_out(item, sales.ledger_comps(con, item)), title
    assert not score.priced_out(_listing("8", CARD, 99.0), [])   # nothing on record: Claude decides


def _three(con):
    from snipehunt import db
    _sold(con, CARD, [12.0, 10.0, 11.0])
    dear, cheap = _listing("1", CARD, price=15.0), _listing("2", CARD, price=2.0)
    other = _listing("3", "2022 Bowman Example Player #BP-1", price=15.0)   # no sales on record
    for i in (dear, cheap, other):
        db.touch(con, i.item_id, i.title)
    con.commit()
    return [dear, cheap, other]


def test_in_steals_mode_priced_out_listings_are_scored_without_a_claude_call(monkeypatch):
    from snipehunt import db, settings
    con = db.connect()
    settings.update(con, {"max_share": 0.5})
    asked, saved = [], []
    monkeypatch.setattr(score, "ask_claude", lambda prompt, tools=score.WEB_TOOLS, **kw:
                        asked.append(prompt) or '{"picks": []}')
    picks, scored = score.score(_three(con), verbose=True, con=con,
                                on_batch=lambda batch, batch_picks: saved.append(
                                    ([b.item_id for b in batch], batch_picks)))
    assert len(asked) == 1 and '"item_id": "1"' not in asked[0]
    assert '"item_id": "2"' in asked[0]
    assert {i.item_id for i in scored} == {"1", "2", "3"}
    assert saved[0] == (["1"], [])                      # the skipped one is saved first, no pick
    assert set(saved[1][0]) == {"2", "3"}
    assert con.execute("SELECT n FROM stats WHERE key='prefiltered'").fetchone()[0] == 1


def test_player_mode_still_sends_every_listing_to_claude(monkeypatch):
    from snipehunt import db
    con = db.connect()
    asked = []
    monkeypatch.setattr(score, "ask_claude", lambda prompt, tools=score.WEB_TOOLS, **kw:
                        asked.append(prompt) or '{"picks": []}')
    score.score(_three(con), con=con)
    assert len(asked) == 1 and all(f'"item_id": "{n}"' in asked[0] for n in "123")
    assert con.execute("SELECT n FROM stats WHERE key='prefiltered'").fetchone() is None


def test_a_claude_reply_is_read_for_what_the_call_took(monkeypatch, capsys):
    from snipehunt import db, usage
    reply = {"result": "{}", "total_cost_usd": 0.41, "num_turns": 5, "modelUsage": {
        "claude-x": {"inputTokens": 100, "cacheReadInputTokens": 900, "outputTokens": 50,
                     "webSearchRequests": 2}}}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, json.dumps(reply), ""))
    assert score.ask_claude("hello", step="appraise 3 snipes") == "{}"
    assert ("claude: appraise 3 snipes: $0.41, 5 turns, 2 web, 1k in (900 cached), 50 out"
            in capsys.readouterr().err)
    log = json.loads(db.kv_get(db.connect(), usage.LOG_KEY))
    assert log[-1]["web"] == 2 and log[-1]["step"] == "appraise 3 snipes"


def test_ask_json_names_its_call_and_forgets_the_name_after(monkeypatch):
    seen = []
    monkeypatch.setattr(score, "ask_claude", lambda prompt, tools=score.WEB_TOOLS:
                        seen.append(score._STEP) or "{}")
    score.ask_json("hi", tools="", step="stories for 3 picks")
    assert seen == ["stories for 3 picks"] and score._STEP == ""


# --- the prompt in two halves: the stable one cached, the batch one sent each time

def _two():
    from snipehunt.ebay_search import Listing
    return [Listing("1", "2023 Topps Chrome Example Player RC #209", 4.0, "USD", 1.0, ["AUCTION"], 2,
                    "2026-11-20T00:00:00-05:00", "s", "u", None),
            Listing("2", "Example Player 2020 Bowman Draft BD-57 PSA 9", 12.5, "USD", None,
                    ["FIXED_PRICE"], None, None, "t", "u", None)]


def _lines(text):
    """Every line but the blank ones, in any order."""
    return sorted(line for line in text.split("\n") if line.strip())


def test_the_two_halves_say_exactly_what_the_one_prompt_says():
    sold = [{"title": "2023 Topps Chrome Example Player RC #209", "price": 9.0, "shipping": 1.0,
             "sold_at": 1_700_000_000, "bids": 4, "kind": "auction", "hint": False}]
    for lookups in (8, 0):
        whole = score.build_prompt(_two(), lookups, sold, "- track line\n", ("- learned rule\n", "=== learned ===\nx\n\n"))
        static, batch = score.prompt_halves(_two(), lookups, sold, "- track line\n",
                                            ("- learned rule\n", "=== learned ===\nx\n\n"))
        assert _lines(static + batch) == _lines(whole + score.BATCH_HEAD)
        # the stable half holds no listing, lookup budget or sold price; the batch half does
        assert "Example Player 2020 Bowman" not in static and "lookups" not in static
        assert "sold prices seen" not in static and "track line" not in static
        assert '"item_id": "2"' in batch and "track line" in batch and "learned rule" in batch
        assert "HARD RULES" in static and "=== inventory.csv ===" in static
        assert batch.rstrip().endswith('"not_player": ["item_id", …]}')
    # and it is the same for any batch, so the API can serve it from its cache
    assert score.prompt_halves(_two()[:1], 3)[0] == score.prompt_halves(_two()[1:], 8)[0]
    assert score.system_for_scoring().endswith(score.prompt_halves([], 0)[0])


def test_team_mode_splits_its_own_prompt_the_same_way(monkeypatch):
    from snipehunt import profile
    monkeypatch.setattr(profile, "_cache", profile.parse(
        (config.EXAMPLES / "profile.team.example.toml").read_text()))
    whole = score.build_prompt(_two(), 4)
    static, batch = score.prompt_halves(_two(), 4)
    assert _lines(static + batch) == _lines(whole + score.BATCH_HEAD)
    assert '"want_ceiling"' in batch and "HARD RULES" in static


def test_scoring_sends_the_stable_half_as_the_system_prompt(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs["input"]))
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": '{"picks": []}'}), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    score.score(_two())
    (cmd, sent), = seen
    system = cmd[cmd.index("--system-prompt") + 1]
    assert "Today is" in system and "=== preferences.md ===" in system and "HARD RULES" in system
    assert sent.startswith(score.BATCH_HEAD) and '"item_id": "1"' in sent and "HARD RULES" not in sent
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    score.ask_claude("hello")                      # other calls keep the usual system prompt
    assert "=== preferences.md ===" not in seen[-1][0][seen[-1][0].index("--system-prompt") + 1]


def test_the_warm_job_sends_what_scoring_sends_ahead_of_the_listings(monkeypatch):
    from snipehunt import db, jobs, profile, settings
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, input=kwargs["input"])
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok"}), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert jobs.warm() == "prompt cache warm (ok)"
    cmd = seen["cmd"]
    system = cmd[cmd.index("--system-prompt") + 1]
    assert system == score.system_for_scoring() and "=== inventory.csv ===" in system
    assert cmd[cmd.index("--tools") + 1] == score.web_tools()     # the same tools as a scoring call
    assert "single word ok" in seen["input"]
    settings.update(db.connect(), {"price_lookups": 0})
    jobs.warm()
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == ""
    assert jobs.warm(dry_run=True) == "would warm the prompt cache"
    monkeypatch.setattr(profile, "_cache", profile.parse(
        (config.EXAMPLES / "profile.team.example.toml").read_text()))
    seen.clear()
    assert jobs.warm().startswith("nothing to warm") and not seen


def test_the_warm_units_run_the_warm_job_at_half_past():
    from pathlib import Path
    deploy = Path(__file__).resolve().parent.parent / "deploy"
    service = (deploy / "snipe-hunt-warm.service").read_text()
    timer = (deploy / "snipe-hunt-warm.timer").read_text()
    assert "-m snipehunt.jobs warm" in service and "Type=oneshot" in service
    assert "OnCalendar=*:31" in timer


def test_an_auction_of_a_card_over_the_long_shot_line_is_yellow_not_fire():
    cfg = dict(score.settings.load(), long_shot_value=50)
    base = {"tier": "fire", "max_basis": "sales", "price": 10.0, "shipping": 1.0, "value": 80.0,
            "is_auction": True, "deal": 4, "max": 12.12}
    got = score.apply_caps(dict(base), cfg)
    assert got["long_shot"] and got["tier"] == "yellow" and got["max"] == 12.12   # max unchanged
    assert score.apply_caps(dict(base, is_auction=False), cfg)["tier"] == "fire"   # can be bought
    assert score.apply_caps(dict(base, value=45.0), cfg)["tier"] == "fire"         # under the line
    assert score.apply_caps(dict(base, want=True), cfg)["tier"] == "fire"          # want list keeps it
    off = score.apply_caps(dict(base), dict(cfg, long_shot_value=0))
    assert off["tier"] == "fire" and "long_shot" not in off                         # switched off


def test_long_shots_are_off_in_player_mode_and_on_at_50_in_steals_mode(monkeypatch):
    from snipehunt import profile, settings
    assert settings.load()["long_shot_value"] == 0
    monkeypatch.setattr(profile, "_cache", profile.parse(
        (config.EXAMPLES / "profile.example.toml").read_text() + "\n[steals]\n"))
    assert settings.load()["long_shot_value"] == 50


def test_a_new_seller_cap_still_comes_off_once_the_price_falls_under_a_long_shot_line():
    cfg = dict(score.settings.load(), long_shot_value=50, min_seller_feedback=100)
    pick = {"tier": "green", "max_basis": "sales", "price": 40.0, "shipping": 1.0, "value": 45.0,
            "is_auction": True, "seller_feedback": 3}
    capped = score.apply_caps(dict(pick), cfg)
    assert capped["tier"] == "yellow" and capped["capped_from"] == "green"
    assert score.apply_caps(dict(capped, price=20.0), cfg)["tier"] == "green"
