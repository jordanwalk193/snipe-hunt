<p align="center"><img src="docs/logo.png" width="120" alt="Snipe Hunt logo"></p>

# Snipe Hunt

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A self-hosted bot that hunts eBay for underpriced cards of **one player you collect**, and tells you what to snipe and for how much.

Every hour it searches eBay (including misspelled listings), throws out everything that isn't your player, and has Claude compare what's left with your inventory, your want list and recent sold prices. The steals come to your phone with a suggested max bid, and to a Home Assistant dashboard.

![The Today tab: what needs a snipe, the top pick, your snipes ending today and what to pay](docs/today.png)

<sub>Screenshots show the built-in demo data. Every card, price and seller in them is made up.</sub>

**Snipe Hunt only recommends.** It never bids, buys, makes offers, messages sellers or changes a Gixen snipe. It has no code that can.

## What it does

- **Finds steals.** Auctions ending soon, new Buy It Now and Best Offer listings, autos, patches, numbered cards, and typo listings other buyers miss.
- **Knows what you own.** It skips cards already in `inventory.csv` unless a copy is clearly better.
- **Sets a max.** Each pick comes with a snipe price where winning still beats recent sales, shipping counted, and says whether that came from real sales or an estimate.
- **Rates the deal.** One to five stars for how far under the going rate you would pay, shipping included: the asking price for Buy It Now, the max for an auction. Five stars is half price or better.
- **Alerts your phone** through Home Assistant or [ntfy](https://ntfy.sh), with quiet hours that urgent auctions can break through. Alerts are optional: without them, picks still reach the dashboard.
- **Watches limited drops.** A daily check for Topps Now and other print-to-order cards of your player, and a weekly check of new product checklists.
- **Keeps the inventory current** from your eBay and Gixen emails (optional, read-only).
- **Shows your Gixen snipes** next to its own picks, so you can see which good auctions have no snipe yet (optional, read-only).
- **Writes a weekly report**: wins and losses, what to pay, what to file, best upcoming snipes.
- **Shows what it costs.** Every Claude call's cost is added up per day and month and shown on the dashboard.

<p>
<img src="docs/picks.png" width="62%" alt="Every live pick with filters, deal ratings and the snipe record">
<img src="docs/phone.png" width="24%" alt="The dashboard on a phone">
</p>

## Try it in two minutes

Demo mode serves made-up data, so you can see the dashboard before you have any keys. It never touches eBay or Claude and sends no alerts.

```
git clone https://github.com/jordanwalk193/snipe-hunt && cd snipe-hunt
docker build -t snipe-hunt .
docker run -d -p 8766:8766 -e SNIPEHUNT_DEMO=1 -e API_TOKEN=demo snipe-hunt
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

Run any of them by hand, with `--dry-run` to see what it would do:

```
docker compose exec snipe-hunt python -m snipehunt.jobs hourly --dry-run
```

## Home Assistant dashboard

Install the [Snipe Hunt integration](https://github.com/jordanwalk193/ha-snipe-hunt) through HACS: add that address as a custom repository of type Integration, download it, restart Home Assistant, then add the **Snipe Hunt** integration. Give it the bot's address, `http://<bot machine>:8766`, and your `API_TOKEN`.

You get a five-tab dashboard, about fifty entities, an auctions calendar, and every setting as a switch or number: which tiers alert, the minimum deal rating worth an alert, quiet hours, the cheap-card limit, a monthly budget, watched sellers and extra searches.

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

- The API needs a bearer token on every request and can only do seven things: hide a pick, restore hidden picks, mark cards filed, refresh, start a search, re-read Gixen, send a test alert.
- Claude reads listing titles and emails written by strangers, so it runs with web search and web fetch only: no shell, no file access.
- Gmail and Gixen are read-only. Gixen allows one login at a time, so the bot and your browser sign each other out; after losing its session the bot waits two hours before signing back in.

## Questions people ask

**Will it bid for me?** No. It recommends a max and gives you the item number to paste into your sniper. There is no code in it that can bid, buy or make an offer.

**Is this allowed by eBay?** It uses eBay's official Browse API with your own developer keys and only searches. Sixteen or so searches an hour is far inside the default daily limit.

**What does it cost to run?** eBay's API is free. Claude is the cost, and it is not trivial: in the one setup this has run on, a single hourly run that scored 48 new listings (with the whole inventory in the prompt and a few sold-price lookups) came to about $0.50 at API prices on the CLI's default model. A quiet player costs far less; a busy one at that rate would be several dollars a day. Set `CLAUDE_MODEL` to a smaller model to cut it, and watch the dashboard's cost sensors for the first day before leaving it running.

**Does it work for football, basketball, hockey, Pokémon?** The searching and filtering work for any name. Set `EBAY_CATEGORY` if your cards aren't in Sports Trading Card Singles, and fill in `sport` and the word lists in your profile. It has only been run for real on one baseball player so far.

**Do I need Home Assistant?** For the dashboard, yes. Alerts alone work through ntfy without it.

**Can it follow more than one player?** One player per bot. Run a second copy with its own home folder and port for another.

**How good are the prices?** The max and the going rate come from Claude reading recent sold listings when it can, and estimating when it can't. Each pick says which. Treat them as a well-informed second opinion, and check anything expensive yourself.

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
