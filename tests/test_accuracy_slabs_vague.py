"""Two ways a cheap card looked dear:

- a slab from a grader the bot didn't know (GMA, PGI, TAG...) counted as a raw
  card, so a $12 GMA 10 priced a $2 raw card;
- a title with no year, no card number and no set ("JORDAN WALKER RC ROOKIE
  REFRACTOR") was a ledger card of its own, and any other vague Refractor
  listing priced from it."""
import time

from snipehunt import db, ledger, sales
from snipehunt.ebay_search import Listing

RAW = "2023 Topps Chrome Update Jordan Walker Youthquake RC #YQ-51 Cardinals"


def _listing(title: str) -> Listing:
    return Listing(item_id="999", title=title, price=5.0, currency="USD", shipping=0.0,
                   buying_options=["FIXED_PRICE"], bid_count=0, end_time=None,
                   seller="someone", url="", image=None)


def test_other_graders_are_slabs_with_their_own_grade():
    for title, grade in ((RAW + " GMA 10", "gma10"), (RAW + " GMA 9", "gma9"),
                         ("PGI 10 Jordan Walker 2023 Topps Now #95", "pgi10"),
                         (RAW + " TAG 9.5", "tag9.5"), (RAW + " HGA 9", "hga9"),
                         (RAW + " CSG 9", "csg9"), (RAW + " AGS 10", "ags10"),
                         (RAW + " ISA 10", "isa10"), (RAW + " KSA 9", "ksa9")):
        assert sales.traits(title)[0] == grade, title
    assert sales.traits(RAW)[0] is None


def test_a_word_that_only_looks_like_a_grader_is_not_a_grade():
    for title in (RAW + " with price tag", RAW + " ace of the staff", RAW + " gma"):
        assert sales.traits(title)[0] is None, title


def test_an_off_brand_slab_never_prices_the_raw_card():
    con = db.connect()
    now = time.time()
    sales.add_manual(con, "1", 15.0, 0.0, RAW + " GMA 10", now=now - 86400)
    sales.add_manual(con, "2", 2.0, 0.0, RAW, now=now - 2 * 86400)
    comps = sales.ledger_comps(con, _listing(RAW))
    assert [c["item_id"] for c in comps] == ["2"]
    assert [c["item_id"] for c in sales.ledger_comps(con, _listing(RAW + " GMA 10"))] == ["1"]
    raw = ledger.find(ledger.groups(con), RAW)
    assert [s["item_id"] for s in raw["sales"]] == ["2"]


VAGUE = "JORDAN WALKER ST LOUIS CARDINALS RC ROOKIE REFRACTOR"


def test_a_title_that_names_no_year_number_or_set_cannot_identify_a_card():
    assert not sales.identifiable(VAGUE)
    assert not sales.identifiable("JORDAN WALKER RARE TOPPS INVESTMENT CARD ST LOUIS CARDINALS MINT SP")
    assert sales.identifiable("2023 Topps Jordan Walker")                       # a year
    assert sales.identifiable("Jordan Walker #BD-57 Refractor")                 # a number
    assert sales.identifiable("Topps Chrome Rookie Autographs Red Refractor Jordan Walker RA-JWA")  # a set


def test_a_vague_sale_prices_nothing():
    con = db.connect()
    now = time.time()
    for i in range(3):
        sales.add_manual(con, str(i + 1), 30.0, 0.0, VAGUE, now=now - (i + 1) * 86400)
    assert ledger.find(ledger.groups(con), VAGUE) is None
    assert sales.ledger_comps(con, _listing(VAGUE + " NEAR MINT")) == []
    assert sales.relevant(con, [_listing(VAGUE + " NEAR MINT")]) == []
    assert sales.last_sold(sales.with_calls(con), VAGUE + " NEAR MINT") is None
    assert sales.history(con) == []


def test_a_vague_sale_is_still_kept_and_listed():
    con = db.connect()
    sales.add_manual(con, "1", 30.0, 0.0, VAGUE, now=time.time() - 86400)
    assert [r["item_id"] for r in sales.with_calls(con)] == ["1"]
