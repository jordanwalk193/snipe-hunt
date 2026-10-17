"""Player mode is the original. Team mode must not change a byte of what it does.

The golden files were written from the code as it was before team mode
existed (commit a5d48e0). If one of these fails, player mode changed: that is
a bug unless the user asked for the change, and then the golden file is
rewritten in the same commit with the reason in the message.
"""
import json
from pathlib import Path

from snipehunt import ebay_search, profile, score, settings
from snipehunt.ebay_search import Listing

GOLDEN = Path(__file__).with_name("golden")

TITLES = [
    "2023 Topps Chrome Jordan Walker RC #209 Cardinals",
    "JORDON WALKER 2020 Bowman Draft 1st BD-57",
    "Jordan Walker RC lot of 5",
    "Walker Cardinals rookie lot (10)",
    "Larry Walker Cardinals 2005 Topps",
    "Jordan Walker Dallas Mavericks Panini Prizm RC",
    "2024 Topps Chrome Masyn Winn RC Cardinals Refractor /250",
    "St. Louis Cardinals 2023 Topps Team Set",
    "Jordan Walker auto",
    "J. Walker Cardinals 2023 Topps Heritage RC",
]


def listings() -> list[Listing]:
    return [
        Listing("100000000001", "2023 Topps Chrome Jordan Walker RC Gold Refractor /50",
                41.0, "USD", 4.99, ["AUCTION"], 3, "2026-10-12T21:30-04:00", "exampleshop",
                "https://www.ebay.com/itm/100000000001", None, "yes", ["auctions-ending"],
                1200, 99.8),
        Listing("100000000002", "Jordan Walker 2020 Bowman Draft BD-57 PSA 9",
                12.5, "USD", None, ["FIXED_PRICE", "BEST_OFFER"], None, None, "othershop",
                "https://www.ebay.com/itm/100000000002", None, "unclear", ["newly-listed"]),
    ]


def test_player_prompt_is_unchanged():
    got = score.build_prompt(listings(), 8)
    assert got == (GOLDEN / "player_prompt.txt").read_text()


def test_player_prompt_without_lookups_is_unchanged():
    got = score.build_prompt(listings(), 0)
    assert got == (GOLDEN / "player_prompt_no_lookups.txt").read_text()


def test_player_searches_are_unchanged():
    got = [list(q) for q in ebay_search.queries()]
    assert got == json.loads((GOLDEN / "player_queries.json").read_text())


def test_player_title_verdicts_are_unchanged():
    got = {t: profile.get().classify(t) for t in TITLES}
    assert got == json.loads((GOLDEN / "player_classify.json").read_text())


def test_player_settings_and_profile_are_unchanged():
    got = {"settings": settings.load(), "visible": settings.visible(),
           "public": profile.get().public()}
    assert got == json.loads((GOLDEN / "player_settings.json").read_text())


RAW_PICKS = [
    {"item_id": "100000000001", "card": "2023 Topps Chrome Gold Refractor /50", "tier": "fire",
     "max": 35.35, "max_basis": "sales", "comps": "two sold near $60", "value": 60,
     "edge": "sleeper", "want": True, "why": "few bids, ends tonight", "player": "Someone Else"},
    {"item_id": "100000000002", "card": "2020 Bowman Draft PSA 9", "tier": "green",
     "max": 15.15, "max_basis": "estimate", "value": 30, "edge": "mispriced", "want": False,
     "why": "priced under going rate"},
]


def test_player_picks_are_unchanged():
    items = {i.item_id: i for i in listings()}
    got = [score._pick_dict(items[r["item_id"]], r) for r in RAW_PICKS]
    assert got == json.loads((GOLDEN / "player_picks.json").read_text())
