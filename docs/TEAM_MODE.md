# Team mode

Player mode follows one player. Team mode follows one team, every player on it,
and is built for collecting that team as cheaply as possible: steals first,
the cheapest copy of a card first, one shipping charge for several cards.

One bot follows a player or a team, never both. To do both, run two copies,
each with its own home folder and port. A profile with both `[player]` and
`[team]` is an error.

## Setting up

```
python -m snipehunt init --team      # or copy examples/profile.team.example.toml to data/profile.toml
```

Only the name is required:

```toml
[team]
name = "St. Louis Cardinals"
```

From that, Snipe Hunt works out the city words ("st. louis", "st louis"), the
nickname ("cardinals"), three misspelled nicknames to search for, product
words for the sport, and the words that rule a title out: other sports'
words, positions and product lines only another sport has ("QB", "helmet",
Panini Phoenix), and the cities of other leagues' teams with the same
nickname ("arizona" for the NFL Cardinals). Colleges and high schools have to
be listed by hand in `wrong_words`.

| Setting | What it does |
|---|---|
| `name` | City and nickname, as the team is known |
| `sport` | baseball, football, basketball or hockey |
| `city_words`, `nickname_words` | How titles write them; default from the name |
| `roster` | Players to put first, most wanted first. Each gets a search of its own (up to 8) |
| `roster_any_team` | `true`: a roster player's card from another team counts too |
| `logo_only` | `true`: unlicensed cards (Panini, Leaf) with no team logos don't count |
| `card_words` | Product words that confirm the sport; a short list per sport by default |
| `wrong_words` | Words that mean it is something else; added to the automatic ones |
| `auto_wrong_words` | `false` turns the automatic ones off |
| `search` | What eBay is asked for; the nickname by default |

`examples/profile.team.example.toml` is a complete, commented profile.

## How a title is judged

1. A wrong word, or a player Claude has found on another team's cards → no.
2. The city and the nickname → yes.
3. The nickname and a card or sport word → yes.
4. A roster player and a team word → yes. A roster player alone → unclear, and
   Claude decides. A roster player with another team in the title → no.
5. The nickname alone → unclear.

A misspelled nickname ("Cardnals") counts as the nickname and is marked, since
that is where other buyers' searches miss a card. A title naming another team
from the same league (a dual auto, league leaders) is a combo card: kept when
a roster player is on it, scored a little lower, and Claude values only the
team's part.

**It learns.** When Claude rejects a card as another team's, it names the
player (a football player on the NFL Cardinals, say). Those names rule titles
out for free from the next search on, so the same mistake is paid for once.
Only plain names are kept, never a roster player's, and at most 400.

## Volume and cost

A team lists far more than a player. Measured on eBay for the Cardinals:

| | New in an hour | New in a day | Auctions ending in a day |
|---|---|---|---|
| One player (Jordan Walker) | 13 | 460 | 1,006 |
| The team ("cardinals") | 1,128 | 33,005 | 44,627 |

Sending all of that to Claude would cost something like $100 a day. Team mode
filters for free first, and only the top of what is left reaches Claude.

**Searches** (about 19 eBay calls an hour, 200 listings a page): the nickname by
ending-soonest auctions and newest listings (two pages each; the newest skip
anything under the price floor at eBay's end), each keyword (`auto`, `patch`,
`/`, `rookie`) by newest and by ending soonest, each roster player, the
misspelled nicknames, and any extra searches and sellers.

**Free filter** (`shortlist.py`), in order, each counted for the dashboard:

| Dropped | Why |
|---|---|
| not a card | breaks, boxes, packs, reprints, customs, digital |
| lot | lots, team sets, "you pick", "50 cards" (unless `team_lots` is on) |
| owned | the same year, card number, player, print run, autograph, grade and parallel as an inventory row |
| too dear | over `team_max_price` |
| combo | another team's card with no roster player on it |
| plain | no reason to look: a rookie card or a roster player's base card at the going rate isn't a steal |
| too cheap | a Buy It Now under `team_min_price` with no strong signal (an auction is never floored) |

**Ranking.** What is left gets points, with evidence of a steal counting most:

| Signal | Points |
|---|---|
| Under a recorded sale of the same card | 1 per 8% under, up to 8 |
| Price cut 15% or more | 3 |
| Want list | 3 |
| Misspelled title | 2.5 |
| Auction ending within 6 hours with no more than 1 bid | 2.5 |
| 1/1, /10, /25, /50, up to `team_max_run` | 4, 3, 2, 1.5, 1 |
| Autograph, patch | 2, 1.5 |
| Roster player | 2.5 for the first, less down the list |
| Relisted | 1.5 |
| Rookie, gem mint | 1, 0.5 |
| Cheap ($10 or less with shipping) | 0.5 |
| Unclear match | −0.5 for a roster player, −2.5 for a bare nickname |
| New seller on a card over $30, combo card | −1 |

A cheap Buy It Now still gets a look under the price floor when it has a
strong signal (an autograph, a patch, /25 or lower, a price cut, a recorded
sale it is under, or the want list): a cheap copy of a good card is what a
frugal collection is built from.

**Claude.** The best-ranked new listings, up to `team_claude_per_run` (30), go
to one scoring call. Listings not sent stay in line, so an auction climbs as
it nears its end. When `team_daily_budget` ($5) has been spent today, the bot
still searches, filters and ranks, and shows the free ranking on the
dashboard, but scoring waits for tomorrow.

**Measured.** One live run: 19 eBay calls, 2,060 listings, 782 worth a look,
30 to Claude. That call cost **$0.31** with 4 price lookups and made 10 picks.
Every hour at that rate is about $7.50 a day; the $5 budget caps it. Lower
`team_claude_per_run` or `price_lookups` to spend less.

Only listings the filter passed get their sold price looked up when they
close (`seen.tracked`), since a team's tens of thousands of auctions a day
would swamp eBay's 5,000-call daily limit. Passed-over listings are forgotten
after 30 days.

## Settings (team bots only)

| Setting | Default | Range |
|---|---|---|
| `team_claude_per_run` | 30 | 0 to 80 |
| `team_daily_budget` | $5 | $0 (no limit) to $500 |
| `team_min_price` | $3 | $0 to $100 |
| `team_max_price` | $250 | $1 to $10,000 |
| `team_max_run` | 99 | 1 to 999 |
| `team_lots` | off | |

A team bot also defaults to `price_lookups` 4 and `alert_min_stars` 4: only
real steals reach the phone. Every setting can be changed from Home Assistant.

## Scoring

The team prompt is player mode's with the player-only lines swapped out, so
the hard rules are the same text (a guard test checks). It tells Claude that
the collector is building the collection as cheaply as possible: real
discounts over nice cards at fair prices, and the cheapest copy of a card. A
fair price is a pick only when the card is on the want list and under its
ceiling. It also gets the roster, in order, and what the collector usually
pays per kind of card (from the inventory), as an anchor for the max.

Every pick says who is on it (`player`). Deal stars, the cheapskate max, the
"to win" bid, the guessed-price caps and the seller-trust rules are the same
as in player mode.

**Want-list ceilings are enforced in code.** When Claude says a pick matches a
want-list row whose Ceiling is a dollar amount, a Buy It Now over the ceiling
(shipping counted) is dropped and an auction's max is cut to fit. A ceiling
Claude names that isn't on the want list is ignored.

**Inventory.** Add a `player` column to `inventory.csv` for a team collection.
It makes the free "already owned" check sharper, and only the rows that could
be one of the listed cards (same player, or same card number) are sent to
Claude, since a team collection can run to thousands of rows.

**Sold prices** are per card, whoever is on it: on a team bot the player's
name stays in the comparison, a sale naming a different roster player is
never a comp, and titles without a card number must be 70% alike.

## On the dashboard and the phone

The snapshot gains a `team` section on team bots:

- `board`: the 25 deepest discounts to the going rate (no guessed prices, two
  stars or more), biggest discount first.
- `players`: each player's live picks, the best steal, the cheapest card and
  the dollars under value.
- `funnel`: found, dropped by reason, worth a look, new, sent to Claude.
- `candidates`: the free top 15, with why each ranked where it did. Shown
  even when the budget is spent.
- `bundles`: sellers with three or more worth-a-look cards, and the shipping
  saved if they combine.
- `budget`: today's spend against the daily budget.

Alerts are titled "N Cardinals steals", name the player on each card, and lead
with the deepest discount.

## For the Home Assistant integration

What `ha-snipe-hunt` needs to support team mode:

- More than one bot: `single_config_entry` blocks a player bot and a team bot
  side by side. One entry per bot address, with entity and device IDs per entry.
- `profile.mode`, `profile.team`, `profile.roster`. `profile.player` holds the
  team name on a team bot, so the existing eBay search link keeps working.
- Panels for `team.board` with player filter chips, `team.players`,
  `team.funnel`, `team.bundles`, `team.candidates` and a budget-reached banner.
- Numbers and a switch for the six team settings.
- Sensors: team spend today against the budget, listings sent to Claude per run.
- `player` on pick cards and in the feedback form.
- Both modes: `spending` (months, forecast, usual price per kind) and
  `usual_paid` on each pick.
