# Must-have cards

The defining cards of each year: first cards, key rookies, the inserts and
autos you most want. Copy this file to `data/musthave.md` and make it yours;
the bot reads it on every refresh and the dashboard shows what you own, what
is for sale now and which copies are live picks. Nothing here costs a Claude call.

- **Search** is an eBay search (a `-word` leaves listings with that word out).
  The dashboard uses it for the eBay link and to count copies for sale now.
  The player's name may be in it or not; titles only need the other words.
- **Owned when** tells the bot which inventory rows count as owning it:
  `set words; card number; parallel words`, each part optional. Set words must
  all be in the row's set and card name (`-word` must not be). The card number
  must equal the row's. Parallel words must all be in the row's parallel;
  `Base` means the base card, `graded` a graded copy, `auto` an autograph,
  and `-word` must not be in it.
- **Going rate** is your own rough guide; the bot only shows it.

| Year | Card | Why | Going rate | Search | Owned when |
|---|---|---|---|---|---|
| 2020 | Bowman Draft Chrome 1st Bowman BD-57 | The key prospect card | ~$4 | jordan walker 2020 bowman draft chrome bd-57 -auto -refractor -psa -bgs -sgc | Bowman Draft Chrome; BD-57; Base |
| 2020 | Bowman Draft Chrome BD-57 Refractor | The classic parallel of the key card | ~$10 | jordan walker 2020 bowman draft chrome bd-57 refractor -auto | Bowman Draft Chrome; BD-57; Refractor -Mojo |
| 2020 | Bowman Draft Chrome BD-57, graded | A slabbed copy of the key card | $25–75 | jordan walker 2020 bowman draft chrome bd-57 psa -auto | Bowman Draft Chrome; BD-57; graded |
| 2023 | Topps Chrome rookie #209 | The flagship Chrome rookie | ~$3 | jordan walker 2023 topps chrome 209 -auto -refractor | Topps Chrome; 209; Base |
| 2023 | Topps Chrome rookie #209 Refractor | Its refractor | ~$5 | jordan walker 2023 topps chrome 209 refractor -auto | Topps Chrome; 209; Refractor |
