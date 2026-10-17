"""The example profiles for other sports and for Pokémon, on titles of their kind."""
import pytest

from snipehunt import config, ebay_search, profile, sales

EX = config.EXAMPLES


def load(name, monkeypatch):
    p = profile.parse((EX / name).read_text())
    monkeypatch.setattr(profile, "_cache", p)
    return p


@pytest.mark.parametrize("name, yes, no, unclear", [
    ("profile.minimal.toml",
     ["2024 Panini Prizm Caleb Williams RC Silver Bears", "Caleb Wiliams USC Trojans Auto"],
     ["Caleb Williams baseball card Topps MLB", "Caleb Williams NBA Hoops"],
     ["Caleb Williams rookie card"]),
    ("profile.basketball.example.toml",
     ["2023 Prizm Victor Wembanyama RC Silver Spurs", "Viktor Wembanyama Hoops RC"],
     ["Victor Wembanyama football custom NFL"],
     ["Victor Wembanyama rookie lot"]),
    ("profile.pokemon.example.toml",
     ["Charizard ex 199/165 Pokemon 151 Special Illustration Rare PSA 10",
      "Charzard Base Set Holo 4/102 Pokémon", "Japanese Charizard VMAX tcg"],
     ["Charizard plush 12 inch", "Charizard custom proxy card holo", "Charizard Funko Pop"],
     ["Charizard card"]),
])
def test_example_profiles_judge_titles(monkeypatch, name, yes, no, unclear):
    p = load(name, monkeypatch)
    for t in yes:
        assert p.classify(t) == "yes", t
    for t in no:
        assert p.classify(t) == "no", t
    for t in unclear:
        assert p.classify(t) == "unclear", t


def test_a_one_word_name_works(monkeypatch):
    p = load("profile.pokemon.example.toml", monkeypatch)
    assert p.first_names == [] and "charzard" in p.last_names
    assert p.typo_searches and all(" " not in t for t in p.typo_searches)
    q = {label: text for label, text, _, _ in ebay_search.queries()}
    assert q["auctions-ending"] == "charizard" and q["psa-10"] == "charizard psa 10"


def test_a_profile_can_name_its_own_ebay_category(monkeypatch):
    load("profile.pokemon.example.toml", monkeypatch)
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    assert ebay_search.EbayClient().category == "183454"
    load("profile.minimal.toml", monkeypatch)
    assert ebay_search.EbayClient().category == "261328"


def test_pokemon_sold_prices_compare_set_numbers(monkeypatch):
    load("profile.pokemon.example.toml", monkeypatch)
    a = "Charizard ex 199/165 Pokemon 151 Special Illustration Rare PSA 10"
    assert sales._same(a, a + " gem")
    assert not sales._same(a, "Charizard ex 199/165 Pokemon 151 Special Illustration Rare PSA 9")
