"""Team mode's profile: parsing, defaults, and which titles are the team's."""
import pytest

from snipehunt import config, profile

TEAM = (config.EXAMPLES / "profile.team.example.toml").read_text()


@pytest.fixture
def team(monkeypatch):
    p = profile.parse(TEAM)
    monkeypatch.setattr(profile, "_cache", p)
    return p


def test_the_example_is_a_team_profile(team):
    assert team.mode == "team" and team.subject == "St. Louis Cardinals"
    assert team.search_name == "cardinals"
    assert "arizona" in team.wrong_words and "nfl" in team.wrong_words
    assert "st louis" not in team.wrong_words
    assert team.public()["mode"] == "team" and team.public()["player"] == "St. Louis Cardinals"


def test_defaults_come_from_the_name():
    p = profile.parse('[team]\nname = "Chicago Cubs"')
    assert p.city_words == ["chicago"] and p.nickname_words == ["cubs"]
    assert "topps" in p.card_words and p.typo_searches == []   # "cubs" is too short to misspell
    p = profile.parse('[team]\nname = "Kansas City Chiefs"\nsport = "football"')
    assert "baseball" in p.wrong_words and "mlb" in p.wrong_words
    assert "panini" in p.card_words


def test_a_team_name_needs_a_city_and_a_nickname():
    with pytest.raises(profile.ProfileError):
        profile.parse('[team]\nname = "Cardinals"')


def test_one_bot_follows_a_player_or_a_team_not_both():
    with pytest.raises(profile.ProfileError, match="both"):
        profile.parse('[player]\nname = "Jordan Walker"\n[team]\nname = "St. Louis Cardinals"')


@pytest.mark.parametrize("title, verdict, via, who", [
    ("2024 Topps Chrome Masyn Winn RC Cardinals Refractor /250", "yes", "nickname", "Masyn Winn"),
    ("St. Louis Cardinals 2023 Topps Team Set", "yes", "team", None),
    ("STL Cardinals Bob Gibson 1968", "yes", "team", None),
    ("Bob Gibson Cardinals 1968 Topps", "yes", "nickname", None),
    ("Jordan Walker Cardinals auto", "yes", "roster", "Jordan Walker"),
    ("J. Walker Redbirds 2023", "yes", "roster", "Jordan Walker"),
    ("2023 Topps Chrome Jordan Walker RC", "unclear", "roster", "Jordan Walker"),
    ("Ozzie Smith Cardnals auto", "unclear", "typo", None),
    ("Ozzie Smith Cardnals Topps auto", "yes", "typo", None),
    ("Cardinals Cubs dual auto Topps", "yes", "nickname", None),
    ("Cardinals lot", "unclear", "nickname", None),
])
def test_team_titles(team, title, verdict, via, who):
    assert team.match(title) == (verdict, via, who)
    assert team.classify(title) == verdict


@pytest.mark.parametrize("title", [
    "Kyler Murray Arizona Cardinals Prizm",          # the NFL team with the same nickname
    "Cardinals Panini Prizm football Murray",
    "Louisville Cardinals Donovan Mitchell",          # a college, from wrong_words
    "Nolan Arenado Rockies 2019 Topps",               # a roster player on another team
    "St. Louis Blues hockey card",
    "2023 Topps Chrome Bobby Witt RC",
])
def test_not_the_team(team, title):
    assert team.classify(title) == "no"


def test_roster_any_team_counts_his_other_cards():
    p = profile.parse('[team]\nname = "St. Louis Cardinals"\nroster = ["Nolan Arenado"]\n'
                      'roster_any_team = true')
    assert p.match("Nolan Arenado Rockies 2019 Topps") == ("yes", "roster", "Nolan Arenado")


def test_combo_cards_are_spotted(team):
    assert team.other_team("Cardinals Cubs dual auto Topps")
    assert not team.other_team("Cardinals Masyn Winn RC")


def test_player_profiles_answer_the_new_questions():
    p = profile.get()
    assert p.mode == "player" and p.subject == "Jordan Walker"
    assert p.match("2023 Topps Chrome Jordan Walker RC #209 Cardinals") == (
        "yes", "player", "Jordan Walker")
    assert p.match("Larry Walker Cardinals 2005 Topps")[0] == "no"


@pytest.mark.parametrize("title", [
    "2023 Panini Phoenix Michael Wilson RC Auto Patch Cardinals",
    "Cardinals QB rookie auto Prizm",
    "2023 Panini Obsidian Cardinals helmet",
])
def test_football_only_words_rule_out_the_nfl_cardinals(team, title):
    assert team.classify(title) == "no"


def test_baseball_panini_cards_still_count(team):
    assert team.classify("2022 Panini Immaculate Juan Yepez Patch Auto Cardinals") != "no"


def test_init_team_starts_from_the_team_example(monkeypatch):
    import sys
    from snipehunt import __main__ as cli
    config.PROFILE.unlink(missing_ok=True)
    monkeypatch.setattr(sys, "argv", ["snipehunt", "init", "--team"])
    cli.main()
    assert profile.parse(config.PROFILE.read_text()).mode == "team"
