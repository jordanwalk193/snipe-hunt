import pytest

from snipehunt import config, inventory, rainbow


@pytest.fixture
def example(monkeypatch):
    monkeypatch.setattr(rainbow, "FILE", config.DATA_DIR / "rainbow.md")
    rainbow.FILE.write_text((config.EXAMPLES / "rainbow.example.md").read_text())


def test_a_parallel_matches_only_its_own_color_and_finish():
    same = rainbow.same_parallel
    assert same("Base", "2026 Topps Chrome Jordan Walker #221")
    assert not same("Base", "2026 Topps Chrome Jordan Walker #221 Refractor")
    assert same("Refractor", "Silver Refractor")
    assert not same("Refractor", "Pink Refractor")
    assert same("Green Refractor", "2026 Topps Chrome Walker #221 Green Refractor /99")
    assert not same("Green Refractor", "2026 Topps Chrome Walker #221 Green Wave Refractor /99")
    assert same("Green Geometric Refractor", "Green Geo Refractor 38/99")
    assert same("X-Fractor", "x fractor") and same("X-Fractor", "XFractor")
    assert same("Red White & Blue Refractor", "Red, White and Blue Refractor")
    assert not same("Red Refractor", "Red White & Blue Refractor")
    assert not same("Blue Refractor", "Red White & Blue Refractor")


def test_no_file_means_no_rainbows(monkeypatch):
    monkeypatch.setattr(rainbow, "FILE", config.DATA_DIR / "rainbow.md")
    assert rainbow.read() == [] and rainbow.view([], [], []) == []


def test_the_example_reads_into_cards_and_parallels(example):
    cards = rainbow.read()
    assert [c["card"] for c in cards] == ["2023 Topps Chrome #209"]
    first = cards[0]
    assert first["year"] == "2023" and first["owned_when"] == "Topps Chrome -Sapphire; 209"
    assert {"name": "Green Refractor", "run": 99, "where": ""} in first["parallels"]


def test_view_counts_owned_and_for_sale_with_the_print_run(example):
    live = [{"title": "2023 Topps Chrome Jordan Walker #209 Green Refractor /99", "total": 9.0},
            {"title": "2023 Topps Chrome Jordan Walker #209 Green Refractor 12/99", "total": 7.5},
            {"title": "2023 Topps Chrome Jordan Walker #209 Green Refractor /150", "total": 1.0}]
    card = rainbow.view(inventory.read()[1], live, [])[0]
    by = {p["name"]: p for p in card["parallels"]}
    assert by["Refractor"]["owned"]["id"] == "1" and card["owned"] == 1   # example row 1
    assert (by["Green Refractor"]["for_sale"], by["Green Refractor"]["cheapest"]) == (2, 7.5)
    assert by["Green Wave Refractor"]["for_sale"] == 0
