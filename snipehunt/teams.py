"""Pro team names, so team mode can tell its team's cards from everyone else's.

Two uses: a title that names another team from the same league is a card with
more than one team on it (a dual auto, league leaders, a combo), and a team
in another league that shares the nickname (the Arizona Cardinals, the New
York Giants and the San Francisco Giants) gives the words that rule a title out.
Nicknames are lower case; a city is how titles usually write it.
"""
from __future__ import annotations

LEAGUES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "mlb": ("baseball", [
        ("arizona", "diamondbacks"), ("atlanta", "braves"), ("baltimore", "orioles"),
        ("boston", "red sox"), ("chicago", "cubs"), ("chicago", "white sox"),
        ("cincinnati", "reds"), ("cleveland", "guardians"), ("colorado", "rockies"),
        ("detroit", "tigers"), ("houston", "astros"), ("kansas city", "royals"),
        ("los angeles", "angels"), ("los angeles", "dodgers"), ("miami", "marlins"),
        ("milwaukee", "brewers"), ("minnesota", "twins"), ("new york", "mets"),
        ("new york", "yankees"), ("oakland", "athletics"), ("philadelphia", "phillies"),
        ("pittsburgh", "pirates"), ("san diego", "padres"), ("san francisco", "giants"),
        ("seattle", "mariners"), ("st louis", "cardinals"), ("tampa bay", "rays"),
        ("texas", "rangers"), ("toronto", "blue jays"), ("washington", "nationals"),
    ]),
    "nfl": ("football", [
        ("arizona", "cardinals"), ("atlanta", "falcons"), ("baltimore", "ravens"),
        ("buffalo", "bills"), ("carolina", "panthers"), ("chicago", "bears"),
        ("cincinnati", "bengals"), ("cleveland", "browns"), ("dallas", "cowboys"),
        ("denver", "broncos"), ("detroit", "lions"), ("green bay", "packers"),
        ("houston", "texans"), ("indianapolis", "colts"), ("jacksonville", "jaguars"),
        ("kansas city", "chiefs"), ("las vegas", "raiders"), ("los angeles", "chargers"),
        ("los angeles", "rams"), ("miami", "dolphins"), ("minnesota", "vikings"),
        ("new england", "patriots"), ("new orleans", "saints"), ("new york", "giants"),
        ("new york", "jets"), ("philadelphia", "eagles"), ("pittsburgh", "steelers"),
        ("san francisco", "49ers"), ("seattle", "seahawks"), ("tampa bay", "buccaneers"),
        ("tennessee", "titans"), ("washington", "commanders"),
    ]),
    "nba": ("basketball", [
        ("atlanta", "hawks"), ("boston", "celtics"), ("brooklyn", "nets"),
        ("charlotte", "hornets"), ("chicago", "bulls"), ("cleveland", "cavaliers"),
        ("dallas", "mavericks"), ("denver", "nuggets"), ("detroit", "pistons"),
        ("golden state", "warriors"), ("houston", "rockets"), ("indiana", "pacers"),
        ("los angeles", "clippers"), ("los angeles", "lakers"), ("memphis", "grizzlies"),
        ("miami", "heat"), ("milwaukee", "bucks"), ("minnesota", "timberwolves"),
        ("new orleans", "pelicans"), ("new york", "knicks"), ("oklahoma city", "thunder"),
        ("orlando", "magic"), ("philadelphia", "76ers"), ("phoenix", "suns"),
        ("portland", "trail blazers"), ("sacramento", "kings"), ("san antonio", "spurs"),
        ("toronto", "raptors"), ("utah", "jazz"), ("washington", "wizards"),
    ]),
    "nhl": ("hockey", [
        ("anaheim", "ducks"), ("boston", "bruins"), ("buffalo", "sabres"),
        ("calgary", "flames"), ("carolina", "hurricanes"), ("chicago", "blackhawks"),
        ("colorado", "avalanche"), ("columbus", "blue jackets"), ("dallas", "stars"),
        ("detroit", "red wings"), ("edmonton", "oilers"), ("florida", "panthers"),
        ("los angeles", "kings"), ("minnesota", "wild"), ("montreal", "canadiens"),
        ("nashville", "predators"), ("new jersey", "devils"), ("new york", "islanders"),
        ("new york", "rangers"), ("ottawa", "senators"), ("philadelphia", "flyers"),
        ("pittsburgh", "penguins"), ("san jose", "sharks"), ("seattle", "kraken"),
        ("st louis", "blues"), ("tampa bay", "lightning"), ("toronto", "maple leafs"),
        ("utah", "mammoth"), ("vancouver", "canucks"), ("vegas", "golden knights"),
        ("washington", "capitals"), ("winnipeg", "jets"),
    ]),
}
# The words that say a title is from a sport, for ruling out the other sports.
SPORT_WORDS = {
    "baseball": ["baseball", "mlb"],
    "football": ["football", "nfl"],
    "basketball": ["basketball", "nba", "wnba"],
    "hockey": ["hockey", "nhl"],
}
# Product words that fit one sport, used when a profile gives none of its own.
CARD_WORDS = {
    "baseball": ["mlb", "topps", "bowman", "heritage", "stadium club", "allen", "ginter"],
    "football": ["nfl", "panini", "prizm", "donruss", "optic", "mosaic", "select"],
    "basketball": ["nba", "panini", "prizm", "hoops", "optic", "mosaic", "select"],
    "hockey": ["nhl", "upper deck", "young guns", "o-pee-chee", "opc"],
}


# Words that only fit one sport's cards: positions, gear and product lines that
# sport alone has. Titles of the NFL Cardinals rarely say "Arizona" or
# "football", but they do say "QB", "helmet" or "Phoenix". Product lines that
# exist for more than one sport (Prizm, Select, Immaculate, Donruss) aren't here.
ONLY_IN = {
    "football": ["qb", "wr", "rb", "helmet", "touchdown", "rookies & stars", "rookies and stars",
                 "xr", "obsidian", "phoenix", "spectra", "playbook", "plates & patches",
                 "gridiron", "score-a-treat", "super bowl", "pro bowl", "draft picks football"],
    "basketball": ["hoops", "nba hoops", "court kings", "dunk", "g league", "g-league"],
    "hockey": ["young guns", "o-pee-chee", "opc", "goalie", "stanley cup"],
    "baseball": ["mlb", "bowman", "1st bowman", "pitcher", "world series", "heritage high number"],
}


def league_of(sport: str) -> str | None:
    return next((key for key, (s, _) in LEAGUES.items() if s == sport), None)


def others_in_league(sport: str, nickname: str) -> list[str]:
    """Every other nickname in the team's own league."""
    league = league_of(sport)
    if not league:
        return []
    return [nick for _, nick in LEAGUES[league][1] if nick != nickname]


def same_nickname_elsewhere(sport: str, nickname: str) -> list[str]:
    """Cities of teams in other leagues with the same nickname: "arizona" for the Cardinals."""
    return sorted({city for key, (s, teams) in LEAGUES.items() if s != sport
                   for city, nick in teams if nick == nickname})


def other_sport_words(sport: str) -> list[str]:
    """Every word that says a title is from another sport."""
    return [w for s, words in SPORT_WORDS.items() if s != sport for w in words] + [
        w for s, words in ONLY_IN.items() if s != sport for w in words
        if w not in CARD_WORDS.get(sport, []) and w not in ONLY_IN.get(sport, [])]
