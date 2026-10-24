# Changelog

Snipe Hunt follows [semantic versioning](https://semver.org). The API snapshot
(`"api": 1`) is the contract with the Home Assistant integration: new keys may
appear in any release; renaming or removing one needs a new API version.

## Unreleased

### Fixed
- Picks showed the price they were scored at: an auction could read $0.99 on
  the dashboard while eBay was at $42. Each pick now takes price and shipping
  from the newest search, its deal is rated again, and an auction bid up to the
  bot's max leaves the list (`snapshot.current`).

### Google Sheet
- `sheets.py`: optional push of the hunt to a Google Sheet after every hourly
  run (and `python -m snipehunt sheets`). Tabs Collection, Picks, Sold, Offers,
  Lowball, Snipes and Data Sources are rewritten each time with RAW values (a
  title can't become a formula); Summary is written once, as formulas with a
  spending-by-month chart, and then left to you. A tab is grown before it
  overflows. Needs `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` and
  `google-auth` (now in requirements.txt). A failed push never stops the hunt.

### Deals the auctions miss
- Offers to send (`offers.py`): Buy It Now listings with Best Offer up 30+
  days, on cards you don't own, $3–$300, each with an odd-cent amount (70% of
  the ask, at most 80% of the going rate less shipping, none under half the
  ask). Snapshot key `offers`. Text only: the bot never makes an offer.
- Lowball snipes (`lowball.py`): auctions of $15+ cards still under half the
  going rate and ending within 48 hours, one low snipe each at a learned share
  of the going rate (starts at 35%, held to 20–60%, set so about one in five
  would win), dead-hour and easy-to-miss marks. Snapshot key `lowball`
  (ended targets left out). New job `lastcall` at :30, scheduled only when a
  target ends within three hours: a fresh eBay read (never the day's cache) and
  one phone alert for the ones still under 30% with at most one bid. Its eBay
  reads are their own lane, cut at 85% of the daily cap.
- Both price from picks, recorded sales and kept appraisals. Up to 20 listings
  nothing has priced get one Claude appraisal each (kept 30 days); setting
  `deal_appraisals` (default on) turns that off, and a team bot skips it once
  its daily budget is spent.
- Gixen snipes are checked for more than price (`snipecheck.doubts`): wrong
  player, already owned, sniped twice, CCG slab, passed on by the bot, new
  seller. Snapshot `gixen.snipes[].doubts` and `gixen.analysis.flagged`.
  Serious ones, and "over the going rate" by $3+, are pushed once each.
- Listings carry `listed_at` and `country` from eBay.

### It corrects its own guesses
- Guessed going rates are corrected per price band from how Claude's raw
  guesses sold (`calibrate.py`): shrunk toward 1 while results are few, only
  past `track_off`, held to x0.75..x1.40. An auction's max moves with its
  value; Claude's number is kept as `value_raw`. Setting `calibrate`
  (default on). Snapshot key `calibration`; shown in the weekly report and the
  self-review.

### Self-review
- Snipes on picks by tier and price band (sniped, won, lost, your max vs the
  bot's) and what won snipes cost against the going rate.
- Claude CLI failures raise `ClaudeError`, so they are counted apart.
- The prompt says which way `value_bias_pct` points.

### Reliability
- Claude costs are spooled to a file when the database is busy and added by
  the next call, instead of being lost.
- Scoring and the snipe check commit before every Claude call, so no run holds
  the write lock through one.

### Mid-tier steals lane
- A $20–$80 lane for mislabeled listings (`[search.midtier]`, `midtier.py`):
  misspellings searched first, auctions ending within 36 hours and Buy It Now /
  Best Offer listings in category 261328, item specifics read within a daily
  budget, eight detectors in `mislabel.py`, its own max (`deal_ratio` of the
  value, odd cents), "BIN ok" and offer-idea text, its own alert block with the
  item number on line two, and a daily summary. Home Assistant settings
  `midtier_enabled`, `midtier_deal_ratio`, `midtier_max_pay`. Recommend only.

### Every lane
- Junk-word filter (`[search] junk_words`, `lots`), fake-autograph flag, new-seller
  scam guard (`filters.py`).
- `[search] category_id` ("" for none), condition and photo count on every listing,
  one `price_filter()` that always adds `priceCurrency:USD`, EXTENDED search fields.
- eBay calls counted per lane, daily cap `ebay_daily_cap` with lanes cut in
  priority order past 80%, two retries on a rate limit, a same-day cache of
  single-listing reads; per-lane counts in the weekly self-review.
- Thin-comp star caps from recorded sales of the exact card; ledger value first.
- `data/known_cards.toml` (card numbers, years, mix-ups), read by the detectors,
  the card-number searches, the inventory check and win messages.
- Weekly refresh of eBay's item-specifics names (`aspect_refresh`).
- Sold prices from your won emails and `python -m snipehunt sale add`; the sold
  block names each price's source.
- Alerts: ET and PT end times, odd-cent auction maxes, "BIN ok ≤ $X", offer
  ideas, a Watch-outs line.
- `python -m snipehunt inventory check` and a weekly report section (read-only).
- Win messages with the binder page, combined-shipping reminders.
- `tests/test_safety.py`; tests can no longer reach the network.

## 0.1.0 (not yet released)

The first tagged release. Everything below is new.

### Hunting
- Hourly eBay search for one player: auctions ending soon, new listings, keyword
  searches, misspelled names, extra searches and watched sellers.
- **Team mode**: follow every player on one team (`[team]` in the profile).
  Searches page through a team's ~1,000 new listings an hour, a free filter drops
  lots, breaks, owned cards, cards outside the price band and plain base cards,
  and a steal-first ranking sends only the top `team_claude_per_run` to Claude
  within `team_daily_budget`. Seller bundles, a per-player view, enforced
  want-list ceilings, and other teams' players learned from Claude's rejections.
  See docs/TEAM_MODE.md.
- One-word names (a Pokémon) and a per-profile eBay category; example profiles
  for baseball, football, basketball, Pokémon and a team.
- Card-number searches for listings that leave the player's name out, price
  drops and relists, and an optional fast lane for brand-new Buy It Nows.

### Pricing
- Scoring by Claude with web search and web fetch only: tier, cheapskate max,
  going rate, one-to-five deal stars, "to win" bid, and an edge label for price
  mistakes, hidden listings and sleeper auctions.
- Sold prices recorded by the bot itself after every auction it saw closes
  (and fixed-price sales that took no offers), kept a year, used as comps.
- Guessed prices are capped and never alert a Buy It Now over `estimate_bin_cap`;
  low-feedback sellers are capped and flagged.
- **Spending**: spend by month, this month's pace against the budget, the usual
  price per kind of card, and a forecast of next month's spend and the next
  card's likely price, from `price_paid` and `order_date` in the inventory.

### Learning
- 👍 / 👎 feedback on picks, Gixen snipes as quiet "good calls", missed steals,
  corrections per price band.
- A weekly self-review of its own numbers, and bounded self-tuning of six
  settings that is undone when its metric gets worse.

### Around it
- Alerts through Home Assistant or ntfy, quiet hours, the bedtime bundle,
  fresh prices before a held alert goes out, one entry per card.
- Weekly report; daily limited-drops check; read-only inventory sync from eBay
  and Gixen emails; read-only Gixen snipe list with a check of each amount.
- A token-protected API for the Home Assistant integration, demo mode, Docker
  image, and a workflow that publishes it to GHCR on a version tag.

### Never
- It never bids, buys, makes offers, messages sellers or changes a Gixen snipe;
  `tests/test_guards.py` fails any change that loosens that.
