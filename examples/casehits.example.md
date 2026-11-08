# Case hits and priority cards

The cards the bot hunts first. Copy this file to `data/casehits.md` and make it
yours; the bot reads it on every run. Each row gets its own eBay searches (a
few rows per hourly run, round-robin, so every row is searched every few
hours; "case hit" and "ssp" are searched every run), its own price ceiling that
overrides steals mode's, the 🎯 label on its picks, and one phone alert the
first time a new copy is listed at or under the ceiling. Nothing here costs a
Claude call.

- **Kind**: `Case hit` (about one per case of the product, across the whole
  checklist), `SSP` (super short print), `Variation` (a short-printed photo or
  design swap of a base card), `Auto`, `Grail`, `Event` (cards tied to a
  moment), or `Hype`. A **Hype** row is a card sellers *call* a case hit or SSP
  that sells like a common: it never alerts, never lifts the ceiling, and the
  dashboard shows it as a warning.
- **Search** is the eBay search and also how a listing title is matched to the
  row: every word must be in the title, `-word` must not be, and `/10` means
  numbered to 10 or fewer. The player's name is implied when matching (typos
  still match).
- **Typical** is what it has sold for; the dashboard shows the bot's own sales
  record next to it. "no sale yet" is fine.
- **Ceiling** is the most you'd pay delivered. A listing at or under it alerts.
  It lifts steals mode's price ceiling only: its `skip_words` (1/1s,
  superfractors, printing plates) still drop a listing.
- Rows are matched top to bottom, first match wins, so keep specific rows
  above general ones (a numbered row above a "Hype" row for the same product).

| Card | Kind | How rare | Typical | Search | Ceiling |
|---|---|---|---|---|---|
| 2023 Topps Chrome Hidden Gems | Case hit | One of the 2023 Topps Chrome case hits; rookie year | ~$200 | jordan walker hidden gems | 180 |
| 2023 Bowman's Best HIM | Case hit | Bowman's Best case hit | ~$60 | jordan walker bowman's best him | 60 |
| 2023 Panini Select Color Wheel | Case hit | Panini Select case hit | ~$45 | jordan walker color wheel | 45 |
| Topps Series 2 Golden Mirror image variation | SSP | Flagship's SSP photo variation, one a year | ~$30 | jordan walker golden mirror -digital | 30 |
| 2020 Bowman Chrome Draft 1st Bowman Chrome auto | Grail | His first certified autograph | $200–650 raw | jordan walker cda-jw | 300 |
| 2026 Topps Holiday numbered /10 or less | SSP | Numbered to 10 or fewer: the truly rare Holiday cards | no sale yet | jordan walker holiday /10 | 40 |
| Topps Holiday "SSP" variations (no number) | Hype | Sellers call these SSP; they sell for $2–3 | ~$3 | jordan walker holiday ssp | |
