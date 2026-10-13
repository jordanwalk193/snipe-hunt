"""Who you collect, and how to tell their cards from everyone else's.

Read from data/profile.toml. Only the player's name is required; everything
else has a default worked out from it, and each default can be overridden.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cached_property

from . import config

# With no card words set, a title needs a team word or the sport's name to be a
# sure match; everything else with the player's name is left for Claude to judge.
CARD_WORDS: list[str] = []
MAX_TYPO_SEARCHES = 6


class ProfileError(RuntimeError):
    pass


def typos(word: str) -> list[str]:
    """Likely misspellings: one letter dropped, or two neighbours swapped."""
    word = word.lower()
    out: list[str] = []
    for i in range(1, len(word)):
        dropped = word[:i] + word[i + 1:]
        swapped = word[:i - 1] + word[i] + word[i - 1] + word[i + 1:] if i > 1 else ""
        for cand in (dropped, swapped):
            if len(cand) >= 3 and cand != word and cand not in out:
                out.append(cand)
    return out


def _words(entries: list[str]) -> str:
    """A regex alternation that treats spaces and dots in each entry loosely."""
    parts = [re.escape(p.strip().lower()).replace(r"\ ", r"\.?\s*") for p in entries
             if p.strip()]
    return "(?:" + "|".join(sorted(parts, key=len, reverse=True)) + ")" if parts else "(?!x)x"


@dataclass
class Profile:
    player: str                       # "Jordan Walker"
    about: str = ""                   # "the St. Louis Cardinals outfielder"
    sport: str = "baseball"
    collector: str = "the collector"  # the name used in prompts and reports
    app_name: str = "Snipe Hunt"
    first_names: list[str] = field(default_factory=list)   # accepted spellings
    last_names: list[str] = field(default_factory=list)
    team_words: list[str] = field(default_factory=list)    # confirm it is them
    card_words: list[str] = field(default_factory=lambda: list(CARD_WORDS))
    wrong_words: list[str] = field(default_factory=list)   # other sports, schools, teams
    other_first_names: list[str] = field(default_factory=list)   # same surname, other people
    keyword_searches: list[str] = field(default_factory=lambda: ["auto", "patch", "relic", "/"])
    typo_searches: list[str] | None = None     # None = pick some automatically
    extra_searches: list[str] = field(default_factory=list)
    products: str = ""                # products to check for new releases
    binder_pages: dict[str, str] = field(default_factory=dict)   # card year -> page
    binder_spare: str = "unsorted"

    def __post_init__(self) -> None:
        names = self.player.split()
        if len(names) < 2:
            raise ProfileError("player.name needs a first and last name")
        first, last = names[0].lower(), names[-1].lower()
        if not self.first_names:
            self.first_names = [first, *typos(first), first[0]]
        if not self.last_names:
            self.last_names = [last, *typos(last)]
        if self.typo_searches is None:
            firsts = [f"{t} {last}" for t in typos(first)]
            lasts = [f"{first} {t}" for t in typos(last)]
            # Alternate first-name and last-name typos so both get covered.
            mixed = [p for pair in zip(firsts, lasts) for p in pair] or firsts + lasts
            self.typo_searches = mixed[:MAX_TYPO_SEARCHES]

    @property
    def last(self) -> str:
        return self.player.split()[-1]

    @property
    def described(self) -> str:
        """The player as a prompt should introduce them."""
        return f"{self.player}, {self.about}" if self.about else self.player

    @property
    def search_name(self) -> str:
        return self.player.lower()

    @cached_property
    def _name_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.first_names)}\.?\s+{_words(self.last_names)}\b", re.I)

    @cached_property
    def _bare_last_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.last_names)}\b", re.I)

    @cached_property
    def _team_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.team_words)}\b", re.I)

    @cached_property
    def _right_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.team_words + self.card_words + [self.sport])}\b", re.I)

    @cached_property
    def _wrong_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.wrong_words)}\b", re.I)

    @cached_property
    def _other_re(self) -> re.Pattern:
        return re.compile(
            rf"\b{_words(self.other_first_names)}\.?\s+{_words(self.last_names)}\b", re.I)

    def classify(self, title: str) -> str:
        """Is this listing title a card of the player? yes / unclear / no."""
        if self._wrong_re.search(title):
            return "no"
        if self._name_re.search(title):
            return "yes" if self._right_re.search(title) else "unclear"
        # "Walker Cardinals rookie lot" style titles with no first name.
        if self.team_words and self._bare_last_re.search(title) and self._team_re.search(title):
            return "no" if self._other_re.search(title) else "unclear"
        return "no"

    def binder_page(self, year: str | None) -> str:
        return self.binder_pages.get(str(year or ""), self.binder_spare)

    def public(self) -> dict:
        """What the dashboard needs to know about the profile."""
        return {"app_name": self.app_name, "player": self.player, "sport": self.sport,
                "collector": self.collector}


def parse(text: str) -> Profile:
    raw = tomllib.loads(text)
    player = raw.get("player") or {}
    if not isinstance(player, dict) or not player.get("name"):
        raise ProfileError('profile.toml needs [player] with name = "First Last"')
    search = raw.get("search") or {}
    binder = raw.get("binder") or {}
    known = {
        "player": player["name"], "about": player.get("about", ""),
        "sport": player.get("sport", "baseball"),
        "collector": raw.get("collector", "the collector"),
        "app_name": raw.get("app_name", "Snipe Hunt"),
        "first_names": player.get("first_names", []),
        "last_names": player.get("last_names", []),
        "team_words": player.get("team_words", []),
        "wrong_words": player.get("wrong_words", []),
        "other_first_names": player.get("other_first_names", []),
        "typo_searches": search.get("typos"),
        "extra_searches": search.get("extra", []),
        "products": search.get("products", ""),
        "binder_pages": {str(k): str(v) for k, v in (binder.get("pages") or {}).items()},
        "binder_spare": binder.get("spare", "unsorted"),
    }
    if "card_words" in player:
        known["card_words"] = player["card_words"]
    if "keywords" in search:
        known["keyword_searches"] = search["keywords"]
    return Profile(**known)


_cache: Profile | None = None


def get() -> Profile:
    global _cache
    if _cache is None:
        if not config.PROFILE.exists():
            raise ProfileError(f"{config.PROFILE} is missing. Run: python -m snipehunt init")
        _cache = parse(config.PROFILE.read_text())
    return _cache


def describe() -> None:
    p = get()
    print(f"{p.app_name}: {p.collector} collects {p.described} ({p.sport})")
    print("first names:", ", ".join(p.first_names))
    print("last names: ", ", ".join(p.last_names))
    print("typo searches:", ", ".join(p.typo_searches or []))


if __name__ == "__main__":
    describe()
