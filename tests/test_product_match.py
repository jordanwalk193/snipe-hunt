"""Chrome, paper, Sapphire, Optic and 1st Edition are different prints of the same
card number: no sale of one may price another (sales.different_product).

The bug: a paper slab of a Bowman Draft card showed as half its value, and the
value was a Chrome slab sale of the same card number."""
import time

from snipehunt import db, ledger, sales
from snipehunt.ebay_search import Listing

PAPER_SLAB = "PSA 10 JORDAN WALKER 1st 2020 Bowman Draft Paper CARDINALS Rookie RC GEM MINT"
CHROME_SLAB = "PSA 10 JORDAN WALKER 1st 2020 Bowman Chrome Draft Rookie Card RC GEM MINT"

BARE_BD57 = "2020 Bowman Draft #BD-57 PSA 10"
CHROME_BD57 = "2020 Bowman Draft Chrome #BD-57 PSA 10"
REFRACTOR_BD57 = "2020 Bowman Draft Chrome Refractor #BD-57 PSA 10"
SAPPHIRE_BD57 = "2020 Bowman Draft Sapphire #BD-57 PSA 10"

PAPER_A = "2020 Bowman Draft Jordan Walker #BD-57 PSA 10 Gem Mint"
PAPER_B = "Jordan Walker 2020 Bowman Draft Paper BD-57 PSA 10 RC Cardinals"
CHROME_A = "2020 Bowman Draft Chrome Jordan Walker #BD-57 PSA 10 Gem Mint"
CHROME_B = "Jordan Walker 2020 Bowman Chrome Draft BD-57 PSA 10 RC Cardinals"


def _alike(a: str, b: str) -> bool:
    g = {"key": sales.card_key(b), "title": b, "traits": sales.traits(b)}
    return ledger._alike(g, sales.card_key(a), a)


def _close(a: str, b: str) -> float | None:
    return sales._closeness(a, sales.words(a), {"title": b, "words": sales.words(b)}, 0.5)


def _listing(title: str) -> Listing:
    return Listing(item_id="999", title=title, price=50.0, currency="USD", shipping=0.0,
                   buying_options=["FIXED_PRICE"], bid_count=0, end_time=None,
                   seller="someone", url="", image=None)


def _comps(listing_title: str, sale_title: str) -> list[dict]:
    con = db.connect()
    sales.add_manual(con, "1", 100.0, 0.0, sale_title, now=time.time() - 86400)
    return sales.ledger_comps(con, _listing(listing_title))


def _matches(a: str, b: str) -> dict:
    return {"alike": _alike(a, b), "same": sales._same(a, b),
            "close": _close(a, b) is not None, "comps": bool(_comps(a, b))}


NONE = {"alike": False, "same": False, "close": False, "comps": False}
ALL = {"alike": True, "same": True, "close": True, "comps": True}


def test_the_paper_slab_never_prices_from_the_chrome_slab():
    assert _matches(PAPER_SLAB, CHROME_SLAB) == NONE
    assert _matches(CHROME_SLAB, PAPER_SLAB) == NONE


def test_a_title_without_chrome_is_not_the_chrome_card_even_with_the_same_number():
    assert _close(BARE_BD57, CHROME_BD57) is None
    assert _close(CHROME_BD57, BARE_BD57) is None
    assert not _comps(BARE_BD57, CHROME_BD57) and not _comps(CHROME_BD57, BARE_BD57)
    assert not sales._same(BARE_BD57, CHROME_BD57) and not _alike(BARE_BD57, CHROME_BD57)


def test_a_chrome_refractor_is_not_a_sapphire():
    assert _close(REFRACTOR_BD57, SAPPHIRE_BD57) is None
    assert _close(SAPPHIRE_BD57, REFRACTOR_BD57) is None
    assert not _comps(REFRACTOR_BD57, SAPPHIRE_BD57) and not _comps(SAPPHIRE_BD57, REFRACTOR_BD57)
    assert not sales._same(REFRACTOR_BD57, SAPPHIRE_BD57)


def test_paper_titles_worded_differently_are_still_the_same_card():
    # one says "Paper", the other says neither: a bare title is the paper card
    assert _matches(PAPER_A, PAPER_B) == ALL
    assert _matches(PAPER_B, PAPER_A) == ALL


def test_chrome_titles_worded_differently_are_still_the_same_card():
    assert _matches(CHROME_A, CHROME_B) == ALL
    assert _matches(CHROME_B, CHROME_A) == ALL


def test_a_refractor_that_drops_the_word_chrome_is_still_chrome():
    # Paper Bowman has no Refractors, so a bare Refractor title is the Chrome card:
    # it prices from Chrome Refractors and never leaks into a paper listing.
    bare_refractor = "2020 Bowman Draft Jordan Walker #BD-57 Refractor PSA 10"
    assert _close(bare_refractor, REFRACTOR_BD57) is not None
    assert _close(PAPER_B, bare_refractor) is None
    assert not _comps(PAPER_B, bare_refractor)


def test_first_edition_is_not_plain_paper():
    first = "2020 Bowman Draft 1st Edition Jordan Walker #BD-57 PSA 10"
    assert _close(first, PAPER_A) is None and _close(PAPER_A, first) is None
    assert not sales._same(first, PAPER_A) and not _alike(first, PAPER_A)
    assert _close(first, "Jordan Walker 2020 Bowman Draft 1st Edition BD-57 PSA 10 RC") is not None


def test_the_ledger_keeps_paper_and_chrome_sales_in_separate_cards():
    con = db.connect()
    now = time.time()
    sales.add_manual(con, "1", 99.0, 4.99, CHROME_SLAB, now=now - 86400)
    sales.add_manual(con, "2", 30.0, 4.99, PAPER_SLAB, now=now - 2 * 86400)
    gs = ledger.groups(con)
    paper = ledger.find(gs, PAPER_SLAB)
    assert paper is not None and [s["item_id"] for s in paper["sales"]] == ["2"]
    assert ledger.value(paper) == 34.99


def test_find_missed_does_not_call_a_paper_sale_a_cheap_chrome():
    con = db.connect()
    now = time.time()
    for i, ago in enumerate((10, 8, 6)):
        sales.add_manual(con, str(i + 1), 100.0, 0.0, CHROME_A, now=now - ago * 86400)
    sales.add_manual(con, "9", 25.0, 0.0, PAPER_B, now=now - 86400)
    assert sales.find_missed(con, now, owned=[]) == []


def test_an_inventory_chrome_row_is_not_priced_from_a_paper_sale():
    con = db.connect()
    sales.add_manual(con, "1", 30.0, 0.0, PAPER_B, now=time.time() - 86400)
    gs = ledger.groups(con)
    row = {"year": "2020", "set": "Bowman Chrome Draft", "card": "Base", "parallel": "Base",
           "card_no": "BD-57", "graded": "PSA 10", "auto": "N"}
    assert ledger.match_row(gs, row) is None
    paper = dict(row, set="Bowman Draft")
    assert ledger.match_row(gs, paper) is not None


def test_a_named_chrome_insert_still_prices_from_titles_that_drop_chrome():
    con = db.connect()
    sales.add_manual(con, "1", 1.5, 0.0, "2022 Bowman #VV-13 Jordan Walker Virtuosic Vibrations",
                     now=time.time() - 86400)
    gs = ledger.groups(con)
    row = {"year": "2022", "set": "Bowman Chrome", "card": "Virtuosic Vibrations insert",
           "parallel": "Base", "card_no": "VV-13", "graded": "", "auto": "N"}
    assert ledger.match_row(gs, row) is not None
