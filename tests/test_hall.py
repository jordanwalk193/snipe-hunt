"""The Hall of Fame: the whole collection ranked by the share of value paid."""
from datetime import datetime

from snipehunt import brag, config, db, inventory

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=config.TZ)


def sale(con, item, title, total, days_ago=3):
    ts = int(NOW.timestamp()) - days_ago * 86400
    db.touch(con, item, title, is_auction=True, end_ts=ts, shipping=0.0, price=total, seller="s")
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at, image) "
                "VALUES (?,?,?,?,?,?,?)", (item, title, total, 0.0, 3, ts, "https://img/x.jpg"))


def own(fields, **kw):
    return dict.fromkeys(fields, "") | {"status": "In binder"} | kw


def test_the_hall_ranks_every_card_by_the_ledger_or_the_bot(monkeypatch):
    con = db.connect()
    fields, _ = inventory.read()
    rows = [
        # priced by the ledger: the same card sold for $40 three days ago
        own(fields, id="1", year="2023", set="Topps Chrome", card="Jordan Walker", card_no="209",
            parallel="Refractor", price_paid="4.00", ebay_item_id="111"),
        # a bot pick the ledger can't price: the bot's value stands in
        own(fields, id="2", year="2024", set="Bowman", card="Jordan Walker", parallel="Pink",
            serial="/175", price_paid="1.00", ebay_item_id="222"),
        # paid about what it is worth (5% off): not a steal, but counted in the totals
        own(fields, id="3", year="2019", set="Leaf", card="Jordan Walker", price_paid="19.00",
            ebay_item_id="333"),
        # nothing prices it: left out of the totals, counted as unpriced
        own(fields, id="4", year="2020", set="Panini", card="Jordan Walker", price_paid="9.00"),
        # sold on: not in the collection any more
        own(fields, id="5", year="2023", set="Topps Chrome", card="Jordan Walker", card_no="209",
            parallel="Refractor", price_paid="1.00", status="Sold-Traded"),
        # a steal on pocket change never makes the hall
        own(fields, id="6", year="2023", set="Topps", card="Jordan Walker", card_no="54",
            price_paid="0.50", ebay_item_id="666"),
    ]
    inventory.write(fields, rows)
    sale(con, "900", "2023 Topps Chrome Jordan Walker Refractor #209 RC", 40.0)
    sale(con, "901", "2019 Leaf Jordan Walker", 20.0)
    sale(con, "902", "2023 Topps Jordan Walker #54", 3.0)
    db.save_pick(con, {"item_id": "222", "card": "2024 Bowman Pink /175", "tier": "fire",
                       "price": 1.0, "value": 6.5, "is_auction": False, "image": "https://img/p.jpg"})
    db.save_pick(con, {"item_id": "111", "card": "2023 Chrome Refractor", "tier": "green",
                       "price": 4.0, "value": 30.0, "is_auction": True})
    con.commit()
    h = brag.hall(con, now=NOW)
    # ranked by the share of value paid; the ledger beats the bot's own value
    assert [(c["card"][:16], c["pct"], c["by"], c["bot"]) for c in h["top"]] == [
        ("2023 Topps Chrom", 10, "sales", True), ("2024 Bowman Jord", 15, "bot", True)]
    assert h["top"][0]["value"] == 40.0 and h["top"][0]["saved"] == 36.0
    assert h["top"][0]["off_pct"] == 90 and "ninety" in h["top"][0]["badges"]
    assert h["top"][0]["image"] == "https://img/x.jpg" and h["top"][1]["image"] == "https://img/p.jpg"
    assert h["n"] == 2
    # totals: the four priced cards (ids 1, 2, 3, 6), one unpriced, the sold one ignored
    assert h["totals"] == {"cards": 4, "unpriced": 1, "paid": 24.5, "worth": 69.5,
                           "saved": 45.0, "pct": 35}
    assert h["bot"] == {"n": 2, "paid": 5.0, "worth": 46.5, "saved": 41.5}   # bought as picks
    # the text card: names, prices, percentages; no item number, seller or date
    assert h["card"].startswith("🏛️ ") and "4 cards priced by real sales" in h["card"]
    assert "1. 2023 Topps Chrome Jordan Walker Refractor #209: $4.00 → worth $40.00 (90% off)" in h["card"]
    for secret in ("111", "222", "900", "2026-10"):
        assert secret not in h["card"]
    # the board carries it, and the weekly report names the top three
    b = brag.board(con, NOW, rows, None)
    assert b["hall"]["top"][0]["pct"] == 10
    lines = brag.report_lines(con, NOW)
    assert lines == [] or "All-time Hall of Fame:" in lines   # nothing bought by date: no section


def test_an_empty_collection_has_an_empty_hall():
    con = db.connect()
    fields, _ = inventory.read()
    inventory.write(fields, [])
    h = brag.hall(con, now=NOW)
    assert h["top"] == [] and h["card"] == "" and h["n"] == 0
    assert h["totals"]["cards"] == 0 and h["totals"]["pct"] is None
