<p align="center"><img src="docs/logo.png" width="120" alt="Snipe Hunt logo"></p>

# Snipe Hunt

[![CI](https://github.com/jordanwalk193/snipe-hunt/actions/workflows/ci.yml/badge.svg)](https://github.com/jordanwalk193/snipe-hunt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A self-hosted bot that hunts eBay for underpriced cards of **one player you collect**, and tells you what to snipe and for how much.

Every hour it searches eBay (including misspelled listings), throws out everything that isn't your player, and has Claude compare what's left with your inventory, your want list and recent sold prices. The steals come to your phone with a suggested max bid, and to a Home Assistant dashboard.

![A tour of the dashboard: Today, Picks, Sold prices and Collection, shown with demo data](docs/tour.gif)

<sub>Screenshots show the built-in demo data. Every card, price and seller in them is made up.</sub>

**Snipe Hunt only recommends.** It never bids, buys, makes offers, messages sellers or changes a Gixen snipe. It has no code that can.

## What it does

- **Finds steals.** Auctions ending soon, new Buy It Now and Best Offer listings, autos, patches, numbered cards, and typo listings other buyers miss.
- **Knows what you own.** It skips cards already in `inventory.csv` unless a copy is clearly better.
- **Sets a max.** Each pick comes with a snipe price where winning still beats recent sales, shipping counted, and says whether that came from real sales or an estimate.
- **Learns what cards sell for.** After every auction it has seen closes, it asks eBay what the final bid was and keeps it, along with Buy It Nows that sold at their listed price. The next time the same card comes up, the price comes from that sale and not from a guess. See [Gets smarter over time](#gets-smarter-over-time). The dashboard lists the sales and keeps score: how many of its picks closed under the max it gave, and how far its going rate was from the real price.
- **Rates the deal.** One to five stars for how far under the going rate you would pay, shipping included: the asking price for Buy It Now, the max for an auction. Five stars is half price or better. A seller you already have an open order with, and who combines shipping, counts as $0 shipping.
- **Alerts your phone** through Home Assistant or [ntfy](https://ntfy.sh), with quiet hours and one "enter before bed" alert in place of overnight wake-ups. Before a held alert goes out its price is checked again, and the same card from several sellers arrives as one entry. Alerts are optional: without them, picks still reach the dashboard.
- **Watches limited drops.** A daily check for Topps Now and other print-to-order cards of your player, and a weekly check of new product checklists.
- **Keeps the inventory current** from your eBay and Gixen emails (optional, read-only).
- **Shows your Gixen snipes** next to its own picks, so you can see which good auctions have no snipe yet (optional, read-only).
- **Hunts for what slipped through the cracks**: price mistakes on Buy It Now listings, cards hidden by a misspelled or thin title, and auctions about to end with nobody watching. Each one is labelled on the dashboard and leads the phone alert.
- **Finds cards listed without the player's name**: it learns your player's card numbers from the listings it has seen ("2020 Bowman Draft BD-57"), searches for those, and keeps the ones that leave the name out. Nobody searching for the player sees them.
- **Watches for sellers who blink**: a Buy It Now cut by 15% or more, or a card that ended unsold and came back. No Claude cost.
- **Gives two bids for every auction**: the cheapskate max, and a "to win" bid at about the going rate, for the card you would rather own than steal. The scorecard shows how each would have done.
- **Has a fast lane for price mistakes** (off until you turn it on): every few minutes it checks brand-new Buy It Now listings and scores only the ones worth a look.
- **Keeps a price history per card**: every card it has seen sell more than once, with the trend, and "same card last sold for" on each pick.
- **Checks your Gixen amounts**: reads the snipes you set (never changes them) and says how each amount sits against the card's going rate: over it, a fair bid that should win, a real discount, or a long shot. A card it has no price for gets one Claude appraisal, kept so it is paid for once.
- **Ranks the rarest cards for sale**: the ten lowest print runs on eBay right now (1/1s, then /5, /10 and so on), read from the listing titles on every search at no Claude cost.
- **Writes a weekly report**: wins and losses, what to pay, what to file, best upcoming snipes, and what the week's auctions sold for.
- **Reviews and tunes itself.** A weekly self-review of its own numbers proposes fixes, and may adjust one bounded setting a week and put it back if things get worse. It never changes its own code. See [How it improves itself](#how-it-improves-itself).
- **Shows what it costs.** Every Claude call's cost is added up per day and month and shown on the dashboard.

<p>
<img src="docs/picks.png" width="62%" alt="Every live pick with filters, deal ratings and the snipe record">
<img src="docs/phone.png" width="24%" alt="The dashboard on a phone">

![Sold prices the bot recorded itself, and how its picks turned out against them](docs/sold.png)

![The rarest cards for sale right now, next to the Top 10 deals and the top pick (demo data)](docs/rarest.png)

![Price drops and relists, price history, the "to win" bid and the fast-lane tag (demo data)](docs/moves.png)
</p>

## Try it in two minutes

Demo mode serves made-up data, so you can see the dashboard before you have any keys. It never touches eBay or Claude and sends no alerts.

```
git clone https://github.com/jordanwalk193/snipe-hunt && cd snipe-hunt
docker compose -f docker-compose.demo.yml up -d
```

Then add the [Home Assistant integration](#home-assistant-dashboard) with the address `http://<this machine>:8766` and the token `demo`.

## What you need

- eBay developer keys (free, [developer.ebay.com](https://developer.ebay.com)). A Production keyset stays disabled until you answer eBay's account-deletion notification; see [eBay's deletion endpoint](#ebays-deletion-endpoint).
- An [Anthropic API key](https://console.anthropic.com) for scoring. This is the running cost: one scoring call an hour for new listings, plus a few web lookups. The dashboard shows what you've spent today and this month, so watch it for the first day or two.
- Somewhere to run it: Docker, or any Linux box with Python 3.11+.
- For the dashboard: Home Assistant with [HACS](https://hacs.xyz).

## Quick start with Docker

```
git clone https://github.com/jordanwalk193/snipe-hunt
cd snipe-hunt
cp .env.example .env        # then fill it in
docker compose up -d
```

The first start creates `home/data/` with starter files. The first search finds everything on eBay at once, so it sends a single welcome alert rather than one per pick; after that you only hear about new listings.

Edit these, then `docker compose restart`:

| File | What it is |
|---|---|
| `profile.toml` | Who you collect and how to tell their cards from everyone else's |
| `inventory.csv` | Every card you own or have coming |
| `wantlist.md` | What you're chasing and your price ceilings |
| `preferences.md` | How you buy, written like a brief to a friend bidding for you |

Check the setup (this never prints secrets):

```
docker compose exec snipe-hunt python -m snipehunt check
docker compose exec snipe-hunt python -m snipehunt.ebay_search --dry-run
```

The second command runs a real search and prints what it found without scoring or alerting. It's the fastest way to tune your profile.

## Setting up your player

Only the name is required:

```toml
[player]
name = "Paul Skenes"
```

From that, Snipe Hunt works out typo spellings and typo searches. It gets much sharper when you add a few lists, because many players share a name with someone in another sport:

```toml
collector = "Alex"

[player]
name = "Paul Skenes"
about = "the Pittsburgh Pirates pitcher"
sport = "baseball"
team_words = ["pirates", "pittsburgh", "lsu"]
card_words = ["topps", "bowman", "mlb"]
wrong_words = ["football", "basketball"]
```

How a title is judged:

1. Any `wrong_words` → dropped.
2. The player's name plus a team word, a card word or the sport → a sure match.
3. The player's name alone → "unclear", and Claude decides while scoring.
4. Just the last name plus a team word → "unclear", unless it's one of `other_first_names`.

`examples/profile.example.toml` is a complete, commented profile.

## The schedule

One process runs everything. Times are in `SNIPEHUNT_TZ`.

| When | What |
|---|---|
| Every hour, on the hour | Search, score, alert |
| 9:00am | Limited-drops check, inventory sync from email |
| Sunday 6:45pm | Weekly report (the first Sunday adds a value check, duplicates and grading candidates) |
| Sunday 7:30pm | Weekly self-review; see [How it improves itself](#how-it-improves-itself) |

Run any of them by hand, with `--dry-run` to see what it would do:

```
docker compose exec snipe-hunt python -m snipehunt.jobs hourly --dry-run
```

## Gets smarter over time

Nothing here needs setting up. The longer the bot runs, the more it has to go on.

- **Sold prices of its own.** Every auction it has seen is looked up once after it closes, and the final bid is kept for a year (365 days). That includes auctions it never found itself: every snipe in your Gixen list for your player is noted, so its closing price is recorded too.
- **Buy It Now sales that are clean.** A fixed-price listing that takes no offers can only sell at its listed price. Once one drops out of the search the bot asks eBay whether it sold, and keeps it as a fixed-price sale (marked BIN). Anything with Best Offer is never recorded, because the price it really went for isn't shown.
- **Comps that are the same card.** A sold price counts for a listing only when the grade, autograph and print run match. The same card number and year match even when the titles share few words, and a different card number never matches. A sale of another grade or print run is shown to Claude as a hint, one per listing, marked "(different grade/parallel)".
- **Your taste.** When you hide a pick, the bot keeps what it was. When you set a snipe, it keeps your amount next to its own max. Scoring is shown the latest fifteen of each, picks fewer cards like the hidden ones and more like the sniped ones, and leans toward your maxes where they keep running above or below its own.
- **"Bad pick" and "good call".** Each pick on the dashboard has a 👎 button that asks what was wrong (overvalued, wrong card, bad seller, don't want this type, already have it, or something else, with an optional note of up to 200 characters) and hides the pick, and a 👍 "good call" button that leaves it on the board. The bot keeps the pick as it saw it (tier, price, its value and max, whether that was from sales or a guess, the seller), the newest 300 at most. A snipe you set in Gixen on one of its picks counts as a quieter "good call", with your amount next to its max. Also available as the `feedback` API action and the `snipe_hunt.pick_feedback` service. Scoring is shown the newest twenty, fenced off and marked as data (a note can't give Claude instructions), with a rule to pick fewer cards like the ones marked overvalued or wrong card, or value them much lower, and more like the good calls. A new listing of the same card as one marked overvalued carries a note saying so.
- **Missed steals.** A sale it made no pick for that closed 35% or more under the usual price of the same card (at least two earlier sales) is a missed steal. The weekly report lists the week's ten biggest, the dashboard counts them, and scoring is shown the latest five as the kind of listing to watch for.
- **Corrections per price band.** Once enough of its picks have sold, the bot tells Claude how far off its going rates have been running, separately for cards valued under $10, $10 to $50 and over $50, since cheap and dear cards tend to miss in different directions.

Where a sale didn't state its shipping, $5 is assumed, the same as everywhere else, and the total is marked as estimated.

## How it improves itself

Three levels, from fully automatic to one that waits for you. The rule across all of them: **the bot never changes its own code.** It can change a few of its own settings, inside fixed bounds; code changes only arrive as pull requests a person merges.

**1. A weekly self-review (automatic).** On Sunday evening, after the weekly report, the bot adds up its own numbers: how its picks sold overall and per price band, the steals it missed by type of card, how many picks you hid or sniped and how your maxes compare with its own, alerts sent, held and dropped as stale, what Claude cost per job and per pick, eBay calls per run, failed runs and Gixen sign-outs. It shows them to Claude next to the four weeks before and asks what it is getting wrong and what the three to five most valuable fixes are, each with its evidence, expected effect, a way to measure it and its risk. The result is written to `reports/reflect-YYYY-MM-DD.md`. It also follows up the last four reviews: which proposals were done, and whether the metric each one aimed at moved.

The review sees numbers and nothing else. Listing titles, seller names and email text are written by strangers, so none of it is ever put in front of a prompt that can lead to a change. Claude gets no tools for this call: no web, no shell.

With `GITHUB_REPO` and `GITHUB_TOKEN` set, the week's proposals also go to one GitHub issue, "Weekly self-review YYYY-MM-DD", labelled `self-review`, as a checklist. Use a fine-grained token for that one repository with **Issues read and write only**. Without a token you get a short phone alert that links the report. On a public repository the issue is public, and it holds your week's numbers (counts, percentages, Claude cost).

**2. Bounded self-tuning (automatic, reversible).** The review may change one setting a week, from this list and no other, inside these bounds:

| Setting | What it does | Default | Range | Most it moves in a week |
|---|---|---|---|---|
| `edge_min_pct` | How far under the going rate a card must be to be called a price mistake, hidden or a sleeper | 25 | 15 to 40 | 5 |
| `min_overlap` | Share of title words a sold price must share with a listing to count as a comp | 0.5 | 0.4 to 0.7 | 0.05 |
| `track_off` | How far off the going rates must run before scoring is told | 10 | 5 to 20 | 5 |
| `price_lookups` | Sold-price web lookups per scoring call | 8 | 0 to 12 | 2 |
| `fast_min_price` | Asking price at which a plain card is worth a fast-lane call | 15 | 5 to 30 | 5 |
| `yellow_max_total` | The cheap-card alert limit | 2 | 1 to 4 | 0.5 |

Every change is logged with its reason and the metric it is judged by, measured over the four weeks before. Two weeks later the metric is measured again. Worse by more than noise (15% of itself, and at least 3 points) and the setting is put back. Fewer than 8 results before or after is "inconclusive, keep watching", for up to six weeks. A setting you have changed by hand since is never put back. The log is on the dashboard and in the weekly report, and the `self_tuning` switch freezes all of it. Never tunable: anything about the read-only rules, quiet hours, which alerts are on, credentials or how the bot signs in to Gixen.

**3. A monthly improvement pull request (you approve).** [`docs/SELF_IMPROVE.md`](docs/SELF_IMPROVE.md) holds the instructions for a Claude Code session, run by you or on a schedule and never by the bot itself: read the open `self-review` issues and the last four reviews, pick the single most valuable code proposal, implement it with tests on a branch, and open a pull request with the problem, the evidence, how to measure success and how to roll back. It never merges and never pushes to main. `make improve` prints the instructions.

`tests/test_guards.py` runs in CI on every pull request and fails if a read-only guard goes missing: Claude's tool limits, the "never bid or buy" rules in the prompt, Gixen being read-only, the price check on every pick, the self-review having no tools. No pull request, including one Claude writes, can quietly drop them.

Run a review by hand, without changing anything:

```
docker compose exec snipe-hunt python -m snipehunt.jobs reflect --dry-run
```

## Alerts and quiet hours

- **Quiet hours** (11pm to 7am unless you change them) hold alerts until morning. A limited drop still comes through.
- **Bedtime bundle** (on by default): on the first search of quiet hours you get one alert, "enter before bed (N)", listing every alert-worthy auction that ends before quiet hours are over or within two hours after. Gixen does the sniping, so nothing wakes you for an auction during the night. Turn `bedtime_bundle` off for the older behaviour, where an auction ending within `urgent_hours` breaks through quiet hours.
- **Fresh prices.** A pick that was held overnight, or left over from a full run, takes the price from the latest search before it is sent. An auction already bid past its max is dropped, and so is a Buy It Now that has been out of the search for two hours.
- **One entry per card.** When several sellers list the same card, the alert shows the card once with a line per listing (price, shipping, end time, max, item number) and a reminder that a Gixen group bid covers them all.
- **Combined shipping.** Scoring is told which sellers you have an unpaid or unshipped order with. For the ones named in `combined_ship_sellers`, the deal stars count $0 shipping and the alert says "combined ship w/ open order".

## Home Assistant dashboard

Install the [Snipe Hunt integration](https://github.com/jordanwalk193/ha-snipe-hunt) through HACS: add that address as a custom repository of type Integration, download it, restart Home Assistant, then add the **Snipe Hunt** integration. Give it the bot's address, `http://<bot machine>:8766`, and your `API_TOKEN`.

You get a five-tab dashboard, about fifty entities, an auctions calendar, and every setting as a switch or number: which tiers alert, the minimum deal rating worth an alert, quiet hours, the bedtime bundle, the cheap-card limit, a monthly budget, watched sellers, sellers who combine shipping and extra searches.

## Without Docker

```
git clone https://github.com/jordanwalk193/snipe-hunt /opt/snipe-hunt && cd /opt/snipe-hunt
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
.venv/bin/python -m snipehunt init          # creates /var/lib/snipe-hunt
```

Put your secrets in `/var/lib/snipe-hunt/env` (same names as `.env.example`, mode 600), then install `deploy/snipe-hunt.service`. `SNIPEHUNT_HOME` moves the home folder.

If `data/` is a git repository, Snipe Hunt pulls before it edits the inventory and commits (and pushes, if there is a remote) after. Keep that repository private: it holds what you paid and what you're chasing.

## eBay's deletion endpoint

eBay disables a Production keyset until your app answers its Marketplace Account Deletion challenge at a public HTTPS address. `deploy/ebay_deletion.py` is a tiny standalone server that does exactly that and nothing else. Run it with `EBAY_VERIFICATION_TOKEN` and `EBAY_DELETION_ENDPOINT` set, put it behind your reverse proxy, and enter the same address and token on developer.ebay.com.

## Safety

- The API needs a bearer token on every request and can only do eight things: hide a pick, give feedback on a pick, restore hidden picks, mark cards filed, refresh, start a search, re-read Gixen, send a test alert.
- Claude reads listing titles and emails written by strangers, so it runs with web search and web fetch only: no shell, no file access.
- Gmail and Gixen are read-only. Gixen allows one login at a time, so the bot and your browser sign each other out; after losing its session the bot waits two hours before signing back in.

## Questions people ask

**Will it bid for me?** No. It recommends a max and gives you the item number to paste into your sniper. There is no code in it that can bid, buy or make an offer.

**Is this allowed by eBay?** It uses eBay's official Browse API with your own developer keys and only searches. Sixteen or so searches an hour is far inside the default daily limit.

**What does it cost to run?** eBay's API is free. Claude is the cost, and it is not trivial: in the one setup this has run on, an hourly run that scored 48 new listings came to about $0.45 at API prices on the CLI's default model. About 40% of that was sold-price web lookups, and most of the rest was Claude reading the prompt and writing its answer. A quiet player costs far less; a busy one at that rate would be several dollars a day. Three things cut it:

- **Price lookups** (a setting in Home Assistant, 0 to 12, default 8): how many sold prices Claude may look up per run. At 0 it prices every card from what it knows and uses no web tools. In that same setup only about a third of the picks worth looking up ended with a real sold price, so fewer lookups costs less accuracy than you might expect. Replaying those 48 listings with lookups at 0 cost $0.25 and picked the same 9 cards, with lower maxes on three of them.
- **`CLAUDE_EFFORT=medium`** in `.env`: Claude thinks less before answering. On a replay of the same 48 listings it cost roughly a tenth less and found 8 of the same 9 picks, with two of them a tier lower.
- **`CLAUDE_MODEL`** in `.env`: a smaller model costs much less per run. Not measured here; compare its picks with the default for a day before trusting it.

Watch the dashboard's cost sensors for the first day before leaving it running.

**Does it work for football, basketball, hockey, Pokémon?** The searching and filtering work for any name. Set `EBAY_CATEGORY` if your cards aren't in Sports Trading Card Singles, and fill in `sport` and the word lists in your profile. It has only been run for real on one baseball player so far.

**What is the fast lane and what does it cost?** A badly mispriced Buy It Now can be gone in minutes, long before the hourly search. Set `FAST_LANE_MINUTES=10` in `.env` and the bot makes one extra eBay search that often for the newest fixed-price listings. A listing it has never seen goes to Claude only when it is worth a look: numbered to 99 or fewer, an autograph, graded, an unclear title, asking $15 or more, or priced under a recorded sale of the same card. At most 12 listings a call and 30 calls a day, so the ceiling is a few dollars a day and most runs cost nothing. Everything else waits for the hourly run.

**Do I need Home Assistant?** For the dashboard, yes. Alerts alone work through ntfy without it.

**How little do I have to set up for another player?** A name, a sport and a few team words: see [`examples/profile.minimal.toml`](examples/profile.minimal.toml), a football quarterback in fifteen lines. Misspellings, searches and card numbers are worked out from there.

**Can it follow more than one player?** One player per bot. Run a second copy with its own home folder and port for another.

**How good are the prices?** They get better the longer it runs. eBay blocks its sold-listings pages, so at first most prices are Claude's estimate, with a web lookup for the dearer cards. But the bot also records the final bid of every auction for your player that it saw close (one free eBay API call each, kept for a year), and shows Claude the sales of the same card as the one being scored. Each pick says whether its max came from real sales or an estimate. A Buy It Now is recorded only when it takes no offers, because then it can only have sold at the listed price; one with Best Offer may have gone for a lower accepted offer, so it isn't counted. Treat the prices as a well-informed second opinion, and check anything expensive yourself. Once enough of its picks have sold, the bot also tells Claude how far off its going rates have been running in each price band, so the estimates correct themselves. More in [Gets smarter over time](#gets-smarter-over-time).

**Can I look up a price myself?** Yes. `python -m snipehunt sold gold refractor /50` lists every recorded sale with those words in the title, with the range and the typical price. With no words it lists the latest sales.

```
$ docker compose exec snipe-hunt python -m snipehunt sold gold refractor
2026-10-11 | $41.00 +4.99 ship, 14 bids | 2023 Topps Chrome Sam Example Gold Refractor /50 #45 RC
2026-10-04 | $36.50 +1.25 ship, 9 bids | 2023 Topps Chrome Sam Example Gold Refractor /50 #45

2 sales · $37.75 to $45.99 with shipping · typical $41.87
```

## License

MIT. See `LICENSE`.

## Development

```
pip install -r requirements.txt pytest
pytest
```

## Support

Snipe Hunt is free. If it finds you a steal and you'd like to say thanks:

[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white)](https://paypal.me/jordanwalker1531)
