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
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (str(n), title, price, 1.0, 3, int(NOW) - n))
    found = sales.relevant(con, [listing("9", "Sam Example 2023 Topps Chrome Refractor #45 RC")])
    assert [s["price"] for s in found] == [40.0, 4.0]
    assert sales.relevant(con, [listing("9", "2024 Panini Prizm Silver #12")]) == []
    assert sales.relevant(con, [listing("2", "2023 Topps Chrome Sam Example Refractor #45")])[0][
        "item_id"] == "0"   # a listing is never its own comparison


def test_the_scoring_prompt_shows_sold_prices_only_when_there_are_some():
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


def test_the_dashboard_gets_recent_sales_and_how_the_picks_turned_out():
    from datetime import datetime
    con = db.connect()
    now = datetime.fromtimestamp(NOW, config.TZ)
    for item_id, price, sold_at in (("1", 10.0, NOW - 3600), ("2", 30.0, NOW - 7200),
                                    ("3", 5.0, NOW - 30 * 86400)):
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                    "VALUES (?,?,?,?,?,?)", (item_id, "card " + item_id, price, 2.0, 3, sold_at))
    db.save_pick(con, {"item_id": "1", "tier": "fire", "max": 12.12, "value": 15.0})
    db.save_pick(con, {"item_id": "2", "tier": "green", "max": 20.2, "value": 40.0})
    view = sales.market(con, now)
    assert (view["count"], view["count_7d"]) == (3, 2)
    assert (view["calls"], view["under_max"], view["over_max"]) == (2, 1, 1)
    assert view["value_gap_pct"] == 20      # $12 and $32 delivered against $15 and $40
    assert [s["item_id"] for s in view["sold"]] == ["1", "2", "3"]
    assert view["sold"][0]["call"] == {"tier": "fire", "max": 12.12, "value": 15.0,
                                       "under_max": True, "win": None, "under_win": True}
    # the second went past the max of 20.20 but a bid at the going rate would have held
    assert view["sold"][1]["call"]["win"] == 38.07 and view["under_win"] == 2
    assert view["sold"][0]["total"] == 12.0 and view["sold"][2]["call"] is None


def sale(con, item_id, title, price, value=None, max_bid=None, ago=3600):
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                "VALUES (?,?,?,?,?,?)", (item_id, title, price, 0.0, 3, NOW - ago))
    if max_bid is not None:
        db.save_pick(con, {"item_id": item_id, "tier": "green", "max": max_bid, "value": value})


def test_scoring_is_told_when_its_going_rates_have_been_running_off():
    con = db.connect()
    for n in range(7):   # every one sold for half again what the bot thought
        sale(con, str(n), f"card {n}", 15.0, value=10.0, max_bid=6.06, ago=n)
    assert sales.track_record(con) == ""             # too few to say anything
    sale(con, "7", "card 7", 15.0, value=10.0, max_bid=6.06)
    sale(con, "8", "no pick on this one", 99.0)
    line = sales.track_record(con)
    assert "valued $10 to $50, final prices ran 50% more" in line and "last 8 picks" in line
    item = listing("9", "Sam Example 2023 Topps Chrome Refractor #45")
    assert line in score.build_prompt([item], track=line)
    con.execute("DELETE FROM sales")
    for n in range(8):   # close enough: nothing worth a line in a paid prompt
        sale(con, str(n), f"card {n}", 10.5, value=10.0, max_bid=6.06, ago=n)
    assert sales.track_record(con) == ""


def test_price_check_finds_sales_by_the_words_in_the_title():
    con = db.connect()
    sale(con, "1", "2023 Topps Chrome Sam Example Gold Refractor /50 #45", 40.0)
    sale(con, "2", "2023 Topps Chrome Sam Example Gold Refractor /50 #45 PSA 10", 90.0, ago=60)
    sale(con, "3", "2023 Topps Chrome Sam Example Refractor #45", 4.0)
    assert [r["item_id"] for r in sales.search(con, "gold /50")] == ["2", "1"]
    text = sales.price_check(con, "Gold Refractor")
    assert "2 sales · $40.00 to $90.00 with shipping · typical $65.00" in text
    assert "No sold prices on record for 'superfractor'. 3 sales" in sales.price_check(
        con, "superfractor")


def test_the_win_bid_is_the_going_rate_less_shipping_and_only_when_above_the_max():
    assert sales.win_bid(40.0, 2.0, 20.2) == 38.07
    assert sales.win_bid(40.0, None, 20.2) == 35.07          # unknown shipping: assume $5
    assert sales.win_bid(15.0, 2.0, 12.12) is None           # no real gap over the max
    assert sales.win_bid(None, 2.0, 12.12) is None


def test_cards_that_sold_more_than_once_get_a_price_history():
    con = db.connect()
    sale(con, "1", "2023 Topps Chrome Sam Example Gold Refractor 07/50 #45", 40.0, ago=3 * 86400)
    sale(con, "2", "2023 Topps Chrome Sam Example Gold Refractor /50 #45", 46.0, ago=86400)
    sale(con, "3", "2023 Topps Chrome Sam Example Gold Refractor 31/50 #45", 52.0, ago=60)
    sale(con, "4", "2021 Bowman Draft Sam Example 1st Auto PSA 10", 150.0)
    sale(con, "5", "2023 Topps Chrome Sam Example Gold Refractor /50 #45 PSA 10", 140.0)
    (card,) = sales.history(con)        # the graded copy is a different card
    assert (card["n"], card["prices"], card["low"], card["high"], card["last"]) == (
        3, [40.0, 46.0, 52.0], 40.0, 52.0, 52.0)
    last = sales.last_sold(sales.with_calls(con), "Sam Example 2023 Topps Chrome Gold Refractor /50 #45")
    assert (last["total"], last["n"]) == (52.0, 3)
    assert sales.last_sold(sales.with_calls(con), "2024 Panini Prizm Silver #12") is None


def test_a_gixen_snipe_the_search_never_found_still_gets_its_sold_price(monkeypatch):
    from datetime import datetime
    from snipehunt import jobs
    con = db.connect()
    ends = datetime.fromtimestamp(NOW + 1800, config.TZ).isoformat(timespec="minutes")
    db.touch(con, "2", "2023 Topps Chrome Jordan Walker Refractor #45 RC", True, NOW + 900,
             1.25, 3.0, "searchseller")
    snipes = [
        {"item_id": "1", "title": "2023 Bowman Chrome Jordan Walker Blue /150", "seller":
         "dcsports87", "end_time": ends, "max_bid": 6.06, "current_bid": 1.0,
         "status": "SCHEDULED"},
        {"item_id": "2", "title": "2023 Topps Chrome Jordan Walker Refr...", "seller": "x",
         "end_time": ends, "max_bid": 3.03, "current_bid": 1.0, "status": "SCHEDULED"},
        {"item_id": "3", "title": "2023 Prizm Football Tom Brady Silver", "seller": "x",
         "end_time": ends, "max_bid": 9.09, "current_bid": 1.0, "status": "SCHEDULED"}]
    monkeypatch.setitem(config.load(), "GIXEN_USER", "me")
    monkeypatch.setattr(jobs.gixen, "snipes", lambda con: (snipes, None, NOW))
    assert jobs._note_snipes(con, NOW) == 2          # the football card isn't the player's
    rows = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM seen")}
    assert set(rows) == {"1", "2"}
    assert (rows["1"]["is_auction"], rows["1"]["end_ts"], rows["1"]["seller"]) == (
        1, int(NOW + 1800), "dcsports87")
    # what the search recorded is better than Gixen's copy and is kept
    assert (rows["2"]["title"], rows["2"]["end_ts"], rows["2"]["seller"], rows["2"]["price"]) == (
        "2023 Topps Chrome Jordan Walker Refractor #45 RC", int(NOW + 900), "searchseller", 3.0)
    ebay = FakeEbay({"1": ended(4.25, end=NOW + 1800), "2": ended(2.0, end=NOW + 900)})
    assert sales.collect(con, ebay, now=NOW) == 0 and ebay.asked == []   # still running
    assert sales.collect(con, ebay, now=NOW + 3600) == 2
    sale = dict(con.execute("SELECT * FROM sales WHERE item_id='1'").fetchone())
    assert (sale["price"], sale["title"]) == (4.25, "2023 Bowman Chrome Jordan Walker Blue /150")


def test_a_sale_with_no_shipping_stated_is_totalled_like_everything_else():
    from datetime import datetime
    con = db.connect()
    title = "2023 Topps Chrome Jordan Walker Gold Refractor /50 #45"
    for item_id, shipping, value in (("1", None, 20.0), ("2", 1.0, 20.0)):
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                    "VALUES (?,?,10.0,?,3,?)", (item_id, title, shipping, NOW - int(item_id)))
        db.save_pick(con, {"item_id": item_id, "tier": "green", "max": 12.12, "value": value})
    unknown, known = sales.with_calls(con)
    assert (unknown["total"], unknown["estimated"]) == (10.0 + sales.UNKNOWN_SHIPPING, True)
    assert (known["total"], known["estimated"]) == (11.0, False)
    assert unknown["off"] == -0.25               # $15 delivered against a $20 value, not $10
    view = sales.market(con, datetime.fromtimestamp(NOW, config.TZ))
    assert [s["estimated"] for s in view["sold"]] == [True, False]
    assert view["history"][0]["prices"] == [11.0, 15.0] and view["history"][0]["estimated"]
    assert sales.last_sold(sales.with_calls(con), title)["estimated"] is True
    assert "1 of them didn't state shipping; $5 is assumed." in sales.price_check(con, "gold")


def test_card_numbers_are_read_from_the_ways_sellers_write_them():
    number = sales.card_number
    assert number("2020 Bowman Chrome Jordan Walker 1st Bowman BCP-236") == "BCP236"
    assert number("2020 Bowman Draft Chrome Jordan Walker #BDC-57 Refractor") == "BDC57"
    assert number("2023 Topps Now Jordan Walker TN-12 RC") == "TN12"
    assert number("2023 Topps Update Jordan Walker RC US123 Cardinals") == "US123"
    assert number("2023 Topps Chrome Jordan Walker #42 Refractor") == "42"
    assert number("2023 Topps Chrome Jordan Walker Gold #07/50 PSA 10") is None   # a copy number
    assert number("2023-24 Jordan Walker 1st-Bowman PSA-10 GEM MT10 Lot12") is None


def test_comps_are_the_same_grade_and_print_run_with_one_marked_hint_at_most():
    con = db.connect()
    raw = "2020 Bowman Chrome Jordan Walker 1st Bowman Refractor /499 BCP-236"
    sale(con, "1", raw, 30.0, ago=10)
    sale(con, "2", "2020 Bowman Chrome Jordan Walker 1st Bowman Refractor /499 BCP-236 PSA 10",
         120.0, ago=20)
    sale(con, "3", "2020 Bowman Chrome Jordan Walker 1st Bowman Refractor /499 BCP-236 PSA 9",
         60.0, ago=30)
    sale(con, "4", "2020 Bowman Chrome Jordan Walker 1st Bowman Refractor Auto /499 BCP-236",
         300.0, ago=40)                      # signed: never a comp for an unsigned copy
    found = sales.relevant(con, [listing("9", raw + " Cardinals")])
    assert [(s["item_id"], s["hint"]) for s in found] == [("1", False), ("2", True)]
    text = sales.lines(found)
    assert text.splitlines()[0].endswith("BCP-236")
    assert text.splitlines()[1].endswith("PSA 10 (different grade/parallel)")
    # the graded copy is the very card for a PSA 10 listing, and unmarked there
    found = sales.relevant(con, [listing("9", raw + " PSA 10"), listing("8", raw)])
    assert {s["item_id"]: s["hint"] for s in found} == {"1": False, "2": False, "3": True}


def test_a_different_print_run_is_a_hint_and_a_different_card_number_is_no_match():
    con = db.connect()
    sale(con, "1", "2023 Topps Chrome Jordan Walker Refractor /99 #45 RC", 40.0, ago=10)
    sale(con, "2", "2023 Topps Chrome Jordan Walker Refractor /150 #45 RC", 22.0, ago=20)
    sale(con, "3", "2023 Topps Chrome Jordan Walker Refractor /150 #212 RC", 5.0, ago=30)
    found = sales.relevant(con, [listing("9", "2023 Topps Chrome Jordan Walker Refractor "
                                              "12/150 #45 RC")])
    assert [(s["item_id"], s["hint"]) for s in found] == [("1", True), ("2", False)]
    rows = sales.with_calls(con)
    assert sales.last_sold(rows, "2023 Topps Chrome Jordan Walker Refractor /150 #212")[
        "total"] == 5.0                      # the history keeps #45 and #212 apart too
    assert sales.last_sold(rows, "2023 Topps Chrome Jordan Walker Refractor /150 #45 RC")[
        "n"] == 1


def test_the_same_card_number_and_year_match_even_when_the_titles_share_few_words():
    con = db.connect()
    sale(con, "1", "Jordan Walker 2020 Bowman Draft Chrome 1st Bowman Prospect Rookie St Louis "
                   "Top Outfield BD-57", 9.0, ago=10)
    sale(con, "2", "Jordan Walker 2021 Bowman Chrome Prospects Rookie St Louis Top Outfield "
                   "BD-57", 3.0, ago=20)     # the same number in another year's set
    thin = listing("9", "2020 Bowman Draft #BD-57")
    assert [s["item_id"] for s in sales.relevant(con, [thin])] == ["1"]
    assert sales.relevant(con, [listing("9", "2020 Bowman Draft BD-58")]) == []


def fixed(price, sold=0, offers=False, end=None):
    raw = {"buyingOptions": ["FIXED_PRICE"] + (["BEST_OFFER"] if offers else []),
           "price": {"value": str(price), "currency": "USD"},
           "estimatedAvailabilities": [{"estimatedSoldQuantity": sold}],
           "shippingOptions": [{"shippingCost": {"value": "0.99", "currency": "USD"}}]}
    if end:
        raw["itemEndDate"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(end))
    return raw


def test_a_buy_it_now_that_takes_no_offers_is_recorded_at_its_listed_price():
    con = db.connect()
    for item_id in "12345":
        db.touch(con, item_id, f"2023 Topps Chrome Jordan Walker Refractor #4{item_id}", False,
                 None, 0.99, 8.0)
    con.execute("UPDATE seen SET last_seen=?", (NOW - 4 * 3600,))
    db.touch(con, "6", "2023 Topps Chrome Jordan Walker Sepia #46", False, None, 0.99, 8.0)
    con.execute("UPDATE seen SET last_seen=? WHERE item_id='6'", (NOW - 600,))   # in the search
    ebay = FakeEbay({"1": fixed(8.0, sold=1, end=NOW - 7200),      # sold as listed
                     "2": fixed(8.0, sold=1, offers=True),         # may hide an accepted offer
                     "3": fixed(8.0, end=NOW - 7200),              # ended unsold
                     "4": fixed(8.0),                              # still for sale
                     "5": None})                                   # gone from eBay
    assert sales.collect(con, ebay, now=NOW) == 1
    assert sorted(ebay.asked) == ["1", "2", "3", "4", "5"]
    sale_row = dict(con.execute("SELECT * FROM sales").fetchone())
    assert (sale_row["item_id"], sale_row["price"], sale_row["shipping"], sale_row["kind"],
            sale_row["sold_at"]) == ("1", 8.0, 0.99, "bin", int(NOW - 7200))
    # the one still for sale is asked again a day later, the others never
    assert sales.collect(con, ebay, now=NOW + 3600) == 0 and len(ebay.asked) == 5
    ebay.answers["4"] = fixed(8.0, sold=1)
    assert sales.collect(con, ebay, now=NOW + 25 * 3600) == 1
    assert sorted(ebay.asked[5:]) == ["4", "6"]   # by then the sixth has dropped out of the search too
    assert "$8.00 +0.99 ship, BIN | 2023 Topps Chrome" in sales.lines(
        sales.relevant(con, [listing("9", "2023 Topps Chrome Jordan Walker Refractor #41")]))


def test_a_fixed_price_sale_is_a_comp_but_never_a_call_on_the_scorecard():
    con = db.connect()
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at, kind) "
                "VALUES ('1', 'card', 8.0, 1.0, NULL, ?, 'bin')", (NOW,))
    db.save_pick(con, {"item_id": "1", "tier": "green", "max": 9.09, "value": 20.0})
    (row,) = sales.with_calls(con)
    assert (row["kind"], row["call"], row["total"]) == ("bin", None, 9.0)
    assert sales.scorecard([row])["calls"] == 0


def test_an_older_sales_table_gains_the_kind_column_with_auctions_as_the_default():
    import sqlite3
    config.ensure_state_dir()
    old = sqlite3.connect(config.SEEN_DB)
    old.execute("CREATE TABLE sales (item_id TEXT PRIMARY KEY, title TEXT, price REAL NOT NULL, "
                "shipping REAL, bids INTEGER, sold_at INTEGER NOT NULL)")
    old.execute("INSERT INTO sales VALUES ('1', 'card', 5.0, 1.0, 3, ?)", (NOW,))
    old.commit()
    old.close()
    assert sales.with_calls(db.connect())[0]["kind"] == "auction"


def test_the_track_record_is_kept_per_price_band():
    con = db.connect()
    for n in range(8):       # cheap cards sold for less than the bot thought
        sale(con, f"1{n}", f"cheap {n}", 5.0, value=7.0, max_bid=3.03, ago=n)
    for n in range(8):       # mid-priced ones were about right
        sale(con, f"2{n}", f"mid {n}", 20.5, value=20.0, max_bid=12.12, ago=n)
    for n in range(7):       # dear ones ran high, but there are too few to say so yet
        sale(con, f"3{n}", f"dear {n}", 90.0, value=60.0, max_bid=35.35, ago=n)
    line = sales.track_record(con)
    assert "for cards valued under $10, final prices ran 29% less than your \"value\"" in line
    assert "$10 to $50" not in line and "over $50" not in line
    bands = sales.scorecard(sales.with_calls(con))["bands"]
    assert [(b["band"], b["calls"], b["value_bias_pct"]) for b in bands] == [
        ("under $10", 8, -29), ("$10 to $50", 8, 2), ("over $50", 7, 50)]
    sale(con, "37", "dear 7", 90.0, value=60.0, max_bid=35.35)
    assert "over $50, final prices ran 50% more" in sales.track_record(con)
    assert sales.scorecard([])["bands"][0] == {
        "band": "under $10", "calls": 0, "value_gap_pct": None, "value_bias_pct": None}
