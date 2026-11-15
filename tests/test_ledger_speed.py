"""The ledger's grouping is fast and finds exactly what pairing every sale did."""
import itertools

from snipehunt import config, db, ledger, profile, sales

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()


def fill(con, n=120):
    """Made-up sales: few card numbers and parallels, so many repeat."""
    sets = ["Topps Chrome", "Bowman Chrome", "Topps Heritage"]
    pars = ["", "Refractor", "Gold /50", "Blue Refractor"]
    extras = ["", "RC", "PSA 10", "Auto"]
    for i, (s, p, e, num) in enumerate(itertools.islice(
            itertools.cycle(itertools.product(sets, pars, extras, [3, 7])), n)):
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES (?,?,?,?,?,?)",
                    (f"t{i}", f"2023 {s} Sam Example {p} #{num} {e}", 5.0 + i % 9, 1.0, 2,
                     1_700_000_000 + i))
    con.commit()


def naive(con):
    """The old grouping: every sale against every group, by _same alone."""
    groups = []
    for row in sales.with_calls(con):
        g = next((g for g in groups if sales._same(row["title"], g["title"])), None)
        if g is None:
            g = {"title": row["title"], "sales": []}
            groups.append(g)
        g["sales"].append(row["item_id"])
    return sorted((g["title"], len(g["sales"])) for g in groups if len(g["sales"]) >= 2)


def test_history_groups_the_same_cards_as_pairing_every_sale():
    con = db.connect()
    fill(con)
    got = sales.history(con, limit=1000)
    assert sorted((g["title"], g["n"]) for g in got) == naive(con)
    assert got   # the fixture has repeats


def test_history_asks_same_only_of_likely_groups(monkeypatch):
    con = db.connect()
    fill(con)
    calls = []
    real = sales._same
    monkeypatch.setattr(sales, "_same", lambda a, b: calls.append(1) or real(a, b))
    sales.history(con)
    # without the traits and word pre-checks this was one call per sale per group
    assert len(calls) <= 120


def test_title_words_follow_a_profile_change(monkeypatch):
    title = "2023 Topps Chrome Masyn Winn Cardinals Refractor"
    monkeypatch.setattr(profile, "_cache", profile.parse('[player]\nname = "Jordan Walker"'))
    assert "cardinals" in sales.words(title)
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))
    assert "cardinals" not in sales.words(title)   # a team bot drops its team's words
    assert sales.card_key("Gold 07/50 #3") == sales.card_key("Gold /50 #3")


def naive_groups(con):
    """ledger.groups as it was: every sale against every card, in order."""
    out = []
    for row in sales.with_calls(con):
        if ledger.LOT.search(row["title"] or ""):
            continue
        key, kind = sales.card_key(row["title"]), sales.traits(row["title"])
        g = next((g for g in out if g["traits"] == kind and ledger._alike(g, key, row["title"])), None)
        if g is None:
            g = {"title": row["title"], "key": key, "traits": kind, "sales": []}
            out.append(g)
        g["sales"].append(row["item_id"])
    return out


def test_ledger_groups_and_find_match_a_scan_of_every_card():
    con = db.connect()
    fill(con, 200)
    # key sizes far apart: a short title and a long one of the same card number
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES "
                "('x1', 'Sam Example #3', 4, 1, 1, 1), "
                "('x2', '2023 Topps Chrome Sam Example Refractor #3 RC Prospect Insert Shiny', 4, 1, 1, 2)")
    con.commit()
    got = ledger.groups(con)
    want = naive_groups(con)
    assert [(g["title"], [s["item_id"] for s in g["sales"]]) for g in got] == \
        [(g["title"], g["sales"]) for g in want]
    for title in [g["title"] for g in want] + ["2023 Topps Chrome Sam Example Refractor #3",
                                               "Sam Example #7 PSA 10", "nothing like it"]:
        key, kind = sales.card_key(title), sales.traits(title)
        scan = next((g for g in got if g["traits"] == kind and ledger._alike(g, key, title)), None)
        assert ledger.find(got, title) is scan
    shorter = got[1:]   # another list is indexed afresh
    assert ledger.find(shorter, got[0]["title"]) is not got[0]


def test_last_sold_finds_what_a_scan_of_every_sale_finds(monkeypatch):
    con = db.connect()
    fill(con, 200)
    rows = sales.with_calls(con)
    titles = [r["title"] for r in rows[:60]] + ["2023 Topps Chrome Sam Example #3", "unrelated"]
    for title in titles:
        scan = [r for r in rows if r["item_id"] != "t0" and sales._same(title, r["title"])]
        got = sales.last_sold(rows, title, "t0")
        assert (got["n"], got["total"]) == (len(scan), scan[0]["total"]) if scan else got is None
    # a team profile reads the words differently: the index is built again
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))
    title = rows[5]["title"]
    scan = [r for r in rows if sales._same(title, r["title"])]
    assert sales.last_sold(rows, title)["n"] == len(scan)
