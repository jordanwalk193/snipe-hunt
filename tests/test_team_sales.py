"""Team mode's sold prices: a comp is the same card, whoever is on it."""
import pytest

from snipehunt import config, db, profile, sales
from snipehunt.ebay_search import Listing

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()


@pytest.fixture(autouse=True)
def team(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))


def sale(con, n, title, price):
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                "VALUES (?,?,?,?,?,?)", (f"s{n}", title, price, 1.0, 4, 1_700_000_000 + n))


def listing(title):
    return Listing("1", title, 5.0, "USD", 1.0, ["AUCTION"], 0, None, "shop", "", None)


def test_another_players_card_from_the_same_set_is_not_a_comp():
    con = db.connect()
    sale(con, 1, "2023 Topps Chrome Jordan Walker Cardinals Gold Refractor RC", 60.0)
    sale(con, 2, "2023 Topps Chrome Masyn Winn Cardinals Gold Refractor RC", 25.0)
    got = sales.relevant(con, [listing("2023 Topps Chrome Masyn Winn Cardinals Gold Refractor RC")])
    assert [s["item_id"] for s in got] == ["s2"]


def test_the_team_name_is_not_what_makes_two_titles_alike():
    words = sales.words("2023 Topps Chrome Masyn Winn St. Louis Cardinals Refractor")
    assert "cardinals" not in words and "louis" not in words and "winn" in words


def test_price_history_keeps_players_apart():
    a, b = ("2023 Topps Chrome Jordan Walker Cardinals Gold Refractor RC",
            "2023 Topps Chrome Masyn Winn Cardinals Gold Refractor RC")
    assert not sales._same(a, b)
    assert sales._same(a, a + " nice")


def test_player_mode_still_matches_as_before(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse('[player]\nname = "Jordan Walker"'))
    assert sales.same_player("anything", "else")
