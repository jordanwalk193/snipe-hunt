"""The market sweep: every live auction each run, the standing Buy It Nows in price
bands a few pages an hour, and only the ledger's steals going to Claude."""
import json
import time

from snipehunt import config, db, deep, ebay_search, jobs, settings

NAME = "jordan walker"
NOW = 1_800_000_000


def raw(n, title, price="9.99", options=("FIXED_PRICE",)):
    return {"legacyItemId": str(700000000000 + n), "title": title, "buyingOptions": list(options),
            "price": {"value": price, "currency": "USD"}, "seller": {"username": "someone"}}


class FakeClient:
    """Pages keyed by (filter prefix, offset); the whole auction run is one key."""
    def __init__(self, pages):
        self.pages, self.calls, self.lane = pages, [], ""

    def search(self, q, flt, sort, limit=100, offset=0, category=None):
        self.calls.append((flt.split(",")[0] if "AUCTION" in flt else flt, sort, offset, limit))
        key = "auction" if "AUCTION" in flt else flt.split("price:")[1].split(",")[0]
        return self.pages.get((key, offset), [])


def full_page(start, title, options=("FIXED_PRICE",)):
    return [raw(start + i, f"{title} #{i}", options=options) for i in range(deep.PAGE)]


def test_auctions_are_paged_past_the_first_page_until_a_short_one():
    con = db.connect()
    client = FakeClient({
        ("auction", 200): full_page(1000, "2023 Topps Jordan Walker RC", ("AUCTION",)),
        ("auction", 400): [raw(5, "2024 Bowman Jordan Walker auto", options=("AUCTION",)),
                           raw(6, "2024 Bowman Herschel Walker", options=("AUCTION",)),
                           raw(7, "2023 Topps Jordan Walker RC #0", options=("AUCTION",))],
    })
    settings.update(con, {"deep_pages": 0})          # Buy It Nows off: auctions only
    found = deep.search(con, known={"700000001000"}, client=client, now=NOW)
    # the sweep stays off entirely at 0 pages
    assert found == [] and client.calls == []
    settings.update(con, {"deep_pages": 1})
    found = deep.search(con, known={"700000001000"}, client=client, now=NOW)
    auctions = [i for i in found if i.is_auction]
    assert len(auctions) == 201   # a full page less the one already known, plus two on the short page
    assert all(i.queries == ["deep-auctions"] for i in auctions)
    assert "700000000006" not in {i.item_id for i in found}      # another Walker
    # pages 2 and 3; the short page 3 ends the auctions
    assert [c[2] for c in client.calls if c[0] == "buyingOptions:{AUCTION}"] == [200, 400]
    assert client.calls[0][1] == "endingSoonest" and client.calls[0][3] == deep.PAGE


def test_the_buy_it_now_cursor_walks_the_bands_and_comes_back_around():
    con = db.connect()
    settings.update(con, {"deep_pages": 2})
    client = FakeClient({
        ("[0..3]", 0): full_page(2000, "2023 Topps Jordan Walker base"),
        ("[0..3]", 200): [raw(1, "2023 Topps Jordan Walker base #x")],   # short: band done
        ("[3..6]", 0): [raw(2, "2023 Topps Chrome Jordan Walker")],
    })
    found = deep.search(con, set(), client=client, now=NOW)
    assert len(found) == 201 and all(i.queries == ["deep-bin"] for i in found)
    cur = deep.cursor(con)
    assert (cur["band"], cur["offset"], cur["passes"]) == (1, 0, 0)
    assert client.calls[-1][0].endswith("price:[0..3],priceCurrency:USD")
    assert "newlyListed" == client.calls[-1][1] and client.calls[-1][2] == 200
    # the next run starts at the next band
    found = deep.search(con, set(), client=client, now=NOW)
    assert [i.item_id for i in found] == ["700000000002"]
    cur = deep.cursor(con)
    assert (cur["band"], cur["offset"]) == (3, 0)       # band 1 was short, band 2 empty
    # bands 3, 4 and 5 are empty: two pages a run finish them in two runs, a full
    # pass is recorded and the sweep starts over at band 0
    deep.search(con, set(), client=client, now=NOW)
    assert (deep.cursor(con)["band"], deep.cursor(con)["passes"]) == (5, 0)
    deep.search(con, set(), client=client, now=NOW)
    cur = deep.cursor(con)
    assert cur["band"] == 0 and cur["passes"] == 1 and cur["last_pass"] == NOW
    # the run's second page went straight on into band 0 of the next pass
    assert cur["offset"] == 200 and cur["found"] == 402
    assert deep.status(con)["passes"] == 1 and deep.status(con)["band"] == "$0–$3 at 200"
    assert deep.status(con)["on"] is True


def test_a_band_stops_at_ebays_ten_thousand():
    con = db.connect()
    settings.update(con, {"deep_pages": 1})
    db.kv_set(con, deep.CURSOR_KEY, json.dumps({"band": 2, "offset": 9800}))
    client = FakeClient({("[6..10]", 9800): full_page(3000, "2023 Topps Jordan Walker")})
    deep.search(con, set(), client=client, now=NOW)
    cur = deep.cursor(con)
    assert (cur["band"], cur["offset"]) == (3, 0)


def listing(n, title, price, auction=False, match="yes"):
    item = ebay_search.parse_item(raw(n, title, str(price), ("AUCTION",) if auction else ("FIXED_PRICE",)))
    item.match = match
    item.queries = ["deep-bin" if not auction else "deep-auctions"]
    return item


def sold(con, n, title, total, days_ago=10):
    ts = NOW - days_ago * 86400
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES (?,?,?,?,?,?)",
                (str(800000000000 + n), title, total, 0.0, 3, ts))


def test_only_the_ledgers_steals_and_cards_worth_a_look_go_to_claude(monkeypatch):
    con = db.connect()
    card = "2023 Topps Chrome Jordan Walker Refractor #209 RC"
    for n, total in enumerate((20.0, 22.0, 18.0)):
        sold(con, n, card, total)
    con.commit()
    steal = listing(1, card, 8.0)               # 8 + 5 shipping = 13: under 60% of 20
    fair = listing(2, card, 15.0)               # 20 delivered: not a steal
    rare = listing(3, "2024 Bowman Jordan Walker Gold /50", 9.0)        # nothing on record, numbered
    plain = listing(4, "2024 Bowman Jordan Walker base", 9.0)           # nothing on record, plain
    thin = listing(5, "2024 Bowman Walker", 9.0, match="unclear")       # a thin title: where mistakes hide
    auction = listing(6, card, 15.0, auction=True)
    hourly = ebay_search.parse_item(raw(7, card, "15.00"))              # not from the sweep
    assert [deep.worth_scoring(con, i, NOW) for i in (steal, fair, rare, plain, thin, auction, hourly)] == [
        True, False, True, False, True, True, True]
    # at most SCORE_MAX a run, the cheapest first
    many = [listing(100 + n, "2024 Bowman Jordan Walker Gold /50", 30 - n) for n in range(20)]
    picked = deep.pick_fresh(con, many, NOW)
    assert len(picked) == deep.SCORE_MAX and picked[0].price == 11.0


def test_the_hourly_run_keeps_the_sweeps_buy_it_nows_out_of_scoring_unless_worth_it(monkeypatch):
    con = db.connect()
    seen = {"searched": None}
    card = "2023 Topps Chrome Jordan Walker Refractor #209 RC"
    swept = [listing(1, card, 2.0), listing(2, "2024 Bowman Jordan Walker base", 9.0)]
    monkeypatch.setattr(ebay_search, "search_all", lambda verbose=False: [ebay_search.parse_item(raw(9, card, "30.00"))])
    monkeypatch.setattr(jobs.hidden, "search", lambda *a, **k: [])
    monkeypatch.setattr(deep, "search", lambda con, known, **k: swept)
    for n, total in enumerate((20.0, 22.0, 18.0)):
        sold(con, n, card, total)
    con.commit()
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "x")
    monkeypatch.setattr(jobs.score, "score", lambda items, **k: seen.update(searched=[i.item_id for i in items]) or ([], items))
    monkeypatch.setattr(jobs.notify, "deliver", lambda *a, **k: 0)
    jobs.hourly(dry_run=True)
    # the hourly listing and the ledger's steal are scored; the plain $9 card waits
    assert seen["searched"] == ["700000000009", "700000000001"]
