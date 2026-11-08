import json
from datetime import datetime, timedelta

from snipehunt import config, db, priority, steals
from snipehunt.ebay_search import Listing

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=config.TZ)
STEALS = """[steals]
max_landed = 25.0
max_landed_auto = 75.0
"""


def item(title, price=10.0, shipping=1.0, item_id="1", auction=False, end=None, match="yes"):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"],
                   bid_count=0 if auction else None, end_time=end, seller="s",
                   url=f"https://ebay.com/itm/{item_id}", image=None, match=match)


def use_example():
    priority.file().write_text((config.EXAMPLES / "casehits.example.md").read_text())


def test_without_the_file_nothing_runs():
    assert priority.read() == []
    assert priority.due(db.connect()) == []
    assert priority.prompt_rule() == ""
    assert priority.view(db.connect(), [], NOW)["n"] == 0


def test_the_example_parses_and_every_row_has_a_search():
    use_example()
    rows = priority.read()
    assert len(rows) >= 5
    assert all(r["search"] and r["kind"] in priority.KINDS for r in rows)
    assert all(r["ceiling"] for r in rows if not r["hype"])
    assert all(r["ceiling"] is None for r in rows if r["hype"])
    assert len({r["key"] for r in rows}) == len(rows)


def test_titles_match_their_row_and_hype_is_recognised():
    use_example()
    kind = lambda t: (priority.entry_for(t) or {}).get("kind")   # noqa: E731
    assert kind("Topps 2023 Chrome Hidden Gems Jordan Walker RC SSP Case Hit") == "Case hit"
    assert kind("2023 Select Color Wheel #CW8 Jordon Walker RC SSP CASE HIT") == "Case hit"   # typo
    assert kind("2026 Topps Holiday #H148 Jordan Walker SSP") == "Hype"
    assert kind("2023 Topps Chrome Jordan Walker #209 Refractor") is None
    # a lot or a custom is never the card
    assert kind("Jordan Walker lot of 5 incl Hidden Gems") is None
    assert kind("Custom Card Art Jordan Walker Hidden Gems 1/1") is None


def test_numbered_terms_need_a_print_run_that_low():
    assert priority.matches("jordan walker holiday /10",
                            "2026 Topps Holiday #H148 Jordan Walker Metallic Stocking SSSP /10")
    assert priority.matches("jordan walker holiday /10", "2026 Topps MLB Holiday #2/10 Jordan Walker Foil")
    assert not priority.matches("jordan walker holiday /10", "2026 Topps Holiday Jordan Walker Metallic")
    assert not priority.matches("jordan walker holiday /10", "2026 Topps Holiday Jordan Walker Blue /99")
    assert priority.ebay_query("jordan walker holiday /10") == "jordan walker holiday"


def test_a_priority_card_lifts_the_steals_ceiling_but_not_the_skip_words(monkeypatch):
    use_example()
    from snipehunt import profile
    prof = profile.parse((config.EXAMPLES / "profile.example.toml").read_text() + STEALS)
    monkeypatch.setattr(profile, "_cache", prof)
    cfg = steals.profile_config()
    assert cfg is not None
    hidden = "Topps 2023 Chrome Hidden Gems Jordan Walker RC SSP Case Hit"
    assert steals.too_dear(item(hidden, 150.0, 5.0), cfg) is None
    assert steals.too_dear(item(hidden, 180.0, 5.0), cfg) == "over the ceiling"
    # skip words still win: a Holiday superfractor is high-end noise
    assert steals.too_dear(item("2026 Topps Holiday Jordan Walker Superfractor 1/1", 20.0, 0.0), cfg) == "high-end"
    # a hype row lifts nothing
    assert steals.too_dear(item("2026 Topps Holiday #H148 Jordan Walker SSP", 30.0, 0.0), cfg) == "over the ceiling"
    assert "Hidden Gems up to $180" in priority.prompt_rule()


def test_searches_rotate_through_the_rows():
    use_example()
    con = db.connect()
    rows = [r for r in priority.read() if not r["hype"]]
    first = priority.due(con, per_run=3)
    second = priority.due(con, per_run=3)
    assert [q for _, q in first[:2]] == ["jordan walker case hit", "jordan walker ssp"] == [q for _, q in second[:2]]
    assert [q for _, q in first[2:]] == [priority.ebay_query(r["search"]) for r in rows[:3]]
    assert [q for _, q in second[2:]] == [priority.ebay_query(r["search"]) for r in rows[3:6]]
    assert all(lbl.startswith("priority-") for lbl, _ in first)


def test_alerts_once_at_or_under_the_ceiling_and_never_for_hype():
    use_example()
    con = db.connect()
    cheap = item("2023 Panini Select Color Wheel #CW8 Jordan Walker SSP", 30.0, 2.0, "11")
    dear = item("2023 Panini Select Color Wheel #CW8 Jordan Walker SSP", 80.0, 2.0, "12")
    hype = item("2026 Topps Holiday #H148 Jordan Walker SSP", 1.0, 1.0, "13")
    ended = item("2023 Bowman's Best HIM Jordan Walker", 5.0, 1.0, "14", auction=True,
                 end=(NOW - timedelta(hours=1)).isoformat())
    unclear = item("2023 Select Color Wheel Walker", 5.0, 1.0, "15", match="unclear")
    found = priority.to_alert(con, [cheap, dear, hype, ended, unclear], NOW)
    assert [i.item_id for i, _ in found] == ["11"]
    db.kv_set(con, priority.SENT_KEY, json.dumps(["11"]))
    assert priority.to_alert(con, [cheap], NOW) == []
    title, body = priority.message(cheap, found[0][1])
    assert title.startswith("🎯 Case hit listed") and "Item 11" in body


def test_a_rows_first_search_is_silent_then_new_copies_alert():
    use_example()
    con = db.connect()
    wheel = next(r["key"] for r in priority.read() if "color-wheel" in r["key"])
    old = item("2023 Panini Select Color Wheel #CW8 Jordan Walker SSP", 30.0, 2.0, "31")
    db.kv_set(con, priority.DUE_KEY, json.dumps([wheel]))
    assert priority.seed(con, priority.to_alert(con, [old], NOW)) == []   # known, not alerted
    assert priority.to_alert(con, [old], NOW) == []
    new = item("2023 Panini Select Color Wheel #CW8 Jordan Walker SSP", 31.0, 2.0, "32")
    assert [i.item_id for i, _ in priority.seed(con, priority.to_alert(con, [new], NOW))] == ["32"]


def test_view_keeps_live_copies_and_counts_owned():
    use_example()
    con = db.connect()
    live = item("Topps 2023 Chrome Hidden Gems Jordan Walker RC SSP", 150.0, 0.0, "21")
    gone = item("Topps 2023 Chrome Hidden Gems Jordan Walker RC SSP", 99.0, 0.0, "22",
                auction=True, end=(NOW - timedelta(minutes=5)).isoformat())
    assert priority.save(con, [live, gone]) == 2
    inv = [{"year": "2023", "set": "Panini Select", "card": "Color Wheel", "parallel": "",
            "card_no": "CW8", "status": "In binder"}]
    v = priority.view(con, inv, NOW)
    row = next(r for r in v["rows"] if r["card"].startswith("2023 Topps Chrome Hidden Gems"))
    assert [c["item_id"] for c in row["live"]] == ["21"] and row["under"] == 1
    wheel = next(r for r in v["rows"] if "Color Wheel" in r["card"])
    assert wheel["owned"] and v["owned_n"] >= 1
