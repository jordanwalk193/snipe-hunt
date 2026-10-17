"""Team mode's free filter: what reaches Claude, and in what order."""
from datetime import datetime, timedelta, timezone

import pytest

from snipehunt import config, db, inventory, profile, sales, shortlist
from snipehunt.ebay_search import Listing

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def team(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))


def item(n, title, price=10.0, ship=1.0, auction=False, bids=None, hours=None, seller="shop1",
         feedback=None):
    who = profile.get()
    verdict, via, player = who.match(title)
    end = (NOW + timedelta(hours=hours)).isoformat() if hours is not None else None
    return Listing(str(700000000000 + n), title, price, "USD", ship,
                   ["AUCTION"] if auction else ["FIXED_PRICE"], bids, end, seller,
                   f"https://www.ebay.com/itm/{700000000000 + n}", None, verdict,
                   seller_feedback_score=feedback, via=via, player=player)


def run(listings, moves=(), rows=()):
    con = db.connect()
    return shortlist.build(con, list(listings), list(moves), list(rows), now=NOW)


def test_plain_base_cards_never_reach_claude():
    kept, funnel, _ = run([item(1, "2023 Topps Cardinals Lars Nootbaar #123"),
                           item(2, "2024 Topps Cardinals Brendan Donovan #99")])
    assert kept == [] and funnel["dropped"]["plain"] == 2 and funnel["found"] == 2


def test_lots_breaks_and_dear_cards_are_dropped():
    kept, funnel, _ = run([
        item(1, "Cardinals Topps lot of 50 cards"),
        item(2, "2024 Topps Chrome Cardinals team break random"),
        item(3, "Cardinals Topps Masyn Winn auto /5", price=900.0),
    ])
    assert kept == []
    assert funnel["dropped"]["lot"] == 1 and funnel["dropped"]["not_a_card"] == 1
    assert funnel["dropped"]["too_dear"] == 1


def test_lots_come_back_when_the_setting_says_so():
    db.connect().close()
    from snipehunt import settings
    settings.update(db.connect(), {"team_lots": True})
    kept, _, _ = run([item(1, "Cardinals Topps Masyn Winn RC auto lot of 5 cards")])
    assert len(kept) == 1


def test_steal_evidence_outranks_flash():
    con = db.connect()
    for n, price in ((1, 40.0), (2, 44.0), (3, 38.0)):   # the same card has sold for ~$40
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                    "VALUES (?,?,?,?,?,?)", (f"s{n}", "2023 Topps Chrome Masyn Winn Cardinals "
                    "Refractor RC #87", price, 1.0, 5, 1_700_000_000 + n))
    con.commit()
    steal = item(1, "2023 Topps Chrome Masyn Winn Cardinals Refractor RC #87", price=12.0)
    flashy = item(2, "2022 Topps Chrome Bob Gibson Cardinals auto /99", price=60.0)
    kept, _, _ = run([flashy, steal])
    assert [lk.item.item_id for lk in kept] == [steal.item_id, flashy.item_id]
    assert kept[0].under_pct >= 65 and "% under a recorded sale" in kept[0].reasons[0]


def test_price_cuts_sleepers_and_typos_are_worth_a_look():
    cut = item(1, "2023 Topps Cardinals Lars Nootbaar #123", price=4.0)
    sleeper = item(2, "2023 Topps Cardinals Ivan Herrera #44", price=0.99, auction=True,
                   bids=0, hours=2)
    typo = item(3, "Ozzie Smith Cardnals Topps 1985 #44")
    moves = [{"item_id": cut.item_id, "kind": "drop", "cut_pct": 40}]
    kept, funnel, _ = run([cut, sleeper, typo], moves)
    reasons = {lk.item.item_id: lk.reasons for lk in kept}
    assert "price cut 40%" in reasons[cut.item_id]
    assert "ending soon, no bidders" in reasons[sleeper.item_id]
    assert "misspelled title" in reasons[typo.item_id]


def test_cheap_buy_it_nows_need_a_strong_reason():
    kept, funnel, _ = run([item(1, "2024 Topps Cardinals Masyn Winn Refractor /99 #50", price=1.0)])
    assert kept == [] and funnel["dropped"]["too_cheap"] == 1
    # an auction is never floored: a $1 auction can still be the steal
    kept, _, _ = run([item(2, "2024 Topps Cardinals Masyn Winn Refractor /99 #50", price=1.0,
                           auction=True, bids=3, hours=30)])
    assert len(kept) == 1
    # and a cheap copy of a good card is what a frugal collection is built from
    kept, _, _ = run([item(3, "2024 Topps Cardinals Masyn Winn auto #50", price=1.0)])
    assert len(kept) == 1


def test_a_rookie_or_roster_card_alone_is_not_worth_a_call():
    kept, funnel, _ = run([item(1, "2024 Topps Cardinals Masyn Winn RC #50")])
    assert kept == [] and funnel["dropped"]["plain"] == 1


def test_a_bare_nickname_ranks_far_below_a_sure_match():
    sure = item(1, "2024 Topps Cardinals Masyn Winn auto")
    bare = item(2, "Cardinals Kirk auto /25")                # likely the football team
    kept, _, _ = run([bare, sure])
    assert kept[0].item.item_id == sure.item_id


def test_owned_cards_are_skipped():
    rows = [{"year": "2024", "card_no": "#50", "player": "Masyn Winn", "parallel": "Refractor",
             "serial": "/99", "auto": "", "graded": "", "status": "In binder"}]
    owned = item(1, "2024 Topps Cardinals Masyn Winn RC #50 Refractor /99")
    gold = item(2, "2024 Topps Cardinals Masyn Winn RC #50 Gold Refractor /50")
    other = item(3, "2024 Topps Cardinals Masyn Winn RC #51 Refractor /99")
    kept, funnel, _ = run([owned, gold, other], rows=rows)
    assert funnel["dropped"]["owned"] == 1
    assert {lk.item.item_id for lk in kept} == {gold.item_id, other.item_id}


def test_combo_cards_need_someone_on_the_roster():
    kept, funnel, _ = run([item(1, "Cardinals Cubs dual auto Topps /25"),
                           item(2, "Masyn Winn Cardinals Cubs dual auto Topps /25")])
    assert funnel["dropped"]["combo"] == 1
    assert "combo card" in kept[0].reasons


def test_roster_order_and_new_sellers():
    first = item(1, "Jordan Walker Cardinals Topps auto #1")
    later = item(2, "Nolan Arenado Cardinals Topps auto #2")
    risky = item(3, "Jordan Walker Cardinals Topps auto /50", price=80.0, feedback=4)
    kept, _, _ = run([later, first, risky])
    pts = {lk.item.item_id: lk for lk in kept}
    assert pts[first.item_id].points > pts[later.item_id].points
    assert "new seller" in pts[risky.item_id].reasons


def test_sellers_with_several_cards_make_a_bundle():
    cards = [item(n, f"Cardinals Topps Masyn Winn RC refractor /99 #{n}", ship=4.0, seller="bulkshop")
             for n in range(1, 5)]
    _, _, bundles = run(cards)
    assert bundles[0]["seller"] == "bulkshop" and bundles[0]["count"] == 4
    assert bundles[0]["shipping_saved"] == 12.0


def test_only_shortlisted_listings_get_their_sale_looked_up():
    con = db.connect()
    good, plain = item(1, "Cardinals Topps Masyn Winn auto"), item(2, "Cardinals Topps #9")
    for i in (good, plain):
        db.touch(con, i.item_id, i.title, True, 1_000.0, 1.0)
    kept, _, _ = shortlist.build(con, [good, plain], [], [], now=NOW)
    shortlist.mark_tracked(con, kept, [good, plain])
    due = {r["item_id"] for r in sales._due(con, 2_000_000_000.0)}
    assert due == {good.item_id}


def test_untracked_listings_are_forgotten_after_a_month():
    con = db.connect()
    db.touch(con, "1", "Cardinals Topps #9")
    con.execute("UPDATE seen SET tracked=0, last_seen=0")
    assert db.prune_untracked(con) == 1


def test_owns_is_sure_or_says_no():
    rows = [{"year": "2023", "card_no": "BCP-12", "parallel": "Refractor", "serial": "/499",
             "auto": "", "graded": "", "status": "Delivered"}]
    assert inventory.owns(rows, "2023 Bowman Chrome BCP-12 Refractor /499 Winn")
    assert not inventory.owns(rows, "2023 Bowman Chrome BCP-12 Refractor /499 Winn auto")
    assert not inventory.owns(rows, "2023 Bowman Chrome Refractor /499 Winn")   # no number
    rows[0]["status"] = "Sold-Traded"
    assert not inventory.owns(rows, "2023 Bowman Chrome BCP-12 Refractor /499 Winn")


def test_the_sales_index_finds_the_same_card_only():
    con = db.connect()
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) "
                "VALUES ('s1','2023 Topps Chrome Winn Refractor #87',30,0,3,1)")
    sold = sales.index(con)
    assert sales.under_sold(sold, item(1, "2023 Topps Chrome Winn Refractor #87", 15, 0)) == 50
    assert sales.under_sold(sold, item(2, "2023 Topps Chrome Winn Refractor #88", 15, 0)) is None
    assert sales.under_sold(sold, item(3, "2023 Topps Chrome Winn Refractor #87 PSA 10", 15, 0)) is None
    assert sales.under_sold(sold, item(4, "2023 Topps Chrome Winn Refractor #87", 45, 0)) is None
