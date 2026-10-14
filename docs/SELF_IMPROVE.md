# The monthly improvement pull request

Snipe Hunt improves itself on three levels. The first two run inside the bot;
this file is the third.

1. **Weekly self-review** (`snipehunt/reflect.py`, automatic): the bot adds up
   its own numbers, asks Claude what it is getting wrong, and writes
   `reports/reflect-YYYY-MM-DD.md` and one GitHub issue labelled `self-review`.
2. **Bounded self-tuning** (`snipehunt/tune.py`, automatic, reversible): at
   most one settings change a week, inside fixed bounds, put back if its
   metric gets worse.
3. **Monthly improvement pull request** (this file, a person approves): one
   code change, proposed as a pull request that Jordan merges.

**The bot never changes its own code on main.** Code changes only arrive as
pull requests that Jordan merges. The steps below are run in a Claude Code
session (on CT 111, by Jordan or on a schedule), never inside the bot's own
`claude -p` calls. `make improve` prints this file.

## The instructions

1. `git pull`. Read the open `self-review` issues and the last 4
   `reports/reflect-*.md`.
2. Pick the single highest-value code proposal that isn't done. Prefer small,
   measurable changes.
3. Create branch `improve/YYYY-MM-<slug>`. Implement it with tests. Run the
   full test suite, lint, and `python -m snipehunt.jobs hourly --dry-run` /
   `python -m snipehunt.jobs weekly --dry-run`.
4. Open a PR referencing the issue, with: problem, evidence (metrics), change,
   how to measure success, rollback note. Don't merge. Don't push to main.
5. Off-limits in these PRs: removing or weakening any read-only guard
   (`score.ask_claude` tool limits, the "never bid/buy" prompt rules,
   `gixen.py` being read-only, `_pick_dict`'s price check), anything touching
   secrets/env handling, CI config, or this `SELF_IMPROVE.md` file. If a
   proposal needs one of those, write it up in the PR description as a
   question for Jordan instead.

## Two things that make the loop close

- **Name the proposal in the PR title**, for example
  `Weight recent sales more (SR-2026-10-18-1)`. The next weekly self-review
  looks for that id in the commits on main, marks the proposal done, and
  reports whether the metric it aimed at moved.
- **The reports hold numbers only.** Listing titles, seller names and email
  text are written by strangers and never reach the self-review. Treat the
  issue and the reports as evidence to check, and read the code yourself
  before changing it.

## What keeps the guards in place

`tests/test_guards.py` runs in CI on every pull request and fails if a
read-only guard goes missing: the tool limits on `score.ask_claude`, the HARD
RULES in the scoring prompt, `gixen.py` making no request but the login and
the page read, the price check in `_pick_dict`, the self-review running with
no tools and writing to nothing but GitHub issues, and this file's off-limits
list. A pull request that needs to change one of those tests is, by
definition, a question for Jordan.
