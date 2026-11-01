"""An inventory row finds its own card in the ledger by its parts (year, number,
parallel, print run, autograph, grade), not by how many words it shares with a title."""
from snipehunt import db, ledger


def sale(con, item, title, total=5.0):
    db.touch(con, item, title, is_auction=True, end_ts=1_700_000_000, shipping=0.0, price=total, seller="s")
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES (?,?,?,?,?,?)",
                (item, title, total, 0.0, 2, 1_700_000_000))


def row(**kw):
    base = {"year": "2023", "set": "Topps Chrome", "card": "Base", "parallel": "", "card_no": "209",
            "serial": "", "auto": "", "graded": "", "status": "In binder"}
    return {**base, **kw}


_ids = iter(range(100000000000, 100000009999))


def ledger_of(*titles):
    con = db.connect()
    for t in titles:
        sale(con, str(next(_ids)), t)
    con.commit()
    return ledger.groups(con)


def test_a_plain_card_is_not_its_refractor_even_in_the_plural():
    gs = ledger_of("2023 Topps Chrome Sam Example Refractors Rookie #209",
                   "2023 Topps Chrome Sam Example Rookie RC #209 Cardinals")
    g = ledger.match_row(gs, row())
    assert g and "Refractor" not in g["title"]
    g = ledger.match_row(gs, row(parallel="Refractor"))
    assert g and "Refractors" in g["title"]


def test_the_number_the_year_and_the_print_run_must_agree():
    gs = ledger_of("2023 Topps Chrome Sam Example Gold Refractor #209 /50",
                   "2024 Topps Chrome Sam Example #209", "2023 Topps Chrome Sam Example #210")
    assert ledger.match_row(gs, row()) is None                       # 2024, #210 and /50 are other cards
    assert ledger.match_row(gs, row(parallel="Gold Refractor", serial="12/50"))["title"].endswith("/50")
    assert ledger.match_row(gs, row(parallel="Gold Refractor")) is None   # the row says no print run


def test_autographs_and_slabs_are_their_own_cards():
    gs = ledger_of("2023 Topps Chrome Sam Example Auto #209", "2023 Topps Chrome Sam Example #209 PSA 10")
    assert ledger.match_row(gs, row()) is None
    assert "Auto" in ledger.match_row(gs, row(auto="Y"))["title"]
    assert "PSA" in ledger.match_row(gs, row(graded="PSA 10"))["title"]


def test_another_product_sharing_the_number_is_not_the_card():
    gs = ledger_of("2023 Bowman Sterling Sam Example Rookie Revelation #18",
                   "2023 Panini Three and Two Sam Example #18")
    assert ledger.match_row(gs, row(set="Bowman Chrome", card_no="18")) is None


def test_an_insert_title_may_leave_out_chrome_when_the_number_says_which_card():
    gs = ledger_of("2022 Bowman Sam Example #VV-13 Virtuosic Vibrations")
    assert ledger.match_row(gs, row(year="2022", set="Bowman Chrome", card="Virtuosic Vibrations insert", card_no="VV-13"))
    # but a plain row without a number can't claim a set it only half names
    assert ledger.match_row(ledger_of("2022 Bowman Sam Example #BP-20 Prospects"),
                            row(year="2022", set="Bowman Heritage", card_no="")) is None


def test_a_row_without_a_number_cannot_claim_a_numbered_card():
    gs = ledger_of("2026 Topps Now Sam Example #451", "2026 Topps Now Sam Example Home Run Robbery")
    g = ledger.match_row(gs, row(year="2026", set="Topps Now", card_no=""))
    assert g and "Robbery" in g["title"]


def test_the_insert_name_must_be_in_the_title():
    gs = ledger_of("2023 Panini Donruss Other Player Crunch Time #CT-15")
    assert ledger.match_row(gs, row(set="Donruss", card="Unleashed insert", card_no="")) is None


def test_worth_and_the_hall_use_the_structured_match(monkeypatch):
    gs = ledger_of("2023 Topps Chrome Sam Example Rookie #209 Cardinals")
    seen = []
    monkeypatch.setattr(ledger, "match_row", lambda g, r: (seen.append(r), gs[0])[1])
    from datetime import datetime
    from snipehunt import config
    out = ledger.worth(db.connect(), [row(price_paid="2")], gs, datetime.now(config.TZ))
    assert seen and out["valued"] == 1


def test_a_parallel_and_its_plain_card_are_two_ledger_cards():
    gs = ledger_of("2023 Topps Chrome Sam Example Refractor #209", "2023 Topps Chrome Sam Example #209",
                   "2023 Topps Chrome Sam Example RC #209 Cardinals")
    assert len(gs) == 2
    assert sorted(len(g["sales"]) for g in gs) == [1, 2]
