from datetime import datetime

from snipehunt import config, db, jobs, notify, snipecheck

ROWS = [
    {"id": "53", "year": "2023", "set": "Topps Chrome", "card": "Base", "parallel": "Base",
     "card_no": "209", "serial": "", "graded": "", "status": "In binder"},
    {"id": "110", "year": "2023", "set": "Topps Chrome", "card": "Base",
     "parallel": "Sepia Refractor", "card_no": "209", "serial": "", "graded": "", "status": "Won"},
    {"id": "24", "year": "2022", "set": "Bowman", "card": "Prospect (paper)", "parallel": "Base",
     "card_no": "", "serial": "", "graded": "", "status": "In binder"},
]


def snipe(item_id, title, amount=5.05):
    return {"item_id": item_id, "title": title, "max_bid": amount, "current_bid": 1.0,
            "seller": "someone", "end_time": "2099-01-01T21:00-05:00", "status": "SCHEDULED"}


def test_owned_copy_needs_the_same_number_parallel_and_grading():
    owned = lambda t: (snipecheck.owned_copy(t, ROWS) or {}).get("id")   # noqa: E731
    assert owned("2023 Topps Chrome Jordan Walker #209 Rookie RC") == "53"
    assert owned("2023 Topps Chrome Sepia Refractor Jordan Walker #209 RC") == "110"
    assert owned("2023 Topps Chrome Pink Refractor Jordan Walker #209 RC") is None
    assert owned("2023 Topps Chrome Jordan Walker #209 RC PSA 10") is None      # an upgrade
    assert owned("2023 Topps Chrome Jordan Walker #209 RC GMA 9") is None       # GMA is graded
    assert owned("2022 Bowman Hi-Fi Futures Jordan Walker HIFI-15") is None    # row has no number


def test_gixen_emoji_codes_do_not_hide_the_name():
    title = "uD83D uDD25/10 ROOKIE AUTO uD83D uDD25Jordan Walker 2022 Panini Prizm Draft Picks"
    assert snipecheck.clean(title).startswith("/10 ROOKIE AUTO Jordan Walker")
    con = db.connect()
    assert snipecheck.doubts(con, [snipe("1", title)], ROWS) == [[]]


def test_doubts_catch_what_slipped_through():
    con = db.connect()
    db.note_snipe(con, "5", "2024 Topps Jordan Walker #12", 4_000_000_000, "x")
    con.execute("UPDATE seen SET scored_at = 1 WHERE item_id = '5'")   # scored, never picked
    found = snipecheck.doubts(con, [
        snipe("1", "2023 Topps Chrome Jordan Walker #209 Rookie RC"),
        snipe("2", "2023 Bowman Chrome Jordan Walker Gold Refractor /50 #BCP-1"),
        snipe("3", "2023 Bowman Chrome Jordan Walker Gold Refractor 12/50 BCP-1"),
        snipe("4", "2022 Topps Chrome Kemba Walker basketball refractor"),
        snipe("5", "2024 Topps Jordan Walker #12 CCG 9"),
    ], ROWS)
    kinds = [sorted(d["kind"] for d in doubt) for doubt in found]
    assert kinds == [["owned"], ["twice"], ["twice"], ["not_player"], ["grader", "passed"]]


def test_each_problem_is_sent_once_and_quiet_hours_hold_far_off_ones(monkeypatch):
    con = db.connect()
    sent = []
    live = [snipe("1", "2023 Topps Chrome Jordan Walker #209 Rookie RC")]
    monkeypatch.setattr(jobs, "_live_snipes", lambda con: live)
    monkeypatch.setattr(jobs.inventory, "read", lambda: ([], ROWS))
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "send", lambda title, message, url=None, image=None:
                        sent.append((title, message)))
    noon = datetime(2026, 10, 17, 12, 0, tzinfo=config.TZ)
    assert jobs._warn_snipes(con, now=noon) == 1
    assert "You already have this: #53" in sent[0][1] and "Item 1" in sent[0][1]
    assert jobs._warn_snipes(con, now=noon) == 0          # already told
    live.append(snipe("2", "2023 Topps Chrome Sepia Refractor Jordan Walker #209"))
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: True)
    assert jobs._warn_snipes(con, now=noon) == 0          # ends in 2099: waits for morning
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: False)
    assert jobs._warn_snipes(con, now=noon) == 1


def test_the_dashboard_checks_gixen_titles_not_the_short_card_name(monkeypatch):
    from snipehunt import gixen, snapshot
    con = db.connect()
    db.save_pick(con, {"item_id": "1", "tier": "green", "card": "2020 Bowman Draft Chrome 1st BD-57 PSA 9"})
    raw = [dict(snipe("1", "JORDAN WALKER 2020 BOWMAN CHROME DRAFT 1ST BD-57 CARDINALS PSA 9"),
                end_time="2099-01-01T21:00-05:00")]
    monkeypatch.setattr(snapshot.config, "get", lambda k, d=None: "x" if k == "GIXEN_USER" else d)
    monkeypatch.setattr(gixen, "snipes", lambda con: (raw, None, 1.0))
    snap = snapshot.snapshot(con)
    # the example collection owns this card; the point is the name isn't doubted
    kinds = [d["kind"] for d in snap["gixen"]["snipes"][0]["doubts"]]
    assert "not_player" not in kinds and kinds == ["owned"]


def test_over_the_going_rate_reaches_the_phone_only_when_it_costs_real_money():
    con = db.connect()
    snipes = [{"item_id": "cheap"}, {"item_id": "dear"}]
    checks = [{"verdict": "over", "value": 3.0, "pct": -17},     # ~50 cents over
              {"verdict": "over", "value": 40.0, "pct": -20}]    # $8 over
    got = snipecheck.to_warn(con, snipes, checks, [[], []])
    assert [s["item_id"] for s, _, _ in got] == ["dear"]
    assert got[0][2] == {"dear:over"}
