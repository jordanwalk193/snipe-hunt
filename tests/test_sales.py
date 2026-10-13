import time

from snipehunt import config, db, sales, score
from snipehunt.ebay_search import Listing

NOW = 1_800_000_000.0


class FakeEbay:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def item(self, item_id):
        self.asked.append(item_id)
        answer = self.answers.get(item_id)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ended(price, sold=1, bids=4, options=("AUCTION",), end=NOW - 3600, title=None):
    raw = {"buyingOptions": list(options), "bidCount": bids,
           "itemEndDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(end)),
           "currentBidPrice": {"value": str(price), "currency": "USD"},
           "estimatedAvailabilities": [{"estimatedSoldQuantity": sold}],
           "shippingOptions": [{"shippingCost": {"value": "1.25", "currency": "USD"}}]}
    if title:
        raw["title"] = title
    return raw


def listing(item_id, title, auction=True):
    return Listing(item_id=item_id, title=title, price=1.0, currency="USD", shipping=1.0,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                   end_time=None, seller="someone", url="", image=None)


def test_a_closed_auction_is_looked_up_once_and_its_price_kept():
    con = db.connect()
    db.touch(con, "1", "2023 Chrome Refractor #45", True, NOW - 3600, 0.99)
    db.touch(con, "2", "2023 Chrome base #45", True, NOW - 3600, 0.99)     # no bids
    db.touch(con, "3", "2023 Chrome Gold /50", True, NOW + 3600, 0.99)     # still running
    db.touch(con, "4", "2023 Chrome Blue /150", False, None, 0.99)         # fixed price
    ebay = FakeEbay({"1": ended(12.5), "2": ended(0.99, sold=0, bids=0)})
    assert sales.collect(con, ebay, now=NOW) == 1
    assert sorted(ebay.asked) == ["1", "2"]
    sale = dict(con.execute("SELECT * FROM sales").fetchone())
    assert (sale["item_id"], sale["price"], sale["shipping"], sale["bids"]) == ("1", 12.5, 1.25, 4)
    assert sales.collect(con, ebay, now=NOW) == 0 and len(ebay.asked) == 2   # never asked twice


def test_an_auction_that_only_just_ended_waits_for_ebay_to_settle():
    con = db.connect()
    db.touch(con, "1", "2023 Chrome Refractor #45", True, NOW - 60, None)
    ebay = FakeEbay({"1": ended(5)})
    assert sales.collect(con, ebay, now=NOW) == 0 and ebay.asked == []


def test_a_failed_lookup_is_tried_again_and_a_vanished_listing_is_not():
    con = db.connect()
    db.touch(con, "1", "2023 Chrome Refractor #45", True, NOW - 3600, None)
    db.touch(con, "2", "2023 Chrome Gold /50", True, NOW - 3600, None)
    ebay = FakeEbay({"1": RuntimeError("eBay 503"), "2": None})
    assert sales.collect(con, ebay, now=NOW) == 0
    ebay.answers["1"] = ended(7)
    assert sales.collect(con, ebay, now=NOW) == 1
    assert ebay.asked == ["1", "2", "1"]


def test_listings_from_before_the_upgrade_are_sorted_out_once_they_drop_off():
    con = db.connect()
    for item_id in ("1", "2", "3"):
        db.touch(con, item_id, "2023 Chrome Refractor #45")   # the old call: no end time
    con.execute("UPDATE seen SET last_seen=?", (NOW - 4 * 3600,))
    ebay = FakeEbay({"1": ended(9), "2": ended(3, end=NOW + 7200),
                     "3": ended(4, options=("FIXED_PRICE", "BEST_OFFER"))})
    assert sales.collect(con, ebay, now=NOW) == 1
    # the one still running is asked again once it has ended, the fixed price never
    ebay.answers["2"] = ended(6, end=NOW + 7200)
    assert sales.collect(con, ebay, now=NOW + 3 * 3600) == 1
    assert ebay.asked == ["1", "2", "3", "2"]


def test_only_sales_of_similar_cards_are_offered_for_a_listing():
    con = db.connect()
    for n, (title, price) in enumerate([
            ("2023 Topps Chrome Sam Example Gold Refractor /50 #45 RC", 40.0),
            ("2021 Bowman Draft Sam Example 1st Bowman Auto PSA 10", 150.0),
            ("2023 Topps Chrome Sam Example Refractor #45", 4.0)]):
        con.execute("INSERT INTO sales VALUES (?,?,?,?,?,?)",
                    (str(n), title, price, 1.0, 3, int(NOW) - n))
    found = sales.relevant(con, [listing("9", "Sam Example 2023 Topps Chrome Refractor #45 RC")])
    assert [s["price"] for s in found] == [40.0, 4.0]
    assert sales.relevant(con, [listing("9", "2024 Panini Prizm Silver #12")]) == []
    assert sales.relevant(con, [listing("2", "2023 Topps Chrome Sam Example Refractor #45")])[0][
        "item_id"] == "0"   # a listing is never its own comparison


def test_the_scoring_prompt_shows_sold_prices_only_when_there_are_some():
    config.PREFERENCES.write_text("Cheap and odd-cent.\n")
    item = listing("9", "Sam Example 2023 Topps Chrome Refractor #45")
    assert "sold prices seen" not in score.build_prompt([item])
    prompt = score.build_prompt([item], sold=[{
        "title": "2023 Topps Chrome Sam Example Refractor #45", "price": 4.0, "shipping": None,
        "bids": 2, "sold_at": int(NOW)}])
    assert "$4.00 ship ?, 2 bids | 2023 Topps Chrome Sam Example Refractor #45" in prompt
    assert prompt.index("sold prices seen (date") < prompt.index("=== listings")


def test_an_older_database_gains_the_new_columns(tmp_path):
    import sqlite3
    config.ensure_state_dir()
    old = sqlite3.connect(config.SEEN_DB)
    old.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, title TEXT, first_seen INTEGER "
                "NOT NULL, last_seen INTEGER NOT NULL, scored_total REAL, scored_at INTEGER, "
                "tier TEXT, alerted_total REAL, alerted_at INTEGER)")
    old.execute("INSERT INTO seen (item_id, title, first_seen, last_seen) VALUES ('1','x',1,1)")
    old.commit()
    old.close()
    con = db.connect()
    db.touch(con, "1", "x", True, NOW, 2.0)
    assert con.execute("SELECT end_ts, shipping FROM seen").fetchone()[:] == (int(NOW), 2.0)
