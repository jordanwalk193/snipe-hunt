# Changelog

Snipe Hunt follows [semantic versioning](https://semver.org). The API snapshot
(`"api": 1`) is the contract with the Home Assistant integration: new keys may
appear in any release; renaming or removing one needs a new API version.

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
