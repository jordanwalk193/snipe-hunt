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
