import json

from snipehunt import config, db, usage


def costs(con):
    return json.loads(db.kv_get(con, usage.KEY, "{}"))


def test_a_cost_is_added_to_todays_total():
    usage.record(0.25)
    usage.record("0.5")
    (day,) = costs(db.connect()).values()
    assert day == {"usd": 0.75, "calls": 2}


def test_a_busy_database_spools_the_cost_and_the_next_call_adds_it(monkeypatch):
    monkeypatch.setattr(usage, "BUSY_MS", 50)
    holder = db.connect()
    holder.execute("BEGIN IMMEDIATE")          # this same run, mid-write
    usage.record(1.0)
    assert (config.STATE_DIR / usage.SPOOL).exists()
    holder.rollback()
    usage.record(0.5)
    (day,) = costs(db.connect()).values()
    assert day == {"usd": 1.5, "calls": 2}
    assert not (config.STATE_DIR / usage.SPOOL).exists()


def test_junk_is_ignored():
    usage.record(None)
    assert costs(db.connect()) == {}


def test_nothing_holds_the_write_lock_while_claude_is_asked(monkeypatch):
    from snipehunt import score, snipecheck
    con = db.connect()
    seen = []

    def fake_ask(prompt, tools="", **kw):
        seen.append(con.in_transaction)
        return '{"values": []}'
    monkeypatch.setattr(score, "ask_claude", fake_ask)
    db.kv_set(con, "gixen_cache", "{}")      # an uncommitted write, like the Gixen read
    snipecheck.appraise(con, [{"item_id": "1", "title": "2023 Topps Example Player #1"}])
    assert seen == [False]


def test_what_a_call_was_made_of_is_counted_and_logged(monkeypatch):
    monkeypatch.setenv(usage.JOB_ENV, "hourly")
    outer = {"num_turns": 9, "modelUsage": {"claude-x": {
        "inputTokens": 1200, "cacheCreationInputTokens": 30000, "cacheReadInputTokens": 8000,
        "outputTokens": 2500, "webSearchRequests": 6}}}
    d = usage.detail(outer, 47.6, "score 1/2: 80 listings")
    assert d == {"step": "score 1/2: 80 listings", "turns": 9, "web": 6, "tokens_in": 39200,
                 "cached": 8000, "tokens_out": 2500, "secs": 48, "model": "claude-x"}
    assert usage.describe(0.52, d) == ("score 1/2: 80 listings: $0.52, 9 turns, 6 web, "
                                       "39k in (8k cached), 2k out, 48s")
    usage.record(0.52, d)
    usage.record(0.10, usage.detail({}, 3))      # a reply with no usage block still counts
    con = db.connect()
    stats = {r["key"]: r["n"] for r in con.execute("SELECT key, n FROM stats")}
    assert stats["claude_calls:hourly"] == 2 and stats["claude_web:hourly"] == 6
    assert stats["claude_turns:hourly"] == 9 and stats["claude_tokens_in:hourly"] == 39200
    log = json.loads(db.kv_get(con, usage.LOG_KEY))
    assert [e["usd"] for e in log] == [0.52, 0.10] and log[0]["step"] == "score 1/2: 80 listings"
    assert log[0]["job"] == "hourly" and "at" in log[0] and "day" not in log[0]
    (day,) = costs(con).values()
    assert day == {"usd": 0.62, "calls": 2}


def test_the_call_log_keeps_only_the_last_calls(monkeypatch):
    monkeypatch.setattr(usage, "LOG_KEEP", 3)
    for n in range(5):
        usage.record(n, usage.detail({}, 1, f"call {n}"))
    log = json.loads(db.kv_get(db.connect(), usage.LOG_KEY))
    assert [e["step"] for e in log] == ["call 2", "call 3", "call 4"]


def test_the_self_review_reads_what_the_calls_were_made_of(monkeypatch):
    from snipehunt import reflect
    monkeypatch.setenv(usage.JOB_ENV, "hourly")
    usage.record(0.40, usage.detail({"num_turns": 4, "modelUsage": {"m": {
        "inputTokens": 1000, "outputTokens": 200, "webSearchRequests": 3}}}, 5))
    usage.record(0.20, usage.detail({"num_turns": 2, "modelUsage": {"m": {
        "inputTokens": 3000, "outputTokens": 400, "webSearchRequests": 1}}}, 5))
    con = db.connect()
    db.bump(con, "prefiltered", 7)
    con.commit()
    from datetime import datetime, timedelta
    day = datetime.now(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    cost = reflect.window(con, day.timestamp(), (day + timedelta(days=1)).timestamp())["cost"]
    assert cost["calls"] == 2 and cost["web_searches_per_call"] == 2.0
    assert cost["turns_per_call"] == 3.0 and cost["tokens_in_per_call"] == 2000
    assert cost["listings_skipped_by_ledger"] == 7 and cost["usd_per_call"] == 0.3
