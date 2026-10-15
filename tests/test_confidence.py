"""How far the bot trusts a pick whose going rate is only a guess, and its seller."""
from snipehunt import db, notify, score, settings
from snipehunt.ebay_search import Listing


def listing(price=55.0, shipping=5.0, auction=False, item_id="296000000001"):
    return Listing(item_id, "2023 Topps Jordon Walker Auto Beckett 10", price, "USD", shipping,
                   ["AUCTION"] if auction else ["FIXED_PRICE", "BEST_OFFER"],
                   0 if auction else None, "2026-10-20T00:00:00-04:00" if auction else None,
                   "newseller", "u", None)


def judged(item=None, **raw):
    return score._pick_dict(item or listing(), {
        "item_id": "296000000001", "tier": "fire", "max": 60.6, "value": 150.0, **raw})


def test_a_guessed_value_caps_fire_at_green_and_stars_at_three():
    guess = judged(max_basis="estimate")
    assert (guess["tier"], guess["deal"]) == ("green", 3)
    sold = judged(max_basis="sales")
    assert (sold["tier"], sold["deal"]) == ("fire", 5)
    wanted = judged(max_basis="estimate", want=True)       # a want-list hit keeps its tier
    assert (wanted["tier"], wanted["deal"]) == ("fire", 5)
    assert judged(max_basis="estimate", want="yes")["tier"] == "green"   # only a real true


def test_a_dear_buy_it_now_priced_from_a_guess_is_shown_but_never_alerted():
    con = db.connect()
    guess = judged(max_basis="estimate")
    assert guess["unverified"] and not notify.worth_alerting(guess)
    assert not judged(max_basis="sales")["unverified"]
    cheap = judged(listing(price=18.0, shipping=4.0), max_basis="estimate", max=20.2)
    assert not cheap["unverified"]                              # $22 is under the $25 cap
    unknown_ship = judged(listing(price=21.0, shipping=None), max_basis="estimate", max=22.2)
    assert unknown_ship["unverified"]                            # 21 + an assumed 5
    auction = judged(listing(price=1.0, auction=True), max_basis="estimate")
    assert not auction["unverified"]                             # auctions alert as before
    settings.update(con, {"estimate_bin_cap": 100})
    assert not judged(max_basis="estimate")["unverified"]
    db.save_pick(con, guess)                                     # still kept for the dashboard


def test_the_caps_hold_after_a_price_change():
    pick = judged(max_basis="estimate")
    again = score.rerate(dict(pick, price=10.0, total=15.0, tier="fire"))
    assert again["tier"] == "green" and again["deal"] <= 3 and not again["unverified"]


def test_the_alert_says_when_a_price_is_a_guess():
    guess = dict(judged(listing(price=1.0, auction=True), max_basis="estimate"))
    lines = notify.format_pick(guess).split("\n")
    assert "Max: 60.60 (guess, no sales found)" in lines
    assert any(ln.startswith("Deal:") and ln.endswith("(guess, no sales found)") for ln in lines)
    sold = notify.format_pick(judged(listing(price=1.0, auction=True), max_basis="sales"))
    assert "Max: 60.60 (sales)" in sold and "guess" not in sold


def test_the_prompt_asks_for_want_list_hits_and_explains_the_cap():
    assert '"want": true if' in score.PROMPT
    prompt = score.build_prompt([listing()])
    assert "won't alert a Buy It Now over $25 priced from a guess" in prompt


def seller_listing(score_=22, pct=100.0, price=55.0):
    item = listing(price=price)
    item.seller_feedback_score, item.seller_feedback_pct = score_, pct
    return item


def test_seller_feedback_is_read_from_the_search_results():
    from snipehunt.ebay_search import parse_item
    raw = {"legacyItemId": "296000000001", "title": "t", "buyingOptions": ["FIXED_PRICE"],
           "price": {"value": "55.00", "currency": "USD"},
           "seller": {"username": "newseller", "feedbackScore": 22,
                      "feedbackPercentage": "100.0"}}
    item = parse_item(raw)
    assert (item.seller_feedback_score, item.seller_feedback_pct) == (22, 100.0)
    del raw["seller"]["feedbackScore"], raw["seller"]["feedbackPercentage"]
    item = parse_item(raw)
    assert (item.seller_feedback_score, item.seller_feedback_pct) == (None, None)
    raw["seller"]["feedbackPercentage"] = "n/a"
    assert parse_item(raw).seller_feedback_pct is None


def test_scoring_sees_seller_feedback_and_is_told_to_be_skeptical():
    prompt = score.build_prompt([seller_listing()])
    assert '"seller_feedback": 22, "seller_pct": 100.0' in prompt
    assert "skeptical of sellers with low feedback (\"seller_feedback\" under about 100)" in prompt
    assert "\"BGS 10\"" in prompt
    assert '"seller_feedback"' not in score._compact(listing())     # not sent when unknown


def test_a_new_seller_caps_a_dear_pick_at_yellow_but_still_alerts_with_a_warning():
    con = db.connect()
    new = judged(seller_listing(), max_basis="sales")
    assert (new["tier"], new["new_seller"], new["capped_from"]) == ("yellow", True, "fire")
    assert notify.worth_alerting(new)
    text = notify.format_pick(new)
    assert "⚠️ new seller (22 feedback) · Seller: newseller · 22 feedback, 100%" in text
    assert text.split("\n")[-1] == "296000000001"
    cheap = judged(seller_listing(price=20.0), max_basis="sales", max=22.2)
    assert not cheap["new_seller"] and cheap["tier"] == "fire"      # $25 total: not over $30
    known = judged(seller_listing(score_=4210, pct=99.6), max_basis="sales")
    assert known["tier"] == "fire" and not known["new_seller"]
    assert "Seller: newseller · 4,210 feedback, 99.6%" in notify.format_pick(known)
    unknown = judged(listing(), max_basis="sales")                     # eBay didn't say
    assert unknown["tier"] == "fire" and not unknown["new_seller"]
    settings.update(con, {"min_seller_feedback": 10})
    assert judged(seller_listing(), max_basis="sales")["tier"] == "fire"


def test_a_new_sellers_pick_gets_its_tier_back_when_the_price_falls_under_30():
    new = judged(seller_listing(), max_basis="sales")
    again = score.rerate(dict(new, price=20.0, shipping=5.0, total=25.0, max=60.6))
    assert again["tier"] == "fire" and not again["new_seller"]
