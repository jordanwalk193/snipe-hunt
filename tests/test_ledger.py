"""The ledger over time: trends, the collection's worth, per-type hours, sell-through,
and lookups only for what the ledger can't price."""
import csv
import time
from datetime import datetime, timedelta

from snipehunt import closes, config, db, ledger, score, snapshot
from snipehunt.ebay_search import Listing

DAY = 86400
CARD = "2023 Topps Chrome Jordan Walker Refractor #209 RC"


def sale(con, item, title, total, days_ago, seller="s", sold=True, hour=13):
    ts = int(datetime.now(config.TZ).replace(hour=hour, minute=0, second=0).timestamp() - days_ago * DAY)
    db.touch(con, item, title, is_auction=True, end_ts=ts, shipping=0.0, price=total, seller=seller)
    con.execute("UPDATE seen SET closed_at=? WHERE item_id=?", (ts + 600, item))
    if sold:
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES (?,?,?,?,?,?)",
                    (item, title, total, 0.0, 3, ts))


def test_a_card_sold_often_gets_a_trend_and_its_picks_carry_it():
    con = db.connect()
    for i, (total, ago) in enumerate([(10.0, 80), (11.0, 60), (10.5, 45), (14.0, 20), (15.0, 10), (16.0, 3)]):
        sale(con, f"20000000000{i}", CARD, total, ago)
    for i, (total, ago) in enumerate([(30.0, 50), (24.0, 20), (21.0, 5)]):
        sale(con, f"30000000000{i}", "2024 Bowman Jordan Walker Gold /50", total, ago)
    con.commit()
    gs = ledger.groups(con)
    now = time.time()
    t = ledger.trends(gs, now)
    assert t["cards"] == 2
    assert t["up"][0]["pct"] == 43 and t["up"][0]["n"] == 6      # 15 against 10.5
    assert t["down"][0]["pct"] == -25                              # 22.5 against 30
    picks = [{"item_id": "9", "title": CARD, "card": CARD}, {"item_id": "8", "title": "2019 Leaf Jordan Walker"}]
    ledger.tag(picks, gs, now)
    assert picks[0]["trend"]["dir"] == "up" and "trend" not in picks[1]


def test_the_collection_is_worth_what_the_ledger_last_saw_with_weekly_movers():
    con = db.connect()
    header = config.INVENTORY.read_text().splitlines()[0].split(",")
    row = dict.fromkeys(header, "")
    row.update(id="99999", year="2023", set="Topps Chrome", card="Base", parallel="Refractor", card_no="209",
               status="In binder", price_paid="4.00")
    with config.INVENTORY.open("a") as fh:
        fh.write(",".join(row[h] for h in header) + "\n")
    _, rows = __import__("snipehunt.inventory", fromlist=["read"]).read()
    sale(con, "200000000001", CARD, 12.0, 2)
    con.commit()
    now = datetime.now(config.TZ)
    w = ledger.worth(con, rows, ledger.groups(con), now)
    mine = next(c for c in w["top"] if c["id"] == "99999")
    assert mine["value"] == 12.0 and mine["by"] == "sales" and w["valued"] >= 1
    assert w["value"] >= 12.0 and w["gain"] == round(w["value"] - w["paid"], 2)
    # a week later the same card sold higher: it is a mover against last week
    sale(con, "200000000002", CARD, 18.0, 1)
    con.commit()
    w2 = ledger.worth(con, rows, ledger.groups(con), now + timedelta(days=7))
    mover = next(m for m in w2["movers"] if m["id"] == "99999")
    assert mover["pct"] == 50 and w2["since"] == w["week"]


def test_endings_are_judged_per_card_type_and_slow_sellers_are_named():
    from tests.test_closes import seed
    con = db.connect()
    seed(con)   # 20 daytime base-card sales at 0.75 from small sellers
    # six autos at small sellers in the daytime that closed at full price
    for i in range(6):
        item = f"40000000000{i}"
        sale(con, item, f"2023 Topps Chrome Jordan Walker Auto #{i}", 50.0, i + 1)
        db.save_pick(con, {"item_id": item, "title": "a", "card": "a", "tier": "green", "max": 5.05,
                           "value": 50.0, "is_auction": True, "max_basis": "sales"})
    # a seller whose auctions mostly end unsold
    for i in range(10):
        sale(con, f"50000000000{i}", "2023 Topps Jordan Walker #1", 3.0, i + 1, seller="reserve-guy", sold=i < 2)
    con.commit()
    learned = closes.learn(con)
    noon = datetime.now(config.TZ).replace(hour=13).isoformat(timespec="minutes")
    assert closes.ending(learned, noon)["share"] == 0.75                           # all small sellers
    assert closes.ending(learned, noon, "2024 Bowman Jordan Walker Auto")["type"] == "auto"
    assert closes.ending(learned, noon, "2024 Bowman Jordan Walker Auto")["share"] == 1.0
    assert learned["sells"]["reserve-guy"] == {"n": 10, "pct": 20}
    assert closes.summary(learned)["slow"][0]["seller"] == "reserve-guy"
    pick = {"is_auction": True, "seller": "reserve-guy", "end_time": noon, "value": 10.0, "title": "t"}
    assert closes.annotate(pick, learned)["closes"]["sells_pct"] == 20
    assert "sell only 20% of the time" in closes.line(pick)


def test_lookups_go_only_to_listings_the_ledger_cannot_price(monkeypatch):
    con = db.connect()
    calls = []
    config.PREFERENCES.write_text("Cheap.\n")
    monkeypatch.setattr(score, "ask_claude", lambda prompt, tools=score.WEB_TOOLS:
                        calls.append(prompt) or '{"picks": []}')
    for i in range(3):
        sale(con, f"60000000000{i}", CARD, 12.0 + i, 5 + i)
    con.commit()
    end = (datetime.now(config.TZ) + timedelta(days=1)).isoformat(timespec="minutes")
    priced = Listing("1", CARD, 1.0, "USD", 1.0, ["AUCTION"], 0, end, "s", "u", None)
    unknown = Listing("2", "2025 Topps Jordan Walker Holiday SP", 1.0, "USD", 1.0, ["AUCTION"], 0, end, "s", "u", None)
    score.score([priced, unknown], con=con)
    assert "at most 1 lookups" in calls[-1]
    score.score([priced], con=con)
    assert "no web access" in calls[-1]      # nothing left for Claude to look up


def test_the_snapshot_carries_trends_worth_and_tagged_picks():
    con = db.connect()
    for i, (total, ago) in enumerate([(10.0, 40), (12.0, 20), (16.0, 2)]):
        sale(con, f"70000000000{i}", CARD, total, ago)
    db.touch(con, "700000000009", CARD, is_auction=True, price=1.0, shipping=0.0)
    db.save_pick(con, {"item_id": "700000000009", "title": CARD, "card": CARD, "tier": "green", "max": 5.05,
                       "value": 14.0, "is_auction": True, "price": 1.0, "shipping": 0.0,
                       "end_time": (datetime.now(config.TZ) + timedelta(days=1)).isoformat(timespec="minutes")})
    con.commit()
    snap = snapshot.snapshot(con, datetime.now(config.TZ))
    assert snap["trends"]["up"][0]["pct"] > 0 and "worth" in snap
    assert next(p for p in snap["picks"] if p["item_id"] == "700000000009")["trend"]["dir"] == "up"


def test_the_ledger_sets_the_max_and_says_the_odds(monkeypatch):
    con = db.connect()
    # nine fresh closes of the exact card, delivered 6..14
    for i, total in enumerate([6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]):
        sale(con, f"80000000000{i}", CARD, total, 3 + i)
    con.commit()
    end = (datetime.now(config.TZ) + timedelta(days=1)).isoformat(timespec="minutes")
    item = Listing("1", CARD, 1.0, "USD", 1.0, ["AUCTION"], 0, end, "s", "u", None)
    comps = __import__("snipehunt.sales", fromlist=["ledger_comps"]).ledger_comps(con, item)
    assert len(comps) == 9
    assert score.ledger_max(comps, 1.0) == 6.06          # 25th percentile close $8, less $1 shipping, odd cents under $7
    assert score.win_odds(comps, 8.07) == {"under": 3, "n": 9, "pct": 33}
    pick = score._pick_dict(item, {"tier": "green", "max": 12.12, "value": 12.0, "max_basis": "sales"},
                            comps=comps)
    assert pick["max"] == 6.06 and pick["max_ledger"] == 6.06     # Claude's 12.12 was lowered
    assert pick["win_odds"] == {"under": 2, "n": 9, "pct": 22}      # $7.06 delivered beats 2 of 9
    low = score._pick_dict(item, {"tier": "green", "max": 5.05, "value": 12.0, "max_basis": "sales"},
                           comps=comps)
    assert low["max"] == 5.05 and "max_ledger" not in low           # never raised
    from snipehunt import notify
    assert "under 2 of 9 sales" in notify.format_pick(pick)
    bin_item = Listing("2", CARD, 9.0, "USD", 0.0, ["FIXED_PRICE"], None, None, "s", "u", None)
    bought = score._pick_dict(bin_item, {"tier": "yellow", "max": 9.09, "value": 12.0, "max_basis": "sales"},
                              comps=comps)
    assert bought["win_odds"]["under"] == 4 and "max_ledger" not in bought


def test_a_single_picks_alert_carries_phone_buttons(monkeypatch):
    from snipehunt import notify
    sent = []
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: sent.append(kw["json"]) or
                        type("R", (), {"status_code": 200, "text": ""})())
    monkeypatch.setattr(notify.config, "get", lambda k, d=None: {"HA_URL": "http://ha", "HA_TOKEN": "t",
                                                                  "HA_NOTIFY_SERVICE": "mobile"}.get(k, d))
    pick = {"item_id": "123456789012", "is_auction": True, "url": "https://www.ebay.com/itm/123456789012"}
    notify.send("t", "m", actions=notify.pick_actions(pick))
    assert sent[-1]["data"]["actions"] == [
        {"action": "SNIPE_HUNT_SNIPED_123456789012", "title": "✓ Added to Gixen"},
        {"action": "SNIPE_HUNT_SKIP_123456789012", "title": "Skip"},
        {"action": "URI", "title": "Open ↗", "uri": "https://www.ebay.com/itm/123456789012"}]
    assert notify.pick_actions({"item_id": "x"}) is None
    assert [a["title"] for a in notify.pick_actions({"item_id": "123456789012", "is_auction": False})] == ["Skip"]
