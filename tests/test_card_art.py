import time

from snipehunt import card_art, db, snapshot


class FakeClient:
    def __init__(self, pics):
        self.pics, self.asked = pics, []

    def item(self, item_id, lane="item", fresh=False):
        self.asked.append(item_id)
        url = self.pics.get(item_id)
        return {"image": {"imageUrl": url}} if url else None


def test_owned_cards_get_their_own_listing_photo_and_misses_wait_a_week():
    con = db.connect()
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at, image) VALUES (?,?,?,?,?,?,?)",
                ("111", "2023 Topps Chrome Sample Player #150", 2.0, 1.0, 1, time.time(), "https://i.ebayimg.com/x/s-l225.jpg"))
    rows = [{"ebay_item_id": "111"}, {"ebay_item_id": "222"}, {"ebay_item_id": "333"}, {"ebay_item_id": ""}]
    client = FakeClient({"222": "https://i.ebayimg.com/y/s-l1600.jpg"})
    assert card_art.fill(con, rows, client) == 1
    assert sorted(client.asked) == ["222", "333"]                  # 111 is in the ledger already
    art = card_art.known(con)
    assert card_art.picture(rows[1], art, None) == ("https://i.ebayimg.com/y/s-l500.jpg", "own")
    assert card_art.picture(rows[0], art, None) == ("https://i.ebayimg.com/x/s-l500.jpg", "own")
    client.asked.clear()
    card_art.fill(con, rows, client)
    assert client.asked == []                                       # 333's miss is cached
    card_art.fill(con, rows, client, now=time.time() + 8 * 86400)
    assert client.asked == ["333"]                                  # ...and retried after a week


def test_binder_rows_carry_a_picture_field():
    out = snapshot.cards([{"id": "1", "year": "2023", "set": "Topps", "card": "Base", "ebay_item_id": "9"}],
                         [], {"9": "https://i.ebayimg.com/z/s-l1600.jpg"})
    assert out[0]["i"] == "https://i.ebayimg.com/z/s-l500.jpg" and out[0]["if"] == "own"


def test_binder_rows_carry_the_ledger_sales_of_the_same_card():
    g = {"image": None, "sales": [{"sold_at": 1794454290, "total": 12.345, "item_id": "5"},
                                  {"sold_at": 1794000000, "total": 9.0, "item_id": "6"}]}
    from snipehunt import ledger
    import pytest
    mp = pytest.MonkeyPatch(); mp.setattr(ledger, "match_row", lambda gs, r: g)
    try:
        out = snapshot.cards([{"id": "1", "year": "2023", "set": "Topps", "card": "Base"}], [g], {})
    finally:
        mp.undo()
    assert out[0]["h"][0][1:] == [12.35, "5"] and len(out[0]["h"]) == 2 and out[0]["h"][0][0].startswith("2026-")


def test_lost_auctions_show_what_they_went_for_and_your_snipe(monkeypatch):
    from datetime import datetime
    from snipehunt import config, report
    con = db.connect()
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at, image) VALUES (?,?,?,?,?,?,?)",
                ("77", "x", 20.0, 4.0, 9, time.time(), "https://i.ebayimg.com/a/s-l1600.jpg"))
    con.execute("INSERT INTO snipe_log (item_id, title, my_max, logged_at) VALUES (?,?,?,?)", ("77", "x", 11.5, time.time()))
    monkeypatch.setattr(report, "wins_and_losses", lambda ev: ([], [{"item_id": "77", "title": "x", "date": "2026-11-11"}]))
    lost = snapshot.record(con, datetime.now(config.TZ))["recent_losses"][0]
    assert lost["sold"] == 24.0 and lost["my_max"] == 11.5 and lost["image"].endswith("s-l300.jpg")
