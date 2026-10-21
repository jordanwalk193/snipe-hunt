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
