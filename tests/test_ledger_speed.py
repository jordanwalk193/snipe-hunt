"""The ledger's grouping is fast and finds exactly what pairing every sale did."""
import itertools

from snipehunt import config, db, profile, sales

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
