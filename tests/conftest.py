import pytest

from snipehunt import config, profile

EXAMPLE = config.EXAMPLES / "profile.example.toml"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own empty home, the example profile and no secrets."""
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    for name, value in {"HOME": tmp_path, "DATA_DIR": data, "STATE_DIR": state,
                        "SEEN_DB": state / "seen.db", "HEALTHZ": state / "healthz",
                        "PROFILE": data / "profile.toml", "INVENTORY": data / "inventory.csv",
                        "WANTLIST": data / "wantlist.md", "PREFERENCES": data / "preferences.md",
                        "REPORTS_DIR": data / "reports"}.items():
        monkeypatch.setattr(config, name, value)
    (data / "inventory.csv").write_text((config.EXAMPLES / "inventory.example.csv").read_text())
    (data / "wantlist.md").write_text((config.EXAMPLES / "wantlist.example.md").read_text())
    (data / "preferences.md").write_text(
        (config.EXAMPLES / "preferences.example.md").read_text())
    monkeypatch.setattr(config, "_cache", dict(config.DEFAULTS))
    monkeypatch.setattr(profile, "_cache", profile.parse(EXAMPLE.read_text()))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may reach the network, even with made-up keys set."""
    import requests

    def blocked(self, method, url, *args, **kwargs):
        raise requests.ConnectionError(f"network is off in tests: {method} {url}")
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch):
    """eBay retries back off with sleeps; with the network off they would only slow tests."""
    from snipehunt import ebay_search
    monkeypatch.setattr(ebay_search.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def fresh_call_counts(monkeypatch):
    """eBay call counts live in the process (ebay_search, usage); each test starts at zero."""
    from collections import Counter

    from snipehunt import ebay_search, usage
    monkeypatch.setattr(ebay_search, "API_CALLS", 0)
    monkeypatch.setattr(ebay_search, "LANE_CALLS", Counter())
    monkeypatch.setattr(usage, "_today", {})
    monkeypatch.setattr(usage, "_warned", [])


@pytest.fixture(autouse=True)
def shipping_neutral(request, monkeypatch):
    """A listing's own shipping moves a cheap card's value (pricing.for_listing). Tests of
    other maths are written at the typical shipping, so it is off unless a test asks for
    the `shipping_aware` fixture."""
    if "shipping_aware" not in request.fixturenames:
        from snipehunt import pricing
        monkeypatch.setattr(pricing, "SHIPPING_MATTERS_UNDER", 0.0)


@pytest.fixture
def shipping_aware():
    """Leave pricing.for_listing switched on (see shipping_neutral)."""
