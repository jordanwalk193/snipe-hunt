"""The mislabel detectors: one listing in, one signal (or none) out."""
from datetime import datetime, timezone

import pytest

from snipehunt import config, known_cards, mislabel
from snipehunt.aspects import FALLBACK as NAMES
from snipehunt.ebay_search import Listing, parse_item

CFG = dict(mislabel.DEFAULTS)


@pytest.fixture
def known():
    (config.DATA_DIR / known_cards.FILE).write_text(
        (config.EXAMPLES / "known_cards.example.toml").read_text())
    return known_cards.load(with_inventory=False)


def item(title, price=30.0, auction=False, end=None, aspects=None, images=3,
         category="261328", feedback=500, description="", shipping=1.0):
    return Listing("900000000001", title, price, "USD", shipping,
                   ["AUCTION"] if auction else ["FIXED_PRICE"], 0, end, "made-up-seller",
                   "https://www.ebay.com/itm/900000000001", None,
                   seller_feedback_score=feedback, category_id=category, image_count=images,
                   short_description=description, aspects=aspects or {})


def names(signals):
    return [s.name for s in signals]


def test_typo_name_fires_on_a_misspelling_only():
    hit = mislabel.typo_name(item("2021 Bowman Chrome Jordon Walker Atomic Refractor"))
    assert hit and hit.weight == 3 and hit.positive and hit.why == '"Jordon"'
    assert mislabel.typo_name(item("2021 Jordan Walkr Bowman Chrome")).why == '"Walkr"'
    assert mislabel.typo_name(item("2021 Bowman Chrome Jordan Walker Atomic")) is None
    # spelled right somewhere in the title: buyers will find it
    assert mislabel.typo_name(item("Jordon Walker / Jordan Walker 2021 Bowman")) is None


def test_hidden_auto_needs_signed_specifics_and_no_auto_word():
    signed = item("2021 Bowman Chrome Jordan Walker Prospect", aspects={"Autographed": "Yes"})
    assert mislabel.hidden_auto(signed, NAMES).weight == 3
    sticker = item("2021 Bowman Chrome Jordan Walker", aspects={"Autograph Format": "Sticker"})
    assert "Sticker" in mislabel.hidden_auto(sticker, NAMES).why
    said = item("2021 Bowman Chrome Jordan Walker Auto", aspects={"Autographed": "Yes"})
    assert mislabel.hidden_auto(said, NAMES) is None
    plain = item("2021 Bowman Chrome Jordan Walker", aspects={"Autographed": "No"})
    assert mislabel.hidden_auto(plain, NAMES) is None


def test_facsimile_guard_holds_back_a_platinum_anniversary_auto():
    pa = item("2025 Topps Chrome Platinum Anniversary Jordan Walker Auto",
              aspects={"Autographed": "No"})
    hit = mislabel.facsimile_guard(pa, NAMES)
    assert hit and hit.weight == -5 and not hit.positive
    # no item specifics yet: the Platinum Anniversary rule still catches it
    bare = item("2025 Topps Chrome Platinum Anniversary Jordan Walker Signed")
    assert "printed signature" in mislabel.facsimile_guard(bare, NAMES).why
    # a real autograph card number proves it
    real = item("2025 Topps Chrome Platinum Anniversary Autographs CPA-JW Jordan Walker Auto")
    assert mislabel.facsimile_guard(real, NAMES) is None
    assert mislabel.facsimile_guard(item("Jordan Walker facsimile signature RC"), NAMES)
    assert mislabel.facsimile_guard(item("2023 Topps Chrome Jordan Walker RC"), NAMES) is None


def test_hidden_parallel_from_a_print_run_or_a_serial_in_the_description():
    run = item("2021 Bowman Chrome Jordan Walker", aspects={"Print Run": "150"})
    hit = mislabel.hidden_parallel(run, NAMES)
    assert hit and hit.weight == 2 and hit.why == "/150 in specifics, not title"
    shown = item("2021 Bowman Chrome Jordan Walker Atomic /150", aspects={"Print Run": "150"})
    assert mislabel.hidden_parallel(shown, NAMES) is None
    described = item("2021 Bowman Chrome Jordan Walker", description="Serial numbered 07/25")
    assert mislabel.hidden_parallel(described, NAMES).why == "/25 in specifics, not title"
    gold = item("2021 Bowman Chrome Jordan Walker Refractor",
                aspects={"Parallel/Variety": "Gold Refractor"})
    assert "Gold Refractor" in mislabel.hidden_parallel(gold, NAMES).why
    base = item("2021 Bowman Chrome Jordan Walker", aspects={"Parallel/Variety": "Base"})
    assert mislabel.hidden_parallel(base, NAMES) is None


def test_ba_jw1_without_a_product_word_says_nothing(known):
    assert mislabel.year_signal(item("2020 Leaf BA-JW1 Jordan Walker Auto"), known) is None


def test_ba_jw1_all_american_listed_2020_is_a_mismatch(known):
    hit = mislabel.year_signal(item("2020 Leaf All-American BA-JW1 Jordan Walker Auto"), known)
    assert hit.name == "year_set_mismatch" and hit.weight == 2
    assert "2019" in hit.why and "2020" in hit.why
    right = item("2019 Leaf Metal All-American BA-JW1 Jordan Walker Auto")
    assert mislabel.year_signal(right, known) is None


def test_bhp_52_listed_2023_is_drift_only(known):
    hit = mislabel.year_signal(item("2023 Bowman Heritage BHP-52 Jordan Walker"), known)
    assert hit.name == "year_drift" and hit.weight == 1
    assert mislabel.year_signal(item("2022 Bowman Heritage BHPC-52 Jordan Walker"), known) is None
    wrong = mislabel.year_signal(item("2024 Bowman Heritage BHP-52 Jordan Walker"), known)
    assert wrong.name == "year_set_mismatch"


def test_wrong_category():
    assert mislabel.wrong_category(item("Jordan Walker lot", category="183050")).weight == 1
    assert mislabel.wrong_category(item("Jordan Walker", category="261328")) is None
    assert mislabel.wrong_category(item("Jordan Walker", category=None)) is None


def test_thin_listing_and_its_scam_flip():
    thin = item("Jordan Walker Bowman", images=1, aspects={"Season": "2021"})
    hit = mislabel.thin_listing(thin, CFG, NAMES, value=40)
    assert hit.positive and "1 photo" in hit.why and "no card number" in hit.why
    full = item("2021 Bowman Chrome Prospects Jordan Walker Atomic Refractor BCP-236 RC",
                images=6, aspects={"Card Number": "BCP-236"})
    assert mislabel.thin_listing(full, CFG, NAMES, value=40) is None
    scam = item("Jordan Walker Bowman", price=10, images=1, feedback=3)
    flip = mislabel.thin_listing(scam, CFG, NAMES, value=60)
    assert flip.name == "too_good_new_seller" and flip.weight == -3 and not flip.positive


def test_odd_end_time_tuesday_3_30_am_et():
    end = parse_item({"legacyItemId": "1", "title": "t", "buyingOptions": ["AUCTION"],
                      "currentBidPrice": {"value": "5", "currency": "USD"},
                      "itemEndDate": "2026-10-20T07:30:00Z"}).end_time
    hit = mislabel.odd_end_time(item("Jordan Walker", auction=True, end=end), CFG)
    assert hit and hit.why == "Tue 3 AM ET"
    assert mislabel.end_et(item("x", auction=True, end=end)).strftime("%a %H:%M") == "Tue 03:30"
    # Buy It Nows have no end that matters; an evening auction isn't odd
    assert mislabel.odd_end_time(item("Jordan Walker", end=end), CFG) is None
    evening = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc).isoformat()
    assert mislabel.odd_end_time(item("Jordan Walker", auction=True, end=evening), CFG) is None


def test_odd_end_time_across_the_dst_change():
    # 07:30 UTC is 3:30 AM EDT in October and 2:30 AM EST once clocks go back (Nov 1)
    before = item("x", auction=True, end="2026-10-31T07:30:00+00:00")
    after = item("x", auction=True, end="2026-11-03T07:30:00+00:00")
    assert mislabel.end_et(before).strftime("%H:%M %Z") == "03:30 EDT"
    assert mislabel.end_et(after).strftime("%H:%M %Z") == "02:30 EST"
    assert mislabel.odd_end_time(after, CFG).why == "Tue 2 AM ET"


def test_odd_end_time_alone_never_qualifies():
    timing = [mislabel.Signal("odd_end_time", 1, True, "Tue 3 AM ET")]
    assert not mislabel.qualifies(timing)
    assert mislabel.qualifies(timing + [mislabel.Signal("wrong_category", 1, True, "x")])
    # more against than for
    assert not mislabel.qualifies([mislabel.Signal("typo_name", 3, True, "x"),
                                   mislabel.Signal("facsimile_guard", -5, False, "x")])
    assert mislabel.qualifies([mislabel.Signal("typo_name", 3, True, "x"),
                               mislabel.Signal("facsimile_guard", -5, False, "x")],
                              forgive_facsimile=True)


def test_detect_runs_them_all(known):
    found = mislabel.detect(item("2023 Bowman Heritage BHP-52 Jordon Walker", images=1,
                                 aspects={"Autographed": "Yes", "Print Run": "50"}), known, CFG)
    assert {"typo_name", "hidden_auto", "hidden_parallel", "year_drift",
            "thin_listing"} <= set(names(found))
