"""Win messages carry the binder page; several open orders with one seller get a reminder."""
import json
from datetime import datetime

import pytest

from snipehunt import config, db, known_cards, notify, wins

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=config.TZ)


@pytest.fixture
def known():
    (config.DATA_DIR / known_cards.FILE).write_text(
        (config.EXAMPLES / "known_cards.example.toml").read_text())
    return known_cards.load()


def won(item_id, title, seller="shop-a", date="2026-10-17", source="ebay", type_="won"):
    return {"type": type_, "is_player": True, "item_id": item_id, "title": title,
            "price": 12.12, "shipping": 1.25, "seller": seller, "date": date, "source": source}


def test_binder_line_uses_the_known_year_when_the_title_is_wrong(known):
    line = wins.binder_line("2023 Bowman Chrome FMF-10 Jordan Walker Atomic", known)
    assert line == "Binder: 2021 → pp4–5 (title says 2023; FMF-10 is 2021)"
    title_only = wins.binder_line("2024 Topps Chrome Jordan Walker #12", known)
    assert title_only == "Binder: 2024 → pp12–13 (year from the title: check the back of the card)"
    assert wins.binder_line("Jordan Walker RC", known).startswith("Binder: year unknown → p20 spare")


def test_the_win_message(known):
    text = wins.win_message(won("900000000001", "2023 Bowman Chrome FMF-10 Jordan Walker",
                                source="gixen"), known)
    assert text.split("\n") == ["🏆 Won: 2023 Bowman Chrome FMF-10 Jordan Walker",
                                "$12.12 + $1.25 ship · shop-a · Gixen",
                                "Binder: 2021 → pp4–5 (title says 2023; FMF-10 is 2021)",
                                "900000000001"]


def test_each_win_is_announced_once(monkeypatch, known):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    con = db.connect()
    events = [won("900000000001", "2021 Bowman Chrome FMF-10 Walker"),
              won("900000000001", "2021 Bowman Chrome FMF-10 Walker"),
              dict(won("900000000002", "Larry Walker 1990"), is_player=False)]
    assert wins.announce(con, events) == 1
    assert wins.announce(con, events) == 0
    assert "Binder: 2021" in sent[0][1]


def test_combined_shipping_reminder_once_a_day_per_seller(monkeypatch):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    con = db.connect()
    db.log_mail_events(con, [won("900000000011", "card one"), won("900000000012", "card two"),
                             won("900000000013", "card three", seller="shop-b")])
    con.commit()
    assert wins.remind(con, NOW) == 1
    assert sent[0][1] == "shop-a: 2 wins, wait for one combined invoice before paying."
    assert wins.remind(con, NOW.replace(hour=15)) == 0          # once a day
    assert wins.remind(con, NOW.replace(day=19)) == 1           # and again tomorrow


def test_the_reminder_stops_once_the_orders_are_paid(monkeypatch):
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)
    con = db.connect()
    db.log_mail_events(con, [won("900000000021", "one"), won("900000000022", "two")])
    con.commit()
    assert wins.remind(con, NOW) == 1
    db.log_mail_events(con, [dict(won("900000000021", "one"), type="paid"),
                             dict(won("900000000022", "two"), type="paid")])
    con.commit()
    assert wins.remind(con, NOW.replace(day=19)) == 0
    assert json.loads(db.kv_get(con, wins.REMIND_KEY)) == {}


def test_inventory_rows_awaiting_payment_count_too(monkeypatch):
    path = config.DATA_DIR / "inventory.csv"
    text = path.read_text().rstrip("\n").split("\n")
    text.append("9,2021,Bowman,Base,,BCP-236,,,,,,,,Awaiting payment,3.03,2026-10-16,shop-c,,"
                "900000000031")
    text.append("10,2021,Bowman,Base,,BCP-237,,,,,,,,Won,3.03,2026-10-16,shop-c,,900000000032")
    path.write_text("\n".join(text) + "\n")
    assert wins.due_reminders(db.connect(), NOW) == [
        "shop-c: 2 wins, wait for one combined invoice before paying."]
