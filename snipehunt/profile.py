"""Who you collect, and how to tell their cards from everyone else's.

Read from data/profile.toml. A profile follows one player ([player], the
original) or one team ([team], team mode), never both: one bot, one hunt. Only
the name is required; everything else has a default worked out from it, and
each default can be overridden.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cached_property

from . import config, teams

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
    category: str | None = None       # eBay category id; None = EBAY_CATEGORY, "" = none
    junk_words: list[str] | None = None   # titles to drop unscored; None = filters.JUNK_WORDS
    lots: bool = True                 # false: drop "lot of" listings before scoring
    midtier: dict | None = None       # [search.midtier]; None = the mid-tier lane is off
    steals: dict | None = None        # [steals]; None = steals mode is off (steals.py)

    def __post_init__(self) -> None:
        names = self.player.split()
        if not names:
            raise ProfileError("player.name is empty")
        if len(names) == 1:
            # One name: a Pokémon ("Charizard"), or a player known by one name.
            # The name itself, or a misspelling of it, is what a title must have.
            only = names[0].lower()
            self.first_names = []
            if not self.last_names:
                self.last_names = [only, *typos(only)]
            if self.typo_searches is None:
                self.typo_searches = typos(only)[:MAX_TYPO_SEARCHES]
            return
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
        if not self.first_names:   # a one-word name
            return re.compile(rf"\b{_words(self.last_names)}\b", re.I)
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

    mode = "player"

    @property
    def subject(self) -> str:
        """Who the hunt is for, as alerts and reports name it."""
        return self.player

    def match(self, title: str) -> tuple[str, str | None, str | None]:
        """(verdict, how it matched, which player): the same verdict as classify."""
        verdict = self.classify(title)
        return verdict, None if verdict == "no" else "player", (
            None if verdict == "no" else self.player)


def _name_pattern(name: str) -> re.Pattern:
    """A roster player's name: first name or initial, then the last name or a typo of it."""
    parts = name.lower().split()
    first, last = parts[0], " ".join(parts[1:])
    lasts = [last, *typos(last)] if " " not in last else [last]
    return re.compile(rf"\b{_words([first, first[0]])}\.?\s+{_words(lasts)}\b", re.I)


@dataclass
class TeamProfile:
    """Team mode: every card of one team, whoever is on it.

    A title belongs to the team by its full name, its city with its nickname,
    its nickname with a card or sport word, or a roster player with a team or
    card word. Titles from other sports, and from teams elsewhere with the same
    nickname, are ruled out by wrong_words (several are added for you).
    """
    team: str                          # "St. Louis Cardinals"
    sport: str = "baseball"
    about: str = ""
    collector: str = "the collector"
    app_name: str = "Snipe Hunt"
    city_words: list[str] = field(default_factory=list)
    nickname_words: list[str] = field(default_factory=list)
    card_words: list[str] = field(default_factory=list)
    roster: list[str] = field(default_factory=list)   # players to put first, best first
    roster_any_team: bool = False     # a roster player's card from another team counts too
    logo_only: bool = False           # true: unlicensed cards with no team logos don't count
    wrong_words: list[str] = field(default_factory=list)
    auto_wrong_words: bool = True     # add other sports and same-nickname teams elsewhere
    keyword_searches: list[str] = field(default_factory=lambda: ["auto", "patch", "/", "rookie"])
    typo_searches: list[str] | None = None
    extra_searches: list[str] = field(default_factory=list)
    products: str = ""
    binder_pages: dict[str, str] = field(default_factory=dict)
    binder_spare: str = "unsorted"
    search: str = ""                  # what eBay is asked for; default: the nickname
    category: str | None = None       # eBay category id; None = EBAY_CATEGORY, "" = none
    junk_words: list[str] | None = None
    lots: bool = True
    midtier = None                    # the mid-tier lane is player mode only
    steals: dict | None = None        # [steals]; None = steals mode is off (steals.py)

    mode = "team"

    def __post_init__(self) -> None:
        words = self.team.split()
        if len(words) < 2:
            raise ProfileError('team.name needs a city and a nickname, like "St. Louis Cardinals"')
        if not self.nickname_words:
            self.nickname_words = [words[-1].lower()]
        if not self.city_words:
            city = " ".join(words[:-1]).lower()
            self.city_words = list(dict.fromkeys([city, city.replace(".", "")]))
        self.nickname_words = [w.lower() for w in self.nickname_words]
        self.city_words = [w.lower() for w in self.city_words]
        if not self.card_words:
            self.card_words = list(teams.CARD_WORDS.get(self.sport, []))
        if self.auto_wrong_words:
            extra = teams.other_sport_words(self.sport) + [
                c for n in self.nickname_words
                for c in teams.same_nickname_elsewhere(self.sport, n)]
            self.wrong_words = list(dict.fromkeys(
                [w.lower() for w in self.wrong_words] + extra))
            # the team's own city never rules a title out
            self.wrong_words = [w for w in self.wrong_words if w not in self.city_words]
        self.search = (self.search or self.nickname_words[0]).lower()
        nick = max(self.nickname_words, key=len)
        self.nickname_typos = [t for t in typos(nick) if t != nick[:-1] and len(t) >= 5]
        if self.typo_searches is None:
            self.typo_searches = self.nickname_typos[:3]

    # The attributes the player-mode code reads, answered for a team.
    @property
    def player(self) -> str:
        return self.team

    @property
    def subject(self) -> str:
        return self.team

    @property
    def last(self) -> str:
        return self.nickname_words[0].title()

    @property
    def described(self) -> str:
        base = f"{self.team} cards, any player"
        return f"{base} ({self.about})" if self.about else base

    @property
    def search_name(self) -> str:
        return self.search

    @property
    def team_words(self) -> list[str]:
        return self.city_words + self.nickname_words + self.nickname_typos

    @cached_property
    def _nick_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.nickname_words + self.nickname_typos)}\b", re.I)

    @cached_property
    def _exact_nick_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.nickname_words)}\b", re.I)

    @cached_property
    def _city_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.city_words)}\b", re.I)

    @cached_property
    def _right_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.card_words + [self.sport])}\b", re.I)

    @cached_property
    def _wrong_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.wrong_words)}\b", re.I)

    @cached_property
    def _roster_res(self) -> list[tuple[str, re.Pattern]]:
        return [(name, _name_pattern(name)) for name in self.roster if len(name.split()) >= 2]

    @cached_property
    def _others_re(self) -> re.Pattern:
        others = [n for n in teams.others_in_league(self.sport, "")
                  if n not in self.nickname_words]
        return re.compile(rf"\b{_words(others)}\b", re.I)

    def roster_player(self, title: str) -> str | None:
        """The first roster player named in the title, in roster order."""
        return next((name for name, pat in self._roster_res if pat.search(title)), None)

    def other_team(self, title: str) -> bool:
        """Does the title name another team from the same league (a combo card)?"""
        return bool(self._others_re.search(title))

    # Players Claude found on other teams' cards with the same nickname (score.learn_not_team).
    learned: list[str] = field(default_factory=list, repr=False)

    def learn(self, names: list[str]) -> None:
        self.learned = list(names)
        self.__dict__.pop("_learned_re", None)

    @cached_property
    def _learned_re(self) -> re.Pattern:
        return re.compile(rf"\b{_words(self.learned)}\b", re.I)

    def match(self, title: str) -> tuple[str, str | None, str | None]:
        """(yes / unclear / no, how it matched, the roster player named or None)."""
        if self._wrong_re.search(title):
            return "no", None, None
        if self.learned and self._learned_re.search(title):
            return "no", None, None
        who = self.roster_player(title)
        nick, city = self._nick_re.search(title), self._city_re.search(title)
        # a misspelled nickname is where other buyers' searches miss a card
        named = "nickname" if nick and self._exact_nick_re.search(title) else "typo"
        if nick and city:
            return "yes", "team" if named == "nickname" else "typo", who
        if nick and self._right_re.search(title):
            return "yes", named, who
        if who:
            if nick or city:
                return "yes", "roster", who
            if self.roster_any_team:
                return "yes", "roster", who
            if self.other_team(title):   # him, but on the card of another team
                return "no", None, None
            return "unclear", "roster", who
        if nick:
            return "unclear", named, None
        return "no", None, None

    def classify(self, title: str) -> str:
        return self.match(title)[0]

    def binder_page(self, year: str | None) -> str:
        return self.binder_pages.get(str(year or ""), self.binder_spare)

    def public(self) -> dict:
        """What the dashboard needs. "player" holds the team so older dashboards still work."""
        return {"app_name": self.app_name, "player": self.team, "sport": self.sport,
                "collector": self.collector, "mode": "team", "team": self.team,
                "roster": list(self.roster)}


def _parse_team(raw: dict) -> TeamProfile:
    team = raw["team"]
    if not isinstance(team, dict) or not team.get("name"):
        raise ProfileError('[team] needs name = "City Nickname", like "St. Louis Cardinals"')
    search = raw.get("search") or {}
    binder = raw.get("binder") or {}
    known = {
        "team": team["name"], "sport": team.get("sport", "baseball"),
        "about": team.get("about", ""),
        "collector": raw.get("collector", "the collector"),
        "app_name": raw.get("app_name", "Snipe Hunt"),
        "city_words": team.get("city_words", []),
        "nickname_words": team.get("nickname_words", []),
        "card_words": team.get("card_words", []),
        "roster": [str(n) for n in team.get("roster", [])],
        "roster_any_team": bool(team.get("roster_any_team", False)),
        "logo_only": bool(team.get("logo_only", False)),
        "wrong_words": team.get("wrong_words", []),
        "auto_wrong_words": bool(team.get("auto_wrong_words", True)),
        "typo_searches": search.get("typos"),
        "extra_searches": search.get("extra", []),
        "products": search.get("products", ""),
        "search": team.get("search", ""),
        **_search_extras(search),
        "binder_pages": {str(k): str(v) for k, v in (binder.get("pages") or {}).items()},
        "binder_spare": binder.get("spare", "unsorted"),
    }
    if "keywords" in search:
        known["keyword_searches"] = search["keywords"]
    if isinstance(raw.get("steals"), dict):
        known["steals"] = dict(raw["steals"])
    return TeamProfile(**known)


def _search_extras(search: dict) -> dict:
    """[search] keys both modes share: the eBay category, junk words, lots."""
    out: dict = {"lots": bool(search.get("lots", True))}
    for key in ("category_id", "category"):   # category_id is the newer name
        if key in search:
            out["category"] = str(search[key]).strip()
            break
    if "junk_words" in search:
        out["junk_words"] = [str(w) for w in search["junk_words"]]
    return out


def parse(text: str) -> Profile | TeamProfile:
    raw = tomllib.loads(text)
    if "team" in raw:
        if "player" in raw:
            raise ProfileError("profile.toml has both [player] and [team]: one bot follows "
                               "one or the other. Run a second copy for the other hunt.")
        return _parse_team(raw)
    player = raw.get("player") or {}
    if not isinstance(player, dict) or not player.get("name"):
        raise ProfileError('profile.toml needs [player] with name = "First Last" (or one '
                           'name, like "Charizard"), '
                           'or [team] with name = "City Nickname"')
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
        **_search_extras(search),
    }
    if isinstance(search.get("midtier"), dict):
        known["midtier"] = dict(search["midtier"])
    if isinstance(raw.get("steals"), dict):
        known["steals"] = dict(raw["steals"])
    if "card_words" in player:
        known["card_words"] = player["card_words"]
    if "keywords" in search:
        known["keyword_searches"] = search["keywords"]
    return Profile(**known)


_cache: Profile | TeamProfile | None = None


def get() -> Profile | TeamProfile:
    global _cache
    if _cache is None:
        if not config.PROFILE.exists():
            raise ProfileError(f"{config.PROFILE} is missing. Run: python -m snipehunt init")
        _cache = parse(config.PROFILE.read_text())
    return _cache


def describe() -> None:
    p = get()
    print(f"{p.app_name}: {p.collector} collects {p.described} ({p.sport})")
    if p.mode == "team":
        print("city words:", ", ".join(p.city_words))
        print("nicknames: ", ", ".join(p.nickname_words))
        print("roster:    ", ", ".join(p.roster) or "(none)")
        print("ruled out: ", ", ".join(p.wrong_words))
        print("typo searches:", ", ".join(p.typo_searches or []))
        return
    print("first names:", ", ".join(p.first_names))
    print("last names: ", ", ".join(p.last_names))
    print("typo searches:", ", ".join(p.typo_searches or []))


if __name__ == "__main__":
    describe()
