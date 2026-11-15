# Aftermarket autographs: never a jackpot

Short design, approved 2026-11-15. Applies to Snipe Hunt and the live bot.

## Why

A base Scouts Top 100 refractor with an in-person sharpie signature was alerted
as a JACKPOT, priced against certified (pack-pulled, numbered) autographs. The
seller's title said "Autograph" only; the scorer's card name added "Auto /99",
called it a "hidden" edge, and took its value from two web-found sales of the
certified card, labelled as sales. The description, which said "aftermarket,
unauthenticated ... in-person (IP) or through-the-mail (TTM)", was never read:
search gives only a 600-character short description and the item cache drops
the full one.

## Goals

- An aftermarket (IP/TTM, unauthenticated) autograph is never comped against
  certified autograph sales, never a jackpot, never in the steals/fire lanes.
- A claimed auto with no proof, priced far under its comp, can't be a jackpot.
- A print run that only the scorer's card name claims is not trusted.

Non-goals: authenticating signatures; a separate price model for IP autos.
Anti-goal: a certified auto that is a real steal losing its jackpot to a loose
word match (e.g. "IPA", "ship", "TTMs" in another sense).

## Approaches

1. **Word check on title + description, enforced in code after scoring (picked).**
   Deterministic, testable, works on the ledger lane that never sees Claude.
2. Prompt rule only (tell Claude to spot IP autos). Cheap, but the scorer never
   saw the description and was already wrong; not enforceable.
3. Drop aftermarket autos as junk before scoring. Simple, but loses a real (if
   small) card the collector may want, and the user asked for a hidden bucket.

## Design

- `filters.aftermarket_auto(title, description, specifics)`: whole-word,
  case-insensitive. Strong words (alone enough): aftermarket, unauthenticated,
  not certified, IP, TTM, through the mail, in person, signed in person, no COA.
  Weak words (hand signed, sharpie) count only when nothing says certified
  (Certified Autograph Issue, on-card, sticker auto, pack pulled, an auto card
  number). Flag `aftermarket_auto` on the listing.
- Description: listings read in full keep a plain-text copy of the
  description (HTML stripped, capped). Every pick that claims an auto (title,
  specifics or card name) gets one read-only item read before caps/jackpots.
- Comps: `sales.traits` reports an aftermarket title's auto as a separate kind,
  so ledger groups and `_same` keep IP sales in their own pool. A flagged pick's
  value is the base card's ledger value plus a small premium, capped at about
  $10 when unknown. Prompt gets a rule like the fake-auto one.
- Tiers: flagged picks go to yellow, edge cleared, deal stars capped, never
  jackpot, steals, bid-now or alerts; listed under a new snapshot key
  `ip_autos`, hidden from picks unless setting `show_ip_autos` (default off).
- Auto unverified: says auto, nothing certifying in description/specifics, no
  auto card number, and paying under 25% of the value → `auto_unverified`:
  yellow at most, no edge, no jackpot. ("Worth a look" maps to yellow.)
- Serial unconfirmed: the pick's card name has /NN that title, specifics and
  description lack → `serial_unconfirmed`: no jackpot, no "hidden" edge,
  green at most.
- Ledger jackpot lane skips aftermarket titles and checks the description it
  already reads in `verify`.
- Regression test: the reported title and description → not a jackpot,
  flagged aftermarket. Plus the real seller title, false-positive words, and a
  certified auto that still is a jackpot.

## Left to the builder

Exact premium and cap constants; where the extra item read sits in the hourly
run; how the live bot's module names differ (`.walker` vs `.match`).
Both bots: Snipe Hunt first, then the same patch to the live bot, tests green
in each, no push.
