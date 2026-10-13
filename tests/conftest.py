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
