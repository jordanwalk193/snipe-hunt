"""The mid-tier lane end to end: search, read, detect, Claude (faked), decide, alert."""
import json
import time

from snipehunt import config, db, jobs, midtier, notify, score
from test_midtier_budget import FakeEbay, raw


def fake_claude(seen):
    def ask(prompt, tools="", timeout=0):
        seen.append(prompt)
        return json.dumps({"picks": [{
            "item_id": "900000000001", "card": "2021 Bowman Chrome Jordon Walker Atomic /150",
            "tier": "green", "max": 30.0, "max_basis": "sales", "value": 62.0,
            "edge": "mislabeled", "identity": "sure", "why": "typo + hidden /150",
            "comps": "3 sold near $62"}], "not_player": []})
    return ask


def test_a_mislabeled_auction_becomes_one_alert(monkeypatch):
    con = db.connect()
    now = time.time()
    for n, price in enumerate((60.0, 62.0, 64.0)):   # three recent sales of the exact card
        con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at)"
                    " VALUES (?,?,?,0,6,?)", (f"80000000000{n}",
                                              "2021 Bowman Chrome Jordan Walker Atomic /150",
                                              price, now - (n + 1) * 86400))
    con.commit()
    listing = raw(1, "2021 Bowman Chrome Jordon Walker Atomic", price=9.0, auction=True)
    ebay = FakeEbay([listing])
    monkeypatch.setattr(midtier, "EbayClient", lambda con=None: ebay)
    prompts = []
    monkeypatch.setattr(score, "ask_claude", fake_claude(prompts))
    monkeypatch.setitem(config.load(), "NTFY_URL", "https://ntfy.example/topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: False)
    picks, alerted = midtier.run(con, set(), verbose=False)
    assert ebay.read == ["900000000001"]
    assert '"signals": ["typo_name: \\"Jordon\\""' in prompts[0]
    assert '"identity": "unsure"' in prompts[0]                  # the lane's rule is there
    (pick,) = picks
    lane = pick["midtier"]
    assert lane["alert"] and pick["edge"] == "mislabeled" and alerted == 1
    assert lane["value"] == 62.0 and lane["comps_n"] == 3
    assert pick["max"] == 35.35     # 60% of $62 is 37.20, less $1 ship 36.20; 36.36 is over it
    body = sent[0][1].split("\n")
    assert body[0].startswith("🎯 MIDTIER ★★★★") and body[1] == "900000000001"
    assert body[4].startswith('Why: typo_name("Jordon"), hidden_parallel(/150')


def test_an_unsure_card_does_not_alert(monkeypatch):
    con = db.connect()
    ebay = FakeEbay([raw(1, "2021 Bowman Chrome Jordon Walker Atomic", price=9.0, auction=True)])
    monkeypatch.setattr(midtier, "EbayClient", lambda con=None: ebay)
    answer = fake_claude([])

    def unsure(prompt, tools="", timeout=0):
        reply = json.loads(answer(prompt))
        reply["picks"][0]["identity"] = "unsure"
        return json.dumps(reply)
    monkeypatch.setattr(score, "ask_claude", unsure)
    picks, alerted = midtier.run(con, set(), dry_run=True, verbose=False)
    assert picks[0]["midtier"]["reason"] == "card not pinned down" and alerted == 0


def test_the_hourly_run_hands_the_lane_its_listings_and_scores_the_rest(monkeypatch):
    from snipehunt.ebay_search import Listing
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    con = db.connect()
    db.touch(con, "100000000009", "older listing", False)   # not the first run
    con.commit()
    plain = Listing("100000000001", "2023 Topps Jordan Walker RC", 1.0, "USD", 1.0,
                    ["FIXED_PRICE"], None, None, "s", "u", None)
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: [plain])
    lane_ran, main_scored = [], []
    monkeypatch.setattr(midtier, "run", lambda con, skip, save, dry: lane_ran.append(1) or (
        [{"item_id": "100000000001"}], 0))
    monkeypatch.setattr(jobs.score, "score",
                        lambda items, **kw: (main_scored.extend(items) or [], items))
    jobs.hourly(dry_run=True)
    assert lane_ran and main_scored == []     # the lane took it, so the main lanes didn't
