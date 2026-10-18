"""Every search: the singles category unless the profile says otherwise, and no price
filter without its currency."""
import re

from snipehunt import config, ebay_search, midtier, profile
from snipehunt.ebay_search import price_filter

EXAMPLE = (config.EXAMPLES / "profile.example.toml").read_text()


def sent_params(monkeypatch, q="jordan walker", flt="buyingOptions:{AUCTION}"):
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "x")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "y")
    client = ebay_search.EbayClient()
    sent = {}
    monkeypatch.setattr(client, "_headers", lambda: {})

    class Answer:
        def json(self):
            return {}
    monkeypatch.setattr(client, "_request",
                        lambda m, u, **kw: sent.update(kw["params"]) or Answer())
    client.search(q, flt, None)
    return sent


def test_every_query_is_in_the_singles_category_by_default(monkeypatch):
    assert sent_params(monkeypatch)["category_ids"] == "261328"


def test_category_id_in_the_profile_wins_and_empty_turns_it_off(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.parse(
        EXAMPLE.replace('category_id = "261328"\n', 'category_id = "183454"\n', 1)))
    assert sent_params(monkeypatch)["category_ids"] == "183454"
    monkeypatch.setattr(profile, "_cache", profile.parse(
        EXAMPLE.replace('category_id = "261328"\n', 'category_id = ""\n', 1)))
    assert "category_ids" not in sent_params(monkeypatch)   # lots are listed everywhere


def test_the_older_category_key_still_works(monkeypatch):
    text = EXAMPLE.replace('category_id = "261328"\n', 'category = "183454"\n', 1)
    monkeypatch.setattr(profile, "_cache", profile.parse(text))
    assert ebay_search.EbayClient.__init__ and profile.get().category == "183454"


def test_price_filter_always_has_its_currency():
    assert price_filter(20, 80) == "price:[20..80],priceCurrency:USD"
    assert price_filter(high=80) == "price:[..80],priceCurrency:USD"
    assert price_filter(3) == "price:[3..],priceCurrency:USD"


def test_no_query_anywhere_has_a_price_without_priceCurrency():
    filters = [flt for _, _, flt, _ in ebay_search.queries()]
    filters += [flt for _, _, flt, _ in midtier.queries(midtier.config_now())]
    for flt in filters:
        if "price:" in flt:
            assert "priceCurrency:USD" in flt, flt


def test_price_filters_are_written_in_one_place_only():
    """Any "price:[" in the code outside price_filter() would be a filter that could
    lose its currency."""
    for path in (config.EXAMPLES.parent / "snipehunt").glob("*.py"):
        for line in path.read_text().splitlines():
            if re.search(r"""["']price:\[""", line) or re.search(r"f[\"']price:\[", line):
                assert path.name == "ebay_search.py" and "priceCurrency:USD" in line, line


def test_prices_are_rechecked_after_the_search():
    item = ebay_search.Listing("1", "t", 85.0, "USD", 0.0, ["FIXED_PRICE"], None, None, "s",
                               "u", None)
    cfg = midtier.config_now()
    assert not midtier.in_band(item, cfg)       # eBay's filter let an $85 card through
    item.price = 30.0
    assert midtier.in_band(item, cfg)
    item.price, item.shipping = 79.0, 5.0       # $84 delivered is past the band
    assert not midtier.in_band(item, cfg)
