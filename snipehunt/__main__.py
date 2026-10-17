"""Snipe Hunt command line.

  python -m snipehunt init     copy the example profile, inventory and lists into data/
  python -m snipehunt init --team
                               the same, with the team-mode example profile
  python -m snipehunt check    show what is configured (never prints secrets)
  python -m snipehunt serve    run the API and the scheduler (what the container runs)
  python -m snipehunt sold gold refractor /50
                               price check: what cards with those words in the
                               title sold for, from the bot's own record

Set SNIPEHUNT_DEMO=1 to serve made-up data instead: a way to see the dashboard
before you have eBay or Claude keys.

Single jobs: python -m snipehunt.jobs hourly|daily|weekly [--dry-run]
"""
from __future__ import annotations

import shutil
import sys
import threading

from . import api, config, db, profile, sales, scheduler

STARTERS = {"profile.example.toml": config.PROFILE, "inventory.example.csv": config.INVENTORY,
            "wantlist.example.md": config.WANTLIST, "preferences.example.md": config.PREFERENCES}


def init(team: bool = False) -> None:
    """Create the home folders and any starter file that isn't there yet."""
    for folder in (config.DATA_DIR, config.STATE_DIR, config.REPORTS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    starters = dict(STARTERS)
    if team:   # team mode: follow every player of one team
        starters = {"profile.team.example.toml": config.PROFILE,
                    **{k: v for k, v in STARTERS.items() if v != config.PROFILE}}
    for name, target in starters.items():
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
    if api.demo_mode():
        print(f"{who.app_name}: DEMO MODE. Serving made-up data; no searches, no alerts.",
              file=sys.stderr)
    else:
        print(f"{who.app_name}: hunting {who.subject} cards", file=sys.stderr)
        threading.Thread(target=scheduler.run_forever, daemon=True).start()
    api.serve()


def sold(words: list[str]) -> None:
    con = db.connect()
    if words:
        print(sales.price_check(con, " ".join(words)))
    else:
        latest = sales.search(con, "", limit=20)
        print(sales.lines(latest) if latest else "No sold prices on record yet. The bot "
              "records one each time an auction it has seen closes with bids.")


def main() -> None:
    commands = {"init": init, "check": check, "serve": serve}
    name, rest = (sys.argv[1:2] or [""])[0], sys.argv[2:]
    if name == "init" and rest == ["--team"]:
        rest, commands["init"] = [], lambda: init(team=True)
    if name not in (*commands, "sold") or (rest and name != "sold"):
        sys.exit(__doc__)
    try:
        sold(rest) if name == "sold" else commands[name]()
    except (config.ConfigError, profile.ProfileError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
