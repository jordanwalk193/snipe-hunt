"""Loose searches (results needn't name the player) and the backfill sweep."""
from snipehunt import db, ebay_search, jobs, profile, score

PROFILE = """
collector = "Sam"
[player]
name = "Rick Monday"
sport = "baseball"
team_words = ["dodgers", "cubs"]
wrong_words = ["football", "cyber monday"]
[search]
category_id = ""
loose = ["1976 flag dodger stadium"]
"""


def raw(item_id, title, category="261328", price="9.99"):
    return {"legacyItemId": item_id, "title": title, "price": {"value": price, "currency": "USD"},
            "buyingOptions": ["FIXED_PRICE"], "categoryId": category,
            "itemWebUrl": f"https://www.ebay.com/itm/{item_id}"}


class Fake:
    """eBay that answers each search with its own results, a page at a time."""
    def __init__(self, results: dict[str, list[dict]]):
        self.results, self.asked, self.calls = results, [], 0

    def search(self, q, flt, sort, limit=100, offset=0):
        self.asked.append((self.lane, q, offset))
        self.calls += 1
        return self.results.get(q, [])[offset:offset + limit]


def use(monkeypatch, text=PROFILE):
    monkeypatch.setattr(profile, "_cache", profile.parse(text))


def test_a_loose_search_hands_titles_without_the_name_to_claude(monkeypatch):
    use(monkeypatch)
    assert ("loose-1976-flag-dodger-stadium", "1976 flag dodger stadium",
            ebay_search.ALL_FORMATS, "newlyListed") in ebay_search.queries()
    fake = Fake({"1976 flag dodger stadium": [
        raw("1", "1976 Flag Rescue Dodger Stadium 8x10 Photo"),
        raw("2", "1976 Dodger Stadium flag football program")]})   # a wrong word
    got = {i.item_id: i.match for i in ebay_search.search_all(fake)}
    assert got == {"1": "unclear"}


def test_an_ordinary_search_still_drops_titles_without_the_name(monkeypatch):
    use(monkeypatch)
    fake = Fake({"rick monday": [raw("1", "1976 Dodger Stadium 8x10 Photo"),
                                 raw("2", "Rick Monday 1977 Topps Dodgers")]})
    assert [i.item_id for i in ebay_search.search_all(fake)] == ["2"]


def test_the_backfill_pages_through_everything_and_can_skip_the_cards(monkeypatch):
    use(monkeypatch)
    cards = [raw(str(n), f"Rick Monday {1960 + n % 20} Topps Dodgers card") for n in range(250)]
    cans = [raw(f"c{n}", f"Rick Monday RC Cola can {n}", category="13597") for n in range(120)]
    fake = Fake({"rick monday": cards + cans,
                 "1976 flag dodger stadium": [raw("f", "1976 flag Dodger Stadium photo", "14")]})
    got = ebay_search.backfill_search(fake, pages=5, skip_category="261328")
    ids = {i.item_id for i in got}
    assert len(ids) == 121 and "f" in ids and not ids & {i["legacyItemId"] for i in cards}
    assert [o for lane, q, o in fake.asked if q == "rick monday"] == [0, 200]   # stops when short
    assert len(ebay_search.backfill_search(fake, pages=1)) == 201   # first 200, plus the flag photo


def test_a_backfill_saves_its_picks_with_one_note_and_no_alert_each(monkeypatch):
    use(monkeypatch)
    con = db.connect()
    items = [ebay_search.parse_item(raw(f"c{n}", f"Rick Monday RC Cola can {n}", "13597"))
             for n in range(3)]
    for i in items:
        i.match = "yes"
    monkeypatch.setattr(jobs.config, "require", lambda *names: ["x"] * len(names))
    monkeypatch.setattr(ebay_search, "backfill_search", lambda **kw: items)

    def fake_score(fresh, verbose=False, on_batch=None, con=None):
        picks = [{"item_id": i.item_id, "title": i.title, "card": i.title, "tier": "green",
                  "total": i.total, "max": 3.03, "value": 10.0} for i in fresh[:2]]
        on_batch(fresh, picks)
        return picks, fresh
    monkeypatch.setattr(score, "score", fake_score)
    sent = []
    monkeypatch.setattr(jobs.notify, "configured", lambda: True)
    monkeypatch.setattr(jobs.notify, "send", lambda title, body, **kw: sent.append(title))
    assert jobs.backfill() == "backfill: 3 listings, 3 scored, 2 picks"
    assert sent == ["Snipe Hunt: 2 more picks"]
    assert not db.needs_scoring(con, "c0", items[0].total)
    # already alerted, so the next hourly run doesn't push them one by one
    assert {r[0] for r in con.execute("SELECT item_id FROM seen WHERE alerted_at IS NOT NULL")
            } >= {"c0", "c1"}
    assert jobs.backfill(dry_run=True).startswith("3 listings, 0 would be scored")
