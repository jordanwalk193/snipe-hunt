"""The pre-filter every lane shares: junk dropped, fake autos flagged, scams held back."""
from snipehunt import config, filters, notify, profile, score
from snipehunt.ebay_search import Listing


def item(title, aspects=None, description=""):
    return Listing("1", title, 20.0, "USD", 1.0, ["FIXED_PRICE"], None, None, "s", "u", None,
                   aspects=aspects or {}, short_description=description)


def test_junk_words_are_dropped_before_scoring():
    kept, dropped = filters.prefilter([
        item("Jordan Walker RP reprint rookie"), item("Jordan Walker custom art card"),
        item("Jordan Walker YOU PICK Topps"), item("Jordan Walker u pick lot"),
        item("2023 Topps Chrome Jordan Walker RC"), item("Jordan Walker RPA patch auto /99")])
    assert [i.title for i in kept] == ["2023 Topps Chrome Jordan Walker RC",
                                       "Jordan Walker RPA patch auto /99"]   # RPA isn't RP
    assert sum(dropped.values()) == 4


def test_junk_words_come_from_the_profile_and_lots_stay_unless_turned_off(monkeypatch):
    text = (config.EXAMPLES / "profile.example.toml").read_text()
    assert filters.junk("Jordan Walker lot of 5 Bowman") is None        # lots allowed
    monkeypatch.setattr(profile, "_cache", profile.parse(text.replace(
        "lots = true", 'lots = false\njunk_words = ["sticker"]')))
    assert filters.junk("Jordan Walker lot of 5 Bowman") == "lot"
    assert filters.junk("Jordan Walker sticker") == "sticker"
    assert filters.junk("Jordan Walker reprint") is None                 # the list replaced


def test_platinum_anniversary_auto_with_autographed_no_is_a_printed_signature():
    pa = item("2025 Topps Chrome Platinum Anniversary Jordan Walker Auto",
              aspects={"Autographed": "No"})
    kept, _ = filters.prefilter([pa])
    assert kept[0].flags == ["fake_auto"]
    assert filters.fake_auto("Jordan Walker auto", specifics={"Autographed": "No"})
    assert filters.fake_auto("Jordan Walker RC", description="pre-printed signature")
    assert filters.fake_auto("2025 Topps Chrome Platinum Anniversary Jordan Walker signed")
    assert filters.fake_auto("2025 Platinum Anniversary Autographs Jordan Walker Auto") is None
    assert filters.fake_auto("2020 Bowman Chrome Jordan Walker Auto") is None


def test_a_flagged_pick_is_never_priced_as_an_auto():
    pa = item("2025 Topps Chrome Platinum Anniversary Jordan Walker Auto")
    filters.flag(pa)
    prompt = score.build_prompt([pa], 0)
    assert "printed signature, not an auto: price it as the unsigned card" in prompt
    assert 'start its "why" with "printed signature, not an auto"' in prompt
    pick = score._pick_dict(pa, {"tier": "fire", "max": 20.0, "value": 80, "why": "cheap auto"})
    assert pick["why"].startswith("printed signature, not an auto")
    assert pick["tier"] == "green" and pick["fake_auto"]
    assert "Watch-outs: printed signature, not an auto" in notify.format_pick(pick)


def test_a_fake_auto_is_compared_with_unsigned_sales(monkeypatch):
    from snipehunt import db, sales
    con = db.connect()
    for n, (title, price) in enumerate([
            ("2025 Topps Chrome Platinum Anniversary Jordan Walker #12", 4.0),
            ("2025 Topps Chrome Platinum Anniversary Jordan Walker #12 Auto", 90.0)]):
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at)"
                    " VALUES (?,?,?,1,3,?)", (str(n + 10), title, price, 1_800_000_000))
    pa = item("2025 Topps Chrome Platinum Anniversary Jordan Walker #12 Auto")
    filters.flag(pa)
    assert [c["price"] for c in sales.ledger_comps(con, pa)] == [4.0]


def test_new_seller_scam_guard():
    assert filters.new_seller_risk(12, 15.0, 60.0)          # under 40% of value, 12 feedback
    assert not filters.new_seller_risk(500, 15.0, 60.0)
    assert not filters.new_seller_risk(12, 30.0, 60.0)       # 50%: not too good
    risky = {"item_id": "1", "card": "x", "tier": "green", "max": 20.0, "price": 15.0,
             "shipping": 0.0, "total": 15.0, "is_auction": False, "value": 60.0,
             "seller_feedback": 12, "max_basis": "estimate"}
    score.apply_caps(risky)
    assert risky["scam_risk"] and not notify.worth_alerting(risky)
    assert "too good for a new seller" in notify.watch_outs(risky)
    backed = dict(risky, max_basis="sales", comps_n=3)
    score.apply_caps(backed)
    assert backed["scam_risk"] and notify.worth_alerting(backed)   # real sales, under $30
    dear = dict(backed, price=32.0, total=32.0, value=90.0, max=33.0)
    score.apply_caps(dear)
    assert dear["new_seller"] and not notify.worth_alerting(dear)  # over SELLER_TRUST_OVER


def test_search_all_results_go_through_the_filter_in_the_hourly_run(monkeypatch):
    from snipehunt import jobs
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    good = Listing("100000000001", "2023 Topps Jordan Walker RC", 1.0, "USD", 1.0,
                   ["FIXED_PRICE"], None, None, "s", "u", None)
    junk = Listing("100000000002", "Jordan Walker reprint RC", 1.0, "USD", 1.0,
                   ["FIXED_PRICE"], None, None, "s", "u", None)
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: [good, junk])
    scored = []
    monkeypatch.setattr(jobs.score, "score", lambda items, **kw: (scored.extend(items) or [], items))
    jobs.hourly(dry_run=True)
    assert [i.item_id for i in scored] == ["100000000001"]
