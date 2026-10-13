"""Snipe Hunt command line.

  python -m snipehunt init     copy the example profile, inventory and lists into data/
  python -m snipehunt check    show what is configured (never prints secrets)
  python -m snipehunt serve    run the API and the scheduler (what the container runs)

Single jobs: python -m snipehunt.jobs hourly|daily|weekly [--dry-run]
"""
from __future__ import annotations

import shutil
import sys
import threading

from . import api, config, profile, scheduler

STARTERS = {"profile.example.toml": config.PROFILE, "inventory.example.csv": config.INVENTORY,
            "wantlist.example.md": config.WANTLIST, "preferences.example.md": config.PREFERENCES}


def init() -> None:
    """Create the home folders and any starter file that isn't there yet."""
    for folder in (config.DATA_DIR, config.STATE_DIR, config.REPORTS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    for name, target in STARTERS.items():
        if target.exists():
            print(f"kept     {target}")
        else:
            shutil.copy(config.EXAMPLES / name, target)
            print(f"created  {target}")
    print(f"\nEdit {config.PROFILE} to say who you collect, then: python -m snipehunt check")


def check() -> None:
    config.describe()
    if config.PROFILE.exists():
        profile.describe()


def serve() -> None:
    who = profile.get()   # fail now, with a clear message, if the profile is missing
    print(f"{who.app_name}: hunting {who.player} cards", file=sys.stderr)
    threading.Thread(target=scheduler.run_forever, daemon=True).start()
    api.serve()


def main() -> None:
    commands = {"init": init, "check": check, "serve": serve}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    try:
        commands[sys.argv[1]]()
    except (config.ConfigError, profile.ProfileError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
