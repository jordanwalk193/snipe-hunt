"""Team mode's eBay searches: what is asked for, paging, and the free title check."""
import pytest

from snipehunt import config, db, ebay_search, profile, settings

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()


@pytest.fixture(autouse=True)
def team(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse(TEAM))


def raw(n: int, title: str, price: str = "9.99") -> dict:
    return {"legacyItemId": str(900000000000 + n), "title": title,
            "buyingOptions": ["FIXED_PRICE"], "price": {"value": price, "currency": "USD"},
            "seller": {"username": "exampleseller"}}


def test_team_queries():
    q = {label: (text, flt, sort) for label, text, flt, sort in ebay_search.queries()}
    assert q["auctions-ending"] == ("cardinals", "buyingOptions:{AUCTION}", "endingSoonest")
    # the newest listings skip the price floor; auctions are never floored
    assert q["newly-listed"][1].endswith(",price:[3..],priceCurrency:USD")
    assert q["auto"][0] == "cardinals auto" and q["ending-auto"][2] == "endingSoonest"
    assert q["roster-jordan-walker"][0] == "jordan walker"
    assert "typo-cardnals" in q
    assert len(q) <= 22   # about twenty eBay calls an hour, before paging


def test_the_floor_follows_the_setting():
    settings.update(db.connect(), {"team_min_price": 5})
    q = {label: flt for label, _, flt, _ in ebay_search.queries()}
    assert ",price:[5..]" in q["newly-listed"]


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def search(self, q, flt, sort, limit=100, offset=0):
        self.calls.append((q, offset, limit))
        return self.pages.get((q, offset), [])


def test_team_search_pages_tags_and_drops(monkeypatch):
    full = [raw(i, f"2023 Topps Cardinals Bob Gibson #{i}") for i in range(200)]
    pages = {("cardinals", 0): full,
             ("cardinals", 200): [raw(500, "Kyler Murray Arizona Cardinals Prizm"),
                                  raw(501, "Masyn Winn Cardinals Topps RC")],
             ("jordan walker", 0): [raw(600, "2023 Topps Chrome Jordan Walker RC")]}
    client = FakeClient(pages)
    found = {i.item_id: i for i in ebay_search.search_all(client)}
    offsets = [o for q, o, _ in client.calls if q == "cardinals"]
    assert offsets.count(200) >= 1 and all(lim == 200 for _, _, lim in client.calls)
    assert "900000000500" not in found                         # the NFL team
    winn = found["900000000501"]
    assert (winn.match, winn.via, winn.player) == ("yes", "nickname", "Masyn Winn")
    walker = found["900000000600"]
    assert (walker.match, walker.via, walker.player) == ("unclear", "roster", "Jordan Walker")


def test_a_short_page_ends_the_paging():
    client = FakeClient({("cardinals", 0): [raw(1, "Cardinals Topps Bob Gibson")]})
    ebay_search.search_all(client)
    assert [o for q, o, _ in client.calls if q == "cardinals"] == [0, 0]   # two searches, one page each


def test_team_settings_exist_only_on_a_team_bot(monkeypatch):
    cfg = settings.load()
    assert cfg["team_claude_per_run"] == 30 and cfg["team_daily_budget"] == 5.0
    assert cfg["price_lookups"] == 4 and cfg["alert_min_stars"] == 4   # frugal by default
    assert "team_lots" in settings.visible()
    with pytest.raises(settings.SettingsError):
        settings.update(db.connect(), {"team_claude_per_run": 500})
    monkeypatch.setattr(profile, "_cache", profile.parse('[player]\nname = "Jordan Walker"'))
    assert "team_claude_per_run" not in settings.load()
    assert settings.load()["price_lookups"] == 8
    with pytest.raises(settings.SettingsError, match="unknown"):
        settings.update(db.connect(), {"team_lots": True})
