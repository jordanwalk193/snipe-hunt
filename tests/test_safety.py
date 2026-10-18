"""Recommend only, checked on the source itself: no buying, no offers, no Gixen writes,
no secrets. Like test_guards.py, a change that needs this file edited is a
question for the owner, not a fix."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "snipehunt").rglob("*.py"))

# eBay order and offer endpoints, bidding and checkout.
FORBIDDEN = ("buy/offer", "buy/order", "place_proxy_bid", "placeoffer", "checkout",
             "gixen.com/api", "/sell/", "/post-order/", "makeoffer", "bestoffer/submit")
# Names a function or URL would have if it changed a Gixen snipe.
GIXEN_WRITES = re.compile(r"(add|edit|delete|remove|change|update|place)_?snipe", re.I)
# Secrets that look real: an eBay OAuth token, or a long hex or base64 value
# given to a name that sounds like a key, token, secret or password.
TOKEN = re.compile(r"v\^1\.1#")
LONG_SECRET = re.compile(
    r"""(?i)\b\w*(key|token|secret|passw(or)?d)\w*\s*[:=]\s*["']"""
    r"""([0-9a-f]{32,}|[A-Za-z0-9+/=_-]{40,})["']""")
HEX40 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def test_there_are_sources_to_scan():
    names = {p.name for p in SOURCES}
    assert {"ebay_search.py", "gixen.py", "midtier.py", "mislabel.py", "filters.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_order_offer_or_checkout_endpoint(path):
    text = path.read_text().lower()
    for word in FORBIDDEN:
        assert word not in text, (path.name, word)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_gixen_is_never_written_to(path):
    text = path.read_text()
    assert not GIXEN_WRITES.search(text), path.name
    # The only POST anywhere near Gixen is the login form gixen.py reads the list behind.
    for line in text.splitlines():
        if re.search(r"\.post\(|\"POST\"|'POST'", line) and "gixen" in text.lower():
            if path.name == "gixen.py":
                assert 'session.post(BASE + "home_1.php"' in line, line
            else:
                assert "gixen" not in line.lower(), (path.name, line)


def test_gixen_posts_only_the_login_form():
    text = (ROOT / "snipehunt" / "gixen.py").read_text()
    assert re.findall(r"\.post\(([^,]+),", text) == ['BASE + "home_1.php"']
    assert re.findall(r"https?://[^\"']*gixen[^\"']*", text) == ["https://www.gixen.com/main/"]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_hard_coded_secrets(path):
    text = path.read_text()
    assert not TOKEN.search(text), path.name
    assert not LONG_SECRET.search(text), path.name
    assert not HEX40.search(text), path.name


def test_the_scan_would_catch_what_it_looks_for():
    """The patterns themselves work: each would fail a file that had the thing in it."""
    assert TOKEN.search('t = "v^1.1#i^1#p^3#r^0#f^0#I^3#t^H4sI"')
    assert LONG_SECRET.search('API_TOKEN = "' + "ab12" * 10 + '"')
    assert LONG_SECRET.search('client_secret: "' + "Zm9vYmFy" * 6 + '"')
    assert HEX40.search("x = '" + "0f" * 20 + "'")
    assert GIXEN_WRITES.search("def add_snipe(item, amount):")
    assert not LONG_SECRET.search('KEY = "claude_cost"')


def test_new_local_files_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text().split()
    for pattern in (".env", "*.db", "/state/", "/home/"):
        assert pattern in ignored, pattern
    # the item cache and aspect names live in the state database, never in the repo
    assert "item_cache" in (ROOT / "snipehunt" / "item_cache.py").read_text()
    assert "db.connect" in (ROOT / "snipehunt" / "item_cache.py").read_text()


def test_the_midtier_lane_only_searches_and_reads(monkeypatch):
    """Run the lane against a recording HTTP session: every call is a GET to the search
    or single-listing endpoint, apart from the OAuth token itself."""
    import requests

    from snipehunt import config, db, ebay_search, midtier

    monkeypatch.setitem(config.load(), "EBAY_CLIENT_ID", "made-up-id")
    monkeypatch.setitem(config.load(), "EBAY_CLIENT_SECRET", "made-up-secret")
    calls = []

    class Answer:
        status_code, headers, text = 200, {}, ""

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def record(self, method, url, **kwargs):
        calls.append((method, url))
        if url == ebay_search.TOKEN_URL:
            return Answer({"access_token": "made-up", "expires_in": 7200})
        if url == ebay_search.SEARCH_URL:
            return Answer({"itemSummaries": [{
                "legacyItemId": "900000000001", "title": "2021 Bowman Jordon Walker Atomic",
                "price": {"value": "30.00", "currency": "USD"},
                "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "made-up"}}]})
        return Answer({"localizedAspects": []})
    monkeypatch.setattr(requests.Session, "request", record)
    midtier.hunt(db.connect())
    assert calls, "the lane made no calls at all"
    for method, url in calls:
        assert (method, url) == ("POST", ebay_search.TOKEN_URL) or (
            method == "GET" and url in (ebay_search.SEARCH_URL, ebay_search.ITEM_URL)), url
