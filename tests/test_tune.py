import pytest

from snipehunt import db, settings, tune

NOW = 1_800_000_000.0
DAY = 86400


def steady(value=20.0, sample=30):
    """A metric that never moves."""
    return lambda name, start, end: (value, sample)


def test_the_tunables_stay_inside_what_the_bot_may_adjust():
    for name, spec in settings.TUNABLES.items():
        assert name in settings.DEFAULTS and name in settings.RANGES
        assert spec["min"] <= settings.DEFAULTS[name] <= spec["max"] and spec["step"] > 0
        assert spec["better"] in ("higher", "lower")
        # never the read-only rules, quiet hours, alert switches, credentials or Gixen sign-in
        assert not any(word in name for word in settings.NEVER_TUNABLE)
    assert set(settings.TUNABLES) == {"edge_min_pct", "min_overlap", "track_off",
                                      "price_lookups", "fast_min_price", "yellow_max_total"}


@pytest.mark.parametrize("proposal, why", [
    ({"name": "quiet_start", "value": 1}, "not a tunable"),
    ({"name": "alerts_enabled", "value": 0}, "not a tunable"),
    ({"name": "gixen_relogin_minutes", "value": 0}, "not a tunable"),
    ({"name": "edge_min_pct", "value": 45}, "outside 15 to 40"),
    ({"name": "edge_min_pct", "value": 10}, "outside 15 to 40"),
    ({"name": "edge_min_pct", "value": 35}, "more than one step"),
    ({"name": "edge_min_pct", "value": 25}, "already has"),
    ({"name": "edge_min_pct", "value": "30"}, "not a number"),
    ({"name": "edge_min_pct", "value": True}, "not a number"),
    (None, "no change proposed"),
])
def test_a_change_outside_the_bounds_or_the_step_is_refused(proposal, why):
    con = db.connect()
    with pytest.raises(tune.TuneError, match=why):
        tune.check(con, proposal, NOW)
    assert tune.apply(con, proposal, steady(), NOW)["applied"] is False
    assert settings.load(con)["edge_min_pct"] == 25 and tune.view(con) == []


def test_one_change_a_week_is_applied_through_settings_and_logged():
    con = db.connect()
    done = tune.apply(con, {"name": "edge_min_pct", "value": 30, "reason": "edges rarely "
                            "get sniped"}, steady(12.5, 40), NOW)
    assert (done["applied"], done["old"], done["new"], done["baseline"]) == (True, 25, 30, 12.5)
    assert settings.load(con)["edge_min_pct"] == 30
    (row,) = tune.view(con)
    assert (row["name"], row["old"], row["new"], row["metric"], row["baseline"],
            row["status"], row["reason"]) == ("edge_min_pct", 25, 30, "sniped_pct", 12.5,
                                              "watching", "edges rarely get sniped")
    # a second change in the same week is refused, whatever it is
    again = tune.apply(con, {"name": "min_overlap", "value": 0.55}, steady(), NOW + 3 * DAY)
    assert again == {"applied": False,
                     "note": "one change a week: the last one was less than a week ago"}
    # and so is one judged by the same metric as a change still being watched
    tune.apply(con, {"name": "min_overlap", "value": 0.55}, steady(), NOW + 8 * DAY)
    clash = tune.apply(con, {"name": "track_off", "value": 15}, steady(), NOW + 16 * DAY)
    assert "still watching the change to min_overlap" in clash["note"]
    assert settings.load(con)["track_off"] == 10


def test_a_dry_run_and_the_freeze_switch_change_nothing():
    con = db.connect()
    assert tune.apply(con, {"name": "track_off", "value": 15}, steady(), NOW,
                      dry_run=True)["applied"] is False
    settings.update(con, {"self_tuning": False})
    assert tune.apply(con, {"name": "track_off", "value": 15}, steady(), NOW) == {
        "applied": False, "note": "self-tuning is switched off"}
    assert settings.load(con)["track_off"] == 10 and tune.view(con) == []


def after(result, sample=30):
    """A metric that was 20 before the change and is `result` after it."""
    return lambda name, start, end: (20.0, 30) if end <= NOW else (result, sample)


def apply(con, value=0.55):
    return tune.apply(con, {"name": "min_overlap", "value": value}, after(None), NOW)


def test_a_change_that_made_its_metric_worse_is_put_back_after_two_weeks():
    con = db.connect()
    apply(con)                                      # value_gap_pct: lower is better
    assert tune.review(con, after(30.0), NOW + 13 * DAY) == []       # not two weeks yet
    (judged,) = tune.review(con, after(30.0), NOW + 14 * DAY)
    assert (judged["status"], judged["result"]) == ("reverted", 30.0)
    assert settings.load(con)["min_overlap"] == 0.5
    assert tune.view(con)[0]["status"] == "reverted"
    assert tune.review(con, after(30.0), NOW + 21 * DAY) == []       # judged once


def test_a_change_that_did_no_harm_is_kept_and_noise_is_not_harm():
    con = db.connect()
    apply(con)
    (judged,) = tune.review(con, after(22.0), NOW + 14 * DAY)        # 10% worse: inside noise
    assert judged["status"] == "kept" and settings.load(con)["min_overlap"] == 0.55
    assert tune.worse("value_gap_pct", 20.0, 24.0, "lower")           # 20% worse: real
    assert not tune.worse("value_gap_pct", 20.0, 12.0, "lower")
    assert tune.worse("sniped_pct", 30.0, 20.0, "higher")
    assert not tune.worse("sniped_pct", 2.0, 0.0, "higher")           # under the 3-point floor
    assert tune.worse("cost_per_pick", 0.05, 0.08, "lower")           # dollars: a smaller floor


def test_too_few_results_is_inconclusive_and_keeps_watching_for_six_weeks():
    con = db.connect()
    apply(con)
    (judged,) = tune.review(con, after(40.0, sample=3), NOW + 14 * DAY)
    assert (judged["status"], judged["note"]) == ("watching", "inconclusive, keep watching")
    assert settings.load(con)["min_overlap"] == 0.55                  # not put back on a hunch
    (judged,) = tune.review(con, after(40.0, sample=3), NOW + 43 * DAY)
    assert (judged["status"], judged["note"]) == ("kept", "inconclusive after six weeks; kept")


def test_a_setting_changed_by_hand_since_is_never_put_back_and_frozen_means_frozen():
    con = db.connect()
    apply(con)
    settings.update(con, {"self_tuning": False})
    assert tune.review(con, after(30.0), NOW + 14 * DAY) == []        # frozen: not even back
    settings.update(con, {"self_tuning": True, "min_overlap": 0.6})
    (judged,) = tune.review(con, after(30.0), NOW + 14 * DAY)
    assert judged["status"] == "kept" and "by hand" in judged["note"]
    assert settings.load(con)["min_overlap"] == 0.6


def test_the_tunables_are_read_through_settings_not_from_code():
    from snipehunt import sales, score
    from snipehunt.ebay_search import Listing
    con = db.connect()
    item = Listing("1", "t", 5.0, "USD", 0.0, ["FIXED_PRICE"], None, None, "s", "u", None)
    raw = {"tier": "green", "max": 6.06, "value": 7.0, "edge": "mispriced"}
    assert score._pick_dict(item, raw)["edge"] == "mispriced"         # 29% under: an edge at 25
    assert "at least 25% under it" in score.build_prompt([item])
    settings.set_value(con, "edge_min_pct", 30)
    con.commit()
    assert score._pick_dict(item, raw)["edge"] is None
    assert "at least 30% under it" in score.build_prompt([item])
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES "
                "('7', '2023 Topps Chrome Jordan Walker Refractor Sepia Wave RC', 9, 1, 2, 1)")
    thin = Listing("2", "2023 Topps Chrome Refractor", 1.0, "USD", 1.0, ["AUCTION"], 0, None,
                   "s", "u", None)
    assert len(sales.relevant(con, [thin])) == 1                      # 4 of 6 words in common
    settings.set_value(con, "min_overlap", 0.7)
    con.commit()
    assert sales.relevant(con, [thin]) == []
