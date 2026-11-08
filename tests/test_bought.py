"""A pick whose very card was bought after it was scored leaves the board and the alerts."""
from datetime import datetime

import pytest

from snipehunt import config, db, inventory, notify, snapshot, snipecheck

NOW = datetime(2026, 10, 30, 18, 0, tzinfo=config.TZ)
BEFORE = int(datetime(2026, 10, 10, 12, 0, tzinfo=config.TZ).timestamp())   # scored before the buy
AFTER = int(datetime(2026, 10, 25, 12, 0, tzinfo=config.TZ).timestamp())    # scored knowing it
ROWS = [
    {"id": "1", "year": "2023", "set": "Topps Chrome", "card": "Base", "parallel": "Base",
     "card_no": "209", "serial": "", "graded": "", "auto": "", "status": "In transit",
     "order_date": "2026-10-20"},
    {"id": "2", "year": "2023", "set": "Topps Now", "card": "Base", "parallel": "Blue",
     "card_no": "54", "serial": "", "graded": "", "auto": "", "status": "Delivered",
     "order_date": "2026-10-20"},
    {"id": "3", "year": "2023", "set": "Topps Now", "card": "Base", "parallel": "Base",
     "card_no": "87", "serial": "", "graded": "", "auto": "", "status": "Awaiting payment",
     "order_date": "2026-10-20"},
]


def pick(title, scored_at=BEFORE, **kw):
    p = {"item_id": "1", "title": title, "card": title, "tier": "green", "price": 4.0,
         "shipping": 1.0, "total": 5.0, "is_auction": False, "url": "https://www.ebay.com/itm/1",
         "seller": "someone", "scored_at": scored_at}
    p.update(kw)
    return p


@pytest.fixture(autouse=True)
def made_up_inventory(monkeypatch):
    monkeypatch.setattr(inventory, "read", lambda: ([], ROWS))


def test_the_very_card_bought_after_scoring_is_bought():
    chrome = "2023 Topps Chrome Jordan Walker #209 RC"
    assert snipecheck.bought(pick(chrome), ROWS)["id"] == "1"
    assert snipecheck.bought(pick(chrome, scored_at=None), ROWS)["id"] == "1"   # unknown: before
    assert snipecheck.bought(pick(chrome, scored_at=AFTER), ROWS) is None     # Claude knew; a better copy
    assert snipecheck.bought(pick("2023 Topps Chrome Jordan Walker #209 Refractor"), ROWS) is None   # he owns the base
    assert snipecheck.bought(pick("2023 Topps Now Jordan Walker #54"), ROWS) is None                 # he owns the Blue
    assert snipecheck.bought(pick("2023 Topps Now Jordan Walker #54 Blue"), ROWS)["id"] == "2"


def test_a_team_name_is_not_a_parallel():
    sox = "2023 TOPPS NOW MLB CARD CARDINALS JORDAN WALKER RED SOX TED WILLIAMS #87"
    assert snipecheck.bought(pick(sox), ROWS)["id"] == "3"
    assert snipecheck.bought(pick("2023 Topps Now Jordan Walker Red #87"), ROWS) is None   # a real Red stays


def test_better_or_different_copies_stay():
    for title in ("2023 Topps Chrome Jordan Walker #209 Auto",
                  "2023 Topps Chrome Jordan Walker #209 PSA 10",
                  "2023 Topps Chrome Jordan Walker #209 Gold /50",
                  "Topps Chrome Jordan Walker #209",             # no year: too vague to call
                  "2023 Topps Chrome Jordan Walker #210"):
        assert snipecheck.bought(pick(title), ROWS) is None, title
    assert snipecheck.bought(pick("2023 Topps Chrome Jordan Walker #209", tier="drop"), ROWS) is None


def test_bought_picks_leave_the_board_and_the_alerts(monkeypatch):
    monkeypatch.setitem(config.load(), "GIXEN_USER", "")
    con = db.connect()
    gone = pick("2023 Topps Chrome Jordan Walker #209 RC", item_id="11")
    kept = pick("2023 Topps Chrome Jordan Walker #210 RC", item_id="12")
    for p in (gone, kept):
        db.touch(con, p["item_id"], p["title"], is_auction=False, price=4.0, shipping=1.0)
        con.execute("UPDATE seen SET last_seen=? WHERE item_id=?", (int(NOW.timestamp()) - 60, p["item_id"]))
        db.save_pick(con, {k: v for k, v in p.items() if k != "scored_at"})
    con.execute("UPDATE picks SET created_at=?", (BEFORE,))
    con.commit()
    assert [p["item_id"] for p in snapshot.live_picks(con, NOW)] == ["12"]
    # a held alert carries no scoring time: it was before, so it is dropped too
    assert notify.deliver(con, [dict(gone), dict(kept)], dry_run=True, now=NOW) == 1
