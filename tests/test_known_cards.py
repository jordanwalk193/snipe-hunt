"""known_cards.toml: card numbers, their years, and the usual mix-ups."""
import pytest

from snipehunt import config, hidden, known_cards


@pytest.fixture
def known():
    (config.DATA_DIR / known_cards.FILE).write_text(
        (config.EXAMPLES / "known_cards.example.toml").read_text())
    return known_cards.load()


def test_ba_jw1_needs_a_product_word(known):
    assert known.check("2019 Leaf BA-JW1 Jordan Walker Auto").kind == "unknown"
    assert known.check("2020 Leaf Valiant BA-JW1 Jordan Walker Auto").kind == "match"
    assert known.check("2019 National Showcase BA-JW1 Walker").kind == "match"
    mixed = known.check("2020 Leaf Metal All-American BA-JW1 Walker")
    assert mixed.kind == "mismatch" and mixed.years == (2019,)   # the longer word wins
    metal = known.check("2021 Leaf Metal Draft BA-JW1 Walker")
    assert metal.kind == "mismatch" and metal.years == (2022, 2023)


def test_bhp_52_listed_2023_is_drift_only(known):
    assert known.check("2023 Bowman Heritage BHP-52 Walker").kind == "drift"
    assert known.check("2023 Bowman Heritage Chrome BHPC-52 Walker").kind == "drift"
    assert known.check("2022 Bowman Heritage BHP-52 Walker").kind == "match"


def test_fmf_10_listed_2023_is_flagged(known):
    verdict = known.check("2023 Bowman Chrome FMF-10 Jordan Walker Atomic")
    assert verdict.kind == "drift" and verdict.years == (2021,)
    assert known.check("2024 Bowman Chrome FMF-10 Walker").kind == "mismatch"


def test_the_rest_of_the_seed(known):
    assert known.check("2023 Bowman Chrome SOS-19 Walker").kind == "match"
    assert known.check("2022 Bowman Chrome BCP-236 Walker").kind == "mismatch"
    assert known.year_for("Leaf Metal DI-JW1 Walker auto") == (2019, "known_cards")
    assert known.year_for("2024 Topps Chrome Walker #12") == (2024, "title")


def test_inventory_rows_seed_the_table_but_never_override_the_file(known):
    # the example inventory has BD-57 from 2020
    assert known.check("2020 Bowman Draft BD-57 Walker").kind == "match"
    assert known.check("2021 Bowman Draft BD-57 Walker").kind == "mismatch"
    cards = known_cards.KnownCards(known_cards._cards_from_file(
        (config.EXAMPLES / "known_cards.example.toml").read_text()))
    cards.add(known_cards.Card("FMF-10", 2023, source="inventory"))
    assert [c.year for c in cards.by_number["FMF10"]] == [2021]


def test_no_file_means_an_empty_table():
    assert len(known_cards.load(with_inventory=False)) == 0


def test_hidden_searches_include_card_numbers_the_file_pins_to_one_year(known):
    from snipehunt import db
    pairs = hidden.card_numbers(db.connect())
    assert ("2021", "FMF-10") in pairs and ("2021", "BCP-236") in pairs
    assert not any(number == "BA-JW1" for _, number in pairs)   # five products, no one year
