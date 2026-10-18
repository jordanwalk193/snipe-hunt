"""The mid-tier lane spends its own budgets and gives way to the other lanes."""
from datetime import datetime

from snipehunt import config, db, midtier, usage


def raw(n, title, price=30.0, auction=False):
    out = {"legacyItemId": f"90000000{n:04d}", "title": title,
           "buyingOptions": ["AUCTION"] if auction else ["FIXED_PRICE"],
           "price": {"value": str(price), "currency": "USD"},
           "shippingOptions": [{"shippingCost": {"value": "1.00", "currency": "USD"}}],
           "seller": {"username": "made-up", "feedbackScore": 900}, "categoryId": "261328",
           "image": {"imageUrl": "x"}, "additionalImages": [{}, {}]}
    if auction:
        out.update(currentBidPrice={"value": str(price), "currency": "USD"}, bidCount=0,
                   itemEndDate="2099-01-01T08:00:00Z")
    return out


class FakeEbay:
    def __init__(self, found):
        self.found, self.searched, self.read, self.lane = found, [], [], ""

    def search(self, q, flt, sort, limit=100, offset=0, category=None):
        self.searched.append((self.lane, category))
        return self.found if q == "jordon walker" else []

    def item(self, item_id, lane="item"):
        from snipehunt import ebay_search
        self.read.append(item_id)
        ebay_search._count_call(lane)
        return {"localizedAspects": [{"name": "Print Run", "value": "150"}],
                "additionalImages": [{}, {}]}


def typo_listings(n):
    return [raw(i, f"2021 Bowman Chrome Jordon Walker Refractor #{i}") for i in range(n)]


def test_item_reads_stop_at_the_daily_budget(monkeypatch):
    con = db.connect()
    profile_cfg = dict(midtier.profile_config(), getitem_daily_budget=2)
    monkeypatch.setattr(midtier, "profile_config", lambda: profile_cfg)
    ebay = FakeEbay(typo_listings(5))
    found, stats = midtier.hunt(con, ebay)
    assert len(ebay.read) == 2 and stats["enriched"] == 2
    assert len(found) == 5                     # the rest are judged on their titles alone
    assert all(cat == "261328" for _, cat in ebay.searched)


def test_searches_stop_at_their_daily_budget(monkeypatch):
    con = db.connect()
    db.bump(con, "ebay_lane:midtier", 400, datetime.now(config.TZ).date().isoformat())
    con.commit()
    ebay = FakeEbay(typo_listings(1))
    found, stats = midtier.hunt(con, ebay)
    assert ebay.searched == [] and stats["skipped_budget"] > 0 and found == []


def test_the_other_lanes_keep_priority(monkeypatch):
    con = db.connect()
    db.bump(con, "ebay_calls:hourly", 4001, datetime.now(config.TZ).date().isoformat())
    con.commit()
    ebay = FakeEbay(typo_listings(1))
    found, _ = midtier.hunt(con, ebay)
    assert ebay.searched == [] and found == []
    assert usage.ebay_allowed("core", con)     # the main searches still run


def test_a_listing_with_nothing_wrong_is_not_read_or_sent(monkeypatch):
    con = db.connect()
    ebay = FakeEbay([raw(1, "2021 Bowman Chrome Prospects Jordan Walker Atomic Refractor")])
    monkeypatch.setattr(midtier, "words", lambda: [("name", "jordon-walker", "jordon walker")])
    found, stats = midtier.hunt(con, ebay)
    assert ebay.read == [] and found == [] and stats["in_band"] == 1


def test_junk_and_out_of_band_listings_are_dropped_before_any_read():
    con = db.connect()
    ebay = FakeEbay([raw(1, "Jordon Walker reprint card"), raw(2, "Jordon Walker RC", 95.0),
                     raw(3, "Jordon Walker RC", 9.0)])
    found, stats = midtier.hunt(con, ebay)
    assert ebay.read == [] and found == [] and stats["junk"] == 1


def test_the_lane_is_off_without_its_profile_block(monkeypatch):
    monkeypatch.setattr(midtier, "profile_config", lambda: None)
    ebay = FakeEbay(typo_listings(1))
    found, stats = midtier.hunt(db.connect(), ebay)
    assert found == [] and stats["searches"] == 0 and ebay.searched == []


def test_home_assistant_can_switch_it_off_and_change_the_ratio():
    from snipehunt import settings
    con = db.connect()
    settings.update(con, {"midtier_enabled": False, "midtier_deal_ratio": 0.5})
    cfg = midtier.config_now(con)
    assert cfg["enabled"] is False and cfg["deal_ratio"] == 0.5
    ebay = FakeEbay(typo_listings(1))
    assert midtier.hunt(con, ebay)[0] == [] and ebay.searched == []
