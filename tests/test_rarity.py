from snipehunt import db, rarity
from snipehunt.ebay_search import Listing


def listing(item_id, title, price=5.0, end=None, match="yes"):
    item = Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=1.0,
                   buying_options=["AUCTION"] if end else ["FIXED_PRICE"], bid_count=0,
                   end_time=end, seller="someone", url="u", image=None)
    item.match = match
    return item


def test_the_print_run_is_read_from_the_title():
    runs = {
        "2023 Topps Chrome Sam Example Gold Refractor /50 #45": 50,
        "2023 Topps Dynasty Sam Example Patch Auto 4/5": 5,
        "2023 Chrome Sam Example Superfractor PSA 9": 1,
        "2022 Trinity Sam Example Printing Plate Black Auto": 1,
        "2024 Signature Series Sam Example Auto #1/1": 1,
        "2023 Topps Now #54 Sam Example RC /4462": None,          # a mass print run
        "2023 Topps Chrome Sam Example Refractor #45 PSA 10": None,
        "2023 Finest Sam Example Auto BGS 9.5/10": None,          # a grade, not a serial
        "Sam Example card lot /99 /50 /25": None,                 # not one card
        "Sam Example 2023 rookie listed 10/12/23": None,          # a date
        "2021 Flash Sam Example Purple Auto #/75 & Blue Refractor #/150": 75,
        "2023 Chrome Sam Example Orange 30/25": None,             # not a real serial
    }
    assert {title: rarity.print_run(title) for title in runs} == runs


def test_the_rarest_list_is_fewest_copies_first_then_autographs():
    items = [listing("1", "2023 Chrome Sam Example Gold /50"),
             listing("2", "2023 Chrome Sam Example Red /5"),
             listing("3", "2023 Dynasty Sam Example Patch Auto 2/5", price=90.0),
             listing("4", "2023 Chrome Sam Example Superfractor 1/1", price=900.0),
             listing("5", "2023 Chrome Sam Example base"),
             listing("6", "2023 Chrome Sam Exampel Red 1/1", match="unclear")]
    top = rarity.rarest(items)
    assert [(r["item_id"], r["run"], r["auto"]) for r in top] == [
        ("4", 1, False), ("3", 5, True), ("2", 5, False), ("1", 50, False)]


def test_the_saved_list_drops_auctions_that_have_ended():
    con = db.connect()
    assert rarity.load(con, "2026-10-13T12:00-04:00") == []       # before the first search
    assert rarity.save(con, [
        listing("1", "2023 Chrome Sam Example Red /5", end="2026-10-13T11:00-04:00"),
        listing("2", "2023 Chrome Sam Example Gold /50", end="2026-10-13T21:00-04:00"),
        listing("3", "2023 Chrome Sam Example Orange /25")]) == 3
    assert [r["item_id"] for r in rarity.load(con, "2026-10-13T12:00-04:00")] == ["3", "2"]
