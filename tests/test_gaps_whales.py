"""Gaps: what has sold that you don't own. Whales: what the big money goes for."""
from datetime import datetime

from snipehunt import config, db, gaps, ledger, whales

_ids = iter(range(200000000000, 200000009999))
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=config.TZ)
TS = int(NOW.timestamp()) - 86400


def sale(con, title, total, kind="auction", days_ago=1):
    item = str(next(_ids))
    ts = TS - days_ago * 86400
    db.touch(con, item, title, is_auction=kind == "auction", end_ts=ts, shipping=0.0, price=total, seller="s")
    con.execute("UPDATE seen SET closed_at=? WHERE item_id=?", (ts + 60, item))
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at, kind) VALUES (?,?,?,?,?,?,?)",
                (item, title, total, 0.0, 2, ts, kind))
    return item


def live(con, title, price, auction=False):
    item = str(next(_ids))
    db.touch(con, item, title, is_auction=auction, end_ts=int(NOW.timestamp()) + 7200, shipping=0.0, price=price, seller="s")
    con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (int(NOW.timestamp()), item))
    return item


def row(**kw):
    base = {"year": "2023", "set": "Topps Chrome", "card": "Base", "parallel": "", "card_no": "209", "serial": "",
            "auto": "", "relic": "", "graded": "", "dupe": "", "status": "In binder", "price_paid": "3"}
    return {**base, **kw}


def market(con):
    for t in (2.0, 2.5, 3.0):
        sale(con, "2023 Topps Chrome Sam Example #209 Rookie", t)                 # owned
    for t in (4.0, 5.0, 4.5, 30.0):
        sale(con, "2023 Topps Chrome Sam Example Refractor #209", t)             # a cheap gap, one overpay
    for t in (80.0, 90.0):
        sale(con, "2020 Bowman Chrome Draft Sam Example 1st Auto #CDA-SE PSA 10", t)   # a slab: not a gap
    for t in (60.0, 70.0):
        sale(con, "2020 Bowman Chrome Draft Sam Example 1st Auto #CDA-SE", t)    # the big gap
    sale(con, "2023 Topps Chrome Sam Example Gold Refractor #209 /50", 25.0)    # sold once: not liquid
    sale(con, "Sam Example 2023 lot of 5 cards", 9.0)                           # a lot: left out
    con.commit()


def test_gaps_count_the_cards_that_sold_and_you_lack():
    con = db.connect()
    market(con)
    gs = ledger.groups(con)
    v = gaps.view(con, [row()], gs, NOW)
    assert v["known"] == 4 and v["owned"] == 1 and v["gap"] == 3        # the slab and the lot are not cards
    assert v["pct"] == 25
    assert [e["title"] for e in v["easy"]] == ["2023 Topps Chrome Sam Example Refractor #209"]
    assert v["easy"][0]["median"] == 4.75 and v["easy"][0]["n"] == 4
    assert v["easy_n"] == 1 and v["easy_cost"] == 4.75
    assert v["big"][0]["title"].endswith("#CDA-SE") and v["big"][0]["median"] == 65.0
    years = {y["name"]: y for y in v["years"]}
    assert years["2023"]["known"] == 3 and years["2023"]["owned"] == 1 and years["2023"]["easy_cost"] == 4.75
    assert years["2020"] == {"name": "2020", "known": 1, "owned": 0, "gap": 1, "pct": 0, "typical": 65.0, "easy_cost": 0.0}
    fams = {f["name"]: f for f in v["families"]}
    assert fams["Topps Chrome"]["gap"] == 2 and fams["Bowman Chrome"]["gap"] == 1


def test_a_gap_on_sale_now_shows_where_to_buy_it_but_not_an_opening_bid():
    con = db.connect()
    market(con)
    live(con, "2023 Topps Chrome Sam Example Refractor #209 Cardinals", 0.99, auction=True)
    bin_item = live(con, "2023 Topps Chrome Sam Example Refractor #209 RC", 3.25)
    con.commit()
    gs = ledger.groups(con)
    v = gaps.view(con, [row()], gs, NOW)
    assert v["live_n"] == 1
    assert v["easy"][0]["live"]["total"] == 3.25 and v["easy"][0]["live"]["url"].endswith(bin_item)
    assert [b["live"]["total"] for b in v["buyable"]] == [3.25]
    live(con, "2020 Bowman Chrome Draft Sam Example 1st Auto #CDA-SE", 1.0, auction=True)
    con.commit()
    v = gaps.view(con, [row()], ledger.groups(con), NOW)
    assert v["big"][0]["live"]["auction"] is True and v["buyable"][0]["live"]["total"] == 3.25


def test_family_names_the_product_most_specific_first():
    assert gaps.family("2023 Topps Chrome Update Sam Example") == "Topps Chrome Update"
    assert gaps.family("2023 Topps Chrome Sam Example") == "Topps Chrome"
    assert gaps.family("2023 Topps Allen and Ginter Sam Example") == "Allen & Ginter"
    assert gaps.family("2023 Bowmans Best Sam Example") == "Bowman's Best"
    assert gaps.family("Sam Example rookie card") == "Other"


def test_the_ladder_tiers_and_the_overpays():
    con = db.connect()
    market(con)
    gs = ledger.groups(con)
    w = whales.view(con, [row(), row(card_no="CDA-SE", set="Bowman Chrome Draft", year="2020", auto="Y", price_paid="50")], gs, NOW)
    assert w["n"] == 12                                       # the lot is left out
    tiers = {l["tier"]: l for l in w["ladder"]}
    assert tiers["Plain"]["median"] == 4.0 and tiers["Plain"]["x"] == 1.0
    # a slabbed autograph is an autograph first: the signature is what the money is for
    assert tiers["Autograph"] == {"tier": "Autograph", "n": 4, "median": 75.0, "p75": 80.0, "max": 90.0, "x": 18.8}
    assert "Slab PSA 10" not in tiers and tiers["Numbered"]["n"] == 1
    assert w["top"][0]["total"] == 90.0 and w["top"][0]["tier"] == "Autograph"
    assert w["overpaid_n"] == 1 and w["overpaid"][0]["total"] == 30.0 and w["overpaid"][0]["usual"] == 4.5
    assert w["overpaid"][0]["x"] == 6.7 and w["overpaid_dollars"] == 25.5
    assert w["concentration"]["top_n"] == 1 and w["concentration"]["share"] == round(100 * 90 / sum(
        [2, 2.5, 3, 4, 5, 4.5, 30, 80, 90, 60, 70, 25]))
    assert w["drivers"][0]["family"] == "Bowman Chrome" and w["drivers"][0]["n"] == 4
    assert w["trend"] is None                                 # too few sales to call a trend
    assert w["yours"]["n"] == 1 and w["yours"]["by_tier"] == {"Autograph": 1}
    assert w["yours"]["top"][0]["value"] == 65.0   # the median of its two sales and w["yours"]["paid"] == 50.0


def test_tiers_from_a_title_and_from_an_inventory_row():
    assert whales.tier("2023 Topps Sam Example Superfractor 1/1") == "1 of 1"
    assert whales.tier("2023 Topps Sam Example Auto /99 PSA 10") == "Autograph"
    assert whales.tier("2023 Topps Sam Example #209 PSA 10") == "Slab PSA 10"
    assert whales.tier("2023 Topps Sam Example #209 BGS 9.5") == "Slab, other"
    assert whales.tier("2023 Topps Sam Example Jersey Relic") == "Relic"
    assert whales.tier("2023 Topps Sam Example Gold /50") == "Numbered"
    assert whales.tier("2023 Topps Sam Example #209") == "Plain"
    assert whales.row_tier(row(serial="1/1")) == "1 of 1"
    assert whales.row_tier(row(relic="Y")) == "Relic"
    assert whales.row_tier(row(graded="PSA 9")) == "Slab, other"
    assert whales.row_tier(row(serial="12/99")) == "Numbered"
    assert whales.row_tier(row()) == "Plain"
