"""Configuration: where things live, and the secrets.

Everything the bot keeps is under one home directory (`SNIPEHUNT_HOME`, default
/var/lib/snipe-hunt): `data/` holds the files you edit (profile.toml,
inventory.csv, wantlist.md, preferences.md, reports/), `state/` holds what the
bot remembers.

Secrets come from environment variables, or from an env file (`SNIPEHUNT_ENV`,
default <home>/env, mode 600). Real environment variables win over the file.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(os.environ.get("SNIPEHUNT_HOME", "/var/lib/snipe-hunt"))
ENV_FILE = Path(os.environ.get("SNIPEHUNT_ENV", HOME / "env"))
DATA_DIR = HOME / "data"
STATE_DIR = HOME / "state"
SEEN_DB = STATE_DIR / "seen.db"
HEALTHZ = STATE_DIR / "healthz"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

PROFILE = DATA_DIR / "profile.toml"
INVENTORY = DATA_DIR / "inventory.csv"
WANTLIST = DATA_DIR / "wantlist.md"
PREFERENCES = DATA_DIR / "preferences.md"
REPORTS_DIR = DATA_DIR / "reports"

DEFAULTS = {
    "EBAY_MARKETPLACE": "EBAY_US",
    "EBAY_CATEGORY": "261328",   # Sports Trading Card Singles
    "CLAUDE_BIN": "claude",
    "API_PORT": "8766",
    "API_BIND": "0.0.0.0",
    "SNIPEHUNT_TZ": "America/New_York",
    "FAST_LANE_MINUTES": "0",    # check brand-new Buy It Nows this often; 0 = off
    "GITHUB_REPO": "",           # owner/name: where the weekly self-review opens its issue
}
SECRETS = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "GMAIL_USER", "GMAIL_APP_PASSWORD",
           "HA_URL", "HA_TOKEN", "HA_NOTIFY_SERVICE", "NTFY_URL", "GIXEN_USER",
           "GIXEN_PASS", "API_TOKEN", "KUMA_PUSH_URL", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
           "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE")


class ConfigError(RuntimeError):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_cache: dict[str, str] | None = None


def load() -> dict[str, str]:
    global _cache
    if _cache is None:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in _read_env_file(ENV_FILE).items() if v})
        merged.update({k: v for k, v in os.environ.items() if v})
        _cache = merged
    return _cache


def get(name: str, default: str | None = None) -> str | None:
    return load().get(name, default)


def require(*names: str) -> list[str]:
    """Return the values for `names`, or fail naming what is missing."""
    cfg = load()
    missing = [n for n in names if not cfg.get(n)]
    if missing:
        raise ConfigError(
            f"Missing {', '.join(missing)}: set as environment variables or in {ENV_FILE}"
        )
    return [cfg[n] for n in names]


TZ = ZoneInfo(get("SNIPEHUNT_TZ"))


def local(when):
    """A datetime in the bot's time zone, so it prints as EDT rather than UTC-04:00."""
    return when.astimezone(TZ)


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def describe() -> None:
    """Show which settings are present without printing any secret values."""
    cfg = load()
    print(f"home:     {HOME}")
    print(f"env file: {ENV_FILE} ({'found' if ENV_FILE.exists() else 'not found'})")
    for path in (PROFILE, INVENTORY, WANTLIST, PREFERENCES):
        print(f"  {path.name:16} "
              f"{'found' if path.exists() else 'MISSING (run: python -m snipehunt init)'}")
    for key in SECRETS:
        print(f"  {key:20} {'set' if cfg.get(key) else '-'}")


if __name__ == "__main__":
    describe()
