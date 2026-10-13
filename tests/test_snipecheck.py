import json

from snipehunt import db, score, snipecheck

NOW = 1_800_000_000.0


def snipe(item_id, amount, bid=1.0, title="2023 Topps Chrome Jordan Walker Gold Refractor /50"):
    return {"item_id": item_id, "max_bid": amount, "current_bid": bid, "title": title,
            "end_time": "2099-01-01T21:00-05:00"}


def test_an_amount_is_judged_against_the_going_rate_with_shipping():
    rate = {"value": 40.0, "basis": "sales", "note": "three sold near $40"}
    verdicts = {amount: snipecheck.judge(snipe("1", amount), rate, 2.0)["verdict"]
                for amount in (45.45, 35.35, 25.25, 12.12)}
    assert verdicts == {45.45: "over", 35.35: "fair", 25.25: "steal", 12.12: "long_shot"}
    low = snipecheck.judge(snipe("1", 12.12), rate, 2.0)
    assert (low["pct"], low["win"], low["value"]) == (65, 38.07, 40.0)
    assert snipecheck.judge(snipe("1", 12.12), None, 2.0)["verdict"] == "unknown"
    past = snipecheck.judge(snipe("1", 12.12, bid=14.0), rate, 2.0)
    assert past["verdict"] == "outbid" and past["room"] is True      # $16 on a $40 card


def test_the_going_rate_comes_from_the_pick_then_a_sale_then_one_appraisal(monkeypatch):
    con = db.connect()
    db.save_pick(con, {"item_id": "1", "tier": "fire", "max": 20.2, "value": 40.0,
                       "max_basis": "sales", "comps": "sold $38 to $42"})
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at) VALUES "
                "('9', '2023 Topps Chrome Jordan Walker Blue Refractor /150', 9.0, 1.0, 4, ?)",
                (NOW,))
    mine = [snipe("1", 25.25), snipe("2", 3.03, title="2023 Topps Chrome Jordan Walker Blue "
                                                      "Refractor 12/150"),
            snipe("3", 8.08, title="2021 Leaf Flash Jordan Walker Purple Auto /75")]
    checks, summary = snipecheck.check(con, mine)
    assert [(c["verdict"], c["basis"]) for c in checks] == [
        ("steal", "sales"), ("steal", "sold"), ("unknown", None)]
    assert (summary["steal"], summary["unknown"], summary["worth"]) == (2, 1, 50.0)

    asked = []

    def claude(prompt, tools=""):
        asked.append(prompt)
        return json.dumps({"values": [
            {"item_id": "3", "value": 30, "basis": "estimate", "note": "raw autos run $25-35"},
            {"item_id": "777", "value": 5}]})          # not one we asked about: ignored
    monkeypatch.setattr(score, "ask_claude", claude)
    assert snipecheck.appraise(con, mine, NOW) == 1
    assert "Purple Auto /75" in asked[0] and "Gold Refractor" not in asked[0]
    assert snipecheck.appraise(con, mine, NOW) == 0 and len(asked) == 1     # paid for once
    checks, summary = snipecheck.check(con, mine)
    assert (checks[2]["verdict"], checks[2]["value"], checks[2]["basis"]) == (
        "long_shot", 30.0, "estimate")
    assert summary["unknown"] == 0 and summary["committed"] == 36.36
