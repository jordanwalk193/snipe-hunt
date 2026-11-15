# Aftermarket Autos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In-person / through-the-mail (aftermarket) signatures, unproven autos and unconfirmed serials can never be priced as certified autos or become jackpots, steals, bid-nows or alerts.

**Architecture:** Pure word checks live in `filters.py`. A new read lane, `autocheck.py`, reads the description of each listing that claims an auto before it gets scored, the same way `specifics.enrich` works: one read-only item call per listing, ever, kept in a table. `_pick_dict` copies the flags onto the pick and `apply_caps` enforces them. That keeps them in force on every re-rate. One helper, `filters.doubted(pick)`, gates jackpot, bid-now, steals radar/drops and alerts. The ledger keeps IP sales in their own pool by giving them their own auto trait, `"ip"`.

**Tech Stack:** Python 3.11+, sqlite3, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-15-aftermarket-autos-design.md`

## Global Constraints

- Whole-word, case-insensitive matching. Strong words: `aftermarket`, `unauthenticated`, `not certified`, `IP`, `TTM`, `through the mail`, `in person`, `signed in person`, `no COA`. Weak words: `hand signed`, `sharpie`. Weak words count only without proof.
- Proof words: `Certified Autograph Issue`, `certified` not preceded by `not`/`un`, `on-card`/`on card`, `sticker auto`, `pack pulled`. Also an auto card number (`filters.AUTO_NUMBER`, plus `\b[A-Z]{1,5}A-[A-Z]{1,4}\b` style codes such as `BTPA-JW`, `CPA-JW`), and the item specific "Autograph Authentication" naming a maker (Topps, Bowman, Panini, Upper Deck, Leaf, Manufacturer).
- `IP_VALUE_CAP = 10.00`: the value of an aftermarket auto that has no IP sales in the ledger. `UNVERIFIED_SHARE = 0.25`. `IP_STARS = 2`: the most deal stars an aftermarket auto can get.
- New setting `show_ip_autos` defaults to `False`. New snapshot key `ip_autos`. Keys are added only, never renamed.
- eBay reads stay read-only, counted in the `"item"` lane, and are gated by `usage.ebay_allowed`. Tests mock eBay.
- Test fixtures use made-up item numbers and sellers. The reported title and description text are fine to use; the real item number and seller are not.
- Commits go on branch `aftermarket-autos` with `git -c user.email=jordanwalk193@users.noreply.github.com -c user.name=jordanwalk193 commit`, and each message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Nothing is pushed.
- Test command: `/tmp/claude-0/-root/fbf57c41-d22b-4b62-92cd-e965a74770e2/scratchpad/venv/bin/python -m pytest -q`, run from the repo root (baseline: 745 passed). Below, this is written as `PYTEST`.

## Review Focus

1. **A real certified auto in a seller's boilerplate.** "We never sell aftermarket or IP autos" sits in a store-wide template, so a negated strong word must not flag the listing. Test `test_negated_boilerplate_is_not_aftermarket` goes in Task 1.
2. **"IP" and "TTM" inside other words.** "IPA", "SHIPPING", "TTMs", "TTMRC" and "IP-" in card codes must not match. Test `test_ip_ttm_whole_words_only` goes in Task 1.
3. **Existing jackpot fixtures that are autos without proof.** Requiring proof for an auto jackpot is a behavior change. Fixtures that are real autos get `auto_proof=True`, and the change is called out in CHANGELOG (Task 4).
4. **The read cap is reached.** A listing that claims an auto but wasn't read this run has no proof, so it can't be a jackpot. It is read on the next run. Test `test_unread_auto_cannot_be_jackpot` goes in Task 4.
5. **A certified auto with a /99 that only item specifics show.** That one stays confirmed: no `serial_unconfirmed`, and it can still be a jackpot. Test `test_serial_in_specifics_is_confirmed` goes in Task 3.

---

### Task 1: Word checks in `filters.py`

**Files:**
- Modify: `snipehunt/filters.py` (after `fake_auto`, around line 80)
- Test: `tests/test_aftermarket_auto.py` (new)

**Interfaces:**
- Produces:
  - `filters.aftermarket_auto(title: str, description: str = "", specifics: dict | None = None) -> str | None`. Returns the matched word in lowercase, or None.
  - `filters.auto_proof(title: str, description: str = "", specifics: dict | None = None) -> bool`
  - `filters.says_auto(pick: dict) -> bool`. True when `sales.AUTO` matches `pick["title"]` or `pick["card"]`. Imports `sales` locally.
  - `filters.doubted(pick: dict) -> str | None`. Returns the first of `"ip_auto"`, `"auto_unverified"`, `"serial_unconfirmed"` that is truthy on the pick, or None.
  - `filters.AFTERMARKET_WHY = "aftermarket signature (IP/TTM), not a certified auto"`
  - `flag(item)` also appends `"aftermarket_auto"` when `aftermarket_auto(item.title, item.short_description + " " + item.description, item.aspects)` matches. (`Listing.description` is added in Task 2. Read it with `getattr(item, "description", "")` so this task stands alone.)

- [ ] **Step 1: Write failing tests**

```python
REPORTED_TITLE = "2023 Bowman Scouts Top 100 Chrome Auto #BTP-5 /99 Jordan Walker"
REPORTED_DESC = ("This is an aftermarket, unauthenticated signed piece with an autograph "
                 "obtained via reputable in-person (IP) or through-the-mail (TTM) methods.")

def test_reported_description_is_aftermarket():
    assert filters.aftermarket_auto(REPORTED_TITLE, REPORTED_DESC) == "aftermarket"

@pytest.mark.parametrize("text", ["IP auto", "ttm signed", "Signed in person", "NO COA",
                                  "through the mail", "not certified"])
def test_each_strong_word_flags(text):
    assert filters.aftermarket_auto(f"Jordan Walker {text}")

def test_ip_ttm_whole_words_only():
    for t in ["Jordan Walker auto IPA", "FREE SHIPPING auto", "TTMs lot", "IP-JW auto"]:
        assert filters.aftermarket_auto(t) is None

def test_weak_words_need_no_proof():
    assert filters.aftermarket_auto("Walker hand signed card") == "hand signed"
    assert filters.aftermarket_auto("Walker hand signed on-card auto") is None
    assert filters.aftermarket_auto("Walker sharpie auto", "Certified Autograph Issue") is None

def test_negated_boilerplate_is_not_aftermarket():
    desc = "Pack pulled. We never sell aftermarket or IP autos."
    assert filters.aftermarket_auto("Walker Chrome Auto", desc) is None

def test_proof_words():
    assert filters.auto_proof("x", "Topps Certified Autograph Issue")
    assert filters.auto_proof("2023 Bowman Chrome CPA-JW Auto")
    assert filters.auto_proof("x", "", {"Autograph Authentication": "Topps"})
    assert not filters.auto_proof("x", "not certified, signed in person")

def test_doubted_names_the_flag():
    assert filters.doubted({"ip_auto": True}) == "ip_auto"
    assert filters.doubted({"tier": "fire"}) is None
```

- [ ] **Step 2:** `PYTEST tests/test_aftermarket_auto.py`. Expected: FAIL, `AttributeError: aftermarket_auto`.
- [ ] **Step 3: Implement.** Negation means a match counts only if the 40 characters before it don't contain `never|no |not |don't|do not|won't` together with "sell" or "offer". Otherwise any of `never|not` directly before the word counts as negation. A strong match without proof always flags. A strong match with proof still flags when the word is `aftermarket`, `unauthenticated` or `not certified`. Other strong words need no proof present, because "IP" is ambiguous next to "Certified".
- [ ] **Step 4:** `PYTEST tests/test_aftermarket_auto.py tests/test_filters.py`. Expected: PASS.
- [ ] **Step 5:** Commit: "Filters: spot aftermarket (IP/TTM) autographs and proof of a certified one".

### Task 2: Read descriptions of listings that claim an auto (`autocheck.py`)

**Files:**
- Modify: `snipehunt/ebay_search.py` (Listing fields, around line 136; `enrich`). Add `description_text(raw)`.
- Modify: `snipehunt/item_cache.py`. Replace `DROP` with keeping the description as plain text.
- Create: `snipehunt/autocheck.py`
- Modify: `snipehunt/jobs.py`. Call it right after `specifics.enrich` (line 52) and in the fast-lane and backlog paths that call `filters.prefilter` (lines 421, 493), in a try/except like specifics.
- Modify: `tests/test_guards.py`. Add the new eBay path to the read-only guard list, following the existing pattern.
- Test: `tests/test_aftermarket_auto.py`

**Interfaces:**
- Produces:
  - `Listing.description: str = ""`: plain text, at most `DESCRIPTION_CHARS = 3000`.
  - `Listing.auto_proof: bool = False`
  - `ebay_search.description_text(raw: dict) -> str`: strips tags, unescapes entities, collapses whitespace, caps at 3000 characters.
  - `item_cache.put` stores `description` as `description_text(raw)` instead of dropping it.
  - `autocheck.enrich(con, listings, client=None, now=None, limit=PER_RUN) -> int`, with `PER_RUN = 25`, `LANE = "item"`, `KEEP_DAYS = 120`, and table `autocheck(item_id TEXT PRIMARY KEY, aftermarket TEXT, proof INTEGER, description TEXT, checked_at INTEGER)`. It looks at listings where `filters.AUTO_WORDS` matches the title, or where the specifics say Autographed: Yes. For each one it sets `item.description`, `item.auto_proof = filters.auto_proof(...)` and the `aftermarket_auto` flag via `filters.flag(item)`, then returns the number checked. A listing with a cached row needs no call.

- [ ] **Step 1: Failing tests.** `test_enrich_reads_auto_listings_once` uses a fake client whose `item()` returns `{"description": "<p>" + REPORTED_DESC + "</p>"}` and counts calls. Two `enrich` runs on the same listing make exactly 1 call, and both leave `"aftermarket_auto" in item.flags`. `test_enrich_skips_non_auto_titles` makes 0 calls. `test_enrich_stops_at_limit`: 3 auto listings with `limit=2` make 2 calls, and the third has no flag and `auto_proof is False`. `test_description_text_strips_html` checks that `"<b>Topps&nbsp;Certified</b>\n\n Autograph"` becomes `"Topps Certified Autograph"`. `test_cache_keeps_description_text` checks that `item_cache.put` followed by `get` returns `description` as plain text.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement** as specified. `enrich` checks `usage.ebay_allowed(LANE, con)` before each call and catches exceptions per item, like `specifics.enrich`. Delete rows older than `KEEP_DAYS`.
- [ ] **Step 4:** Run the full `PYTEST`. Expected: all pass, including the guards.
- [ ] **Step 5:** Commit: "Read the description of listings that say auto, once each".

### Task 3: The pick, its caps and the scoring prompt (`score.py`)

**Files:**
- Modify: `snipehunt/score.py`: `_pick_dict` (flags block around line 1010), `apply_caps` (line 139), `_compact` warning (line 554), `_filled` lane rules (line 646). Add `AFTERMARKET_RULE` next to `FAKE_AUTO_RULE`.
- Modify: `snipehunt/filters.py`. Add `serial_unconfirmed(card: str, title: str, specifics: dict, text: str) -> str | None`. It returns the run ("99") when `card` has `/NN` and none of the title, the specifics values or `text` do.
- Test: `tests/test_aftermarket_auto.py`

**Interfaces:**
- Consumes: Task 1 functions, and `Listing.description` and `Listing.auto_proof` from Task 2.
- Produces pick fields: `ip_auto: bool`, `aftermarket: str`, `auto_proof: bool`, `auto_unverified: bool`, `serial_unconfirmed: bool`. They are only present when they apply, except `auto_proof`, which is always set when the pick says auto. Constants: `score.IP_VALUE_CAP = 10.00`, `score.IP_STARS = 2`, `score.UNVERIFIED_SHARE = 0.25`.

- [ ] **Step 1: Failing tests.** Build the pick with the module's existing test helper for `_pick_dict`. Use the scorer reply from the incident: `card` is the reported title, `tier` "fire", `edge` "hidden", `value` 43, `max` 18.18, `max_basis` "sales". The listing is an auction at 0.99 + 1.99 with the seller's real title wording, `"Jordan Walker Topps 2023 Bowman Scouts Top 100 Bowman Chrome Refractor Autograph"`, and `description=REPORTED_DESC`, run through `filters.flag`.
  - `test_reported_listing_is_an_ip_auto_not_a_jackpot`: `pick["ip_auto"]`, tier `"yellow"`, `edge` None, `value <= 10`, `max <= value`, `deal <= 2`, and `jackpot.check(pick, NOW)` is None.
  - `test_auto_without_proof_far_under_value_is_unverified`: same listing with a plain description. Then `auto_unverified`, tier no better than `"yellow"`, and edge None.
  - `test_auto_with_proof_keeps_its_tier`: description "Certified Autograph Issue" and specifics showing Print Run 99. Tier stays `"fire"`, with no `auto_unverified` and no `serial_unconfirmed`.
  - `test_serial_only_in_card_name_is_unconfirmed`: `serial_unconfirmed`, tier `"green"` at most, and an edge of "hidden" is cleared.
  - `test_serial_in_specifics_is_confirmed`: specifics `{"Print Run": "99"}` gives no flag.
  - `test_ip_pool_comps_are_kept`: an ip_auto pick with `comps_n=3` keeps its ledger value and isn't capped at $10.
  - `test_prompt_warns_on_aftermarket`: `_compact(item)` has the `"warning"` key starting with `filters.AFTERMARKET_WHY`, and the built prompt contains `AFTERMARKET_RULE`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - In `_pick_dict`, set `ip_auto`/`aftermarket` from `item.flags` and set `auto_proof`. Set `serial_unconfirmed` from `filters.serial_unconfirmed(pick["card"], item.title, item.aspects, item.short_description + " " + item.description)`.
  - In `apply_caps`, in this order:
    1. ip_auto: without `comps_n`, `value = min(value, IP_VALUE_CAP)`. Then `max = pricing.odd_cents(min(max, value - ship))` when there is a max, tier `"yellow"`, edge None, `deal = min(deal, IP_STARS)` after `rerate`'s stars, and add `filters.AFTERMARKET_WHY` to the start of `why`.
    2. auto_unverified: says auto, no `auto_proof`, not ip_auto, and `price + ship < UNVERIFIED_SHARE * value` give tier `"yellow"` (from fire/green) and edge None. Otherwise pop the flag.
    3. serial_unconfirmed: fire becomes green, and an edge of "hidden" becomes None.
  - Every cap is idempotent, so `apply_caps(apply_caps(p)) == apply_caps(p)`. Add that assertion to the first test.
  - `AFTERMARKET_RULE` text: `- A listing with a "warning" of an aftermarket signature was signed in person or through the mail, not pulled from a pack. Price it as the base card plus a few dollars, never from certified autograph sales, and start its "why" with "aftermarket signature".`
- [ ] **Step 4:** Run the full `PYTEST`. Expected: all pass.
- [ ] **Step 5:** Commit: "Scoring: aftermarket autos priced as the base card, unproven autos and serials capped".

### Task 4: Gate jackpots, bid-now, steals and alerts; the ledger lane

**Files:**
- Modify: `snipehunt/jackpot.py`: `check()` (line 78), `scan()` (line 213 loop), `verify()` (line 266 `p.update`).
- Modify: `snipehunt/bidnow.py`: `_trusted` (line 42).
- Modify: `snipehunt/steals.py`: `radar` (line 177) and `dropped_into_range` (line 303). Skip a pick when `filters.doubted(p)` matches.
- Modify: `snipehunt/notify.py`: `worth_alerting` (line 199) returns False for `filters.doubted(pick)`.
- Modify: existing tests whose jackpot or bid-now fixtures are autos, so they carry `auto_proof=True`. Find them with `grep -ln "auto\|Auto" tests/test_jackpot.py tests/test_*bid*.py tests/test_steals*.py`.
- Test: `tests/test_aftermarket_auto.py`

**Interfaces:**
- Consumes: `filters.doubted`, `filters.says_auto`, `filters.aftermarket_auto`, `filters.auto_proof`, `ebay_search.description_text`.
- Produces: `jackpot.check` returns None when `filters.doubted(pick)` matches, or when `filters.says_auto(pick) and not pick.get("auto_proof")`. `scan` skips titles where `filters.aftermarket_auto(title)` matches. `verify` reads `description_text(raw)` and drops aftermarket candidates; otherwise it sets `p["auto_proof"]`.

- [ ] **Step 1: Failing tests.**
  - `test_doubted_picks_never_jackpot_bidnow_or_alert`: for each of the three flags, `jackpot.check` is None, `bidnow._trusted` is False and `notify.worth_alerting` is False. Quiet hours off, per the repo's test lesson.
  - `test_unread_auto_cannot_be_jackpot`: an auto pick with no `auto_proof` at 10% of its value gives None.
  - `test_certified_auto_still_jackpots`: the same pick with `auto_proof=True` gives `"sales"`.
  - `test_ledger_verify_drops_aftermarket`: a fake client returns the reported description, and `verify` leaves the candidate out.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement** as specified. Then update the existing auto fixtures, and add a CHANGELOG line: "Jackpots and bid-nows on an autograph now need proof it is certified."
- [ ] **Step 4:** Run the full `PYTEST`. Expected: all pass.
- [ ] **Step 5:** Commit: "Aftermarket and unproven autos never jackpot, bid-now or alert".

### Task 5: A separate price pool for IP sales (`sales.py`, `ledger.py`)

**Files:**
- Modify: `snipehunt/sales.py`: `traits` (line 538) and `item_traits` (line 522).
- Modify: `snipehunt/ledger.py`: `_row_fit` (line 151).
- Test: `tests/test_aftermarket_auto.py`

**Interfaces:**
- Produces: the auto element of `traits()` and `item_traits()` is `"ip"` when `filters.aftermarket_auto(title)` matches (in `item_traits`, also when the item has the `aftermarket_auto` flag), otherwise a bool as before. `_row_fit` returns None when `auto == "ip"`, so an IP sale never values an inventory row.

- [ ] **Step 1: Failing tests.** `test_ip_sale_is_not_same_card_as_certified`: `sales._same("2023 Bowman Scouts Top 100 Jordan Walker Auto TTM", "2023 Bowman Scouts Top 100 Jordan Walker Auto")` is False. `test_ip_sales_group_together`: two IP titles land in one `ledger.groups` group, separate from a certified one. Build a test DB with the same helpers `tests/test_ledger.py` uses.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** Import `filters` locally inside `traits` to avoid an import cycle.
- [ ] **Step 4:** Run the full `PYTEST`. Expected: all pass.
- [ ] **Step 5:** Commit: "Ledger: IP/TTM sales are their own price pool".

### Task 6: Hidden "IP autos" list, setting, docs

**Files:**
- Modify: `snipehunt/settings.py`: in `DEFAULTS`, add `"show_ip_autos": False` with the comment `# aftermarket (IP/TTM) autos stay off the picks list; they are listed under ip_autos`.
- Modify: `snipehunt/snapshot.py`: picks (line 506) leave out `ip_auto` picks unless the setting is on. Add the new key `"ip_autos"`: `[_slim(p) for p in ip][:MAX_PICKS]`. Bump `api.VERSION` only if this repo bumps it when it adds keys; check `git log -p snipehunt/api.py`.
- Modify: `README.md` (alerts/jackpot section), `docs/` FAQ if it covers jackpots, `CHANGELOG.md`.
- Test: `tests/test_aftermarket_auto.py`

- [ ] **Step 1: Failing test.** `test_ip_autos_hidden_by_default`: add one ip_auto pick and one normal pick (helper `add` from `tests/test_snapshot.py`). The snapshot `picks` has only the normal pick, and `ip_autos` has the IP one. With `settings.update(con, {"show_ip_autos": True})`, both are in `picks`. Monkeypatch `API_SETTINGS` if `update` refuses the key.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3: Implement**, then update the docs.
- [ ] **Step 4:** Run the full `PYTEST`. Expected: all pass. Also run `ruff check --isolated --select F snipehunt` (2 pre-existing findings are allowed).
- [ ] **Step 5:** Commit: "IP autos listed apart, hidden from picks by default; docs".

### Task 7: Port to the live bot (`/root/walker-pc`)

The live bot has no `filters.py`, no `item_cache.py`, no `short_description` and no fake-auto setter. Its listing match field is `.walker`, not `.match`, its `client.item(item_id)` takes only the id, and it is on `main` with real secrets in its config.

**Files:**
- Create: `src/autocheck.py`. It holds Task 1's word functions, `doubted`, `says_auto`, `serial_unconfirmed`, `description_text` and Task 2's `enrich` with its own table, all self-contained.
- Modify: `src/ebay_search.py` (the `description` and `auto_proof` Listing fields), `src/jobs.py`, `src/score.py`, `src/jackpot.py`, `src/bidnow.py`, `src/steals.py`, `src/notify.py`, `src/sales.py`, `src/ledger.py`, `src/settings.py`, `src/snapshot.py`. Same behavior as Tasks 2–6, adapted to the live names.
- Test: `tests/test_aftermarket_auto.py`, the same cases.

- [ ] **Step 1:** `git -C /root/walker-pc checkout -b aftermarket-autos`, then run its suite with `cd /root/walker-pc && /opt/walker-scout/bin/python -m pytest -q tests` and record the baseline. Before that, read `tests/conftest.py`: `GIXEN_USER` must be blanked, `CLAUDE_BIN=/bin/false`, and `PREFERENCES`/`WANTLIST` must be copies. Confirm the new `autocheck` table and eBay reads are mocked in every test that calls `jobs`.
- [ ] **Step 2:** Port the tests and confirm they fail.
- [ ] **Step 3:** Port the code.
- [ ] **Step 4:** Run the full suite and review `git status` before `git add` (lesson from the PREFERENCES overwrite). Expected: green, and only intended files changed.
- [ ] **Step 5:** Commit on the branch and stop. Merging to main and restarting the live services wait for the user's go-ahead.

### Task 8: Verify against the real incident

- [ ] **Step 1:** Write a read-only script that opens `/var/lib/walker-scout/seen.db` as `file:...?mode=ro`. It loads the stored pick for the incident listing and runs it, with the reported description, through the new live `apply_caps` and `jackpot.check`. Expected: `ip_auto`, yellow, value ≤ 10, check None.
- [ ] **Step 2:** Count how many of the live bot's current picks would change tier, for each of the three flags, and report the counts to the user. This shows how often the unverified-auto cap would bite real certified autos.
