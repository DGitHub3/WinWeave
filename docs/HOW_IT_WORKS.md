# How WinWeave Works — A Complete Breakdown

This is the reference document for understanding every moving part of
WinWeave: what each script does, how the math works, how the signals
feed into a prediction, and a shared vocabulary so future
conversations can point at things precisely instead of "that thing
that does the bet stuff."

---

## 1. The Big Picture — One Pipeline, Four Stages

Everything WinWeave does is one pipeline with four stages. Every
script belongs to exactly one of these:

```
  STAGE 1              STAGE 2              STAGE 3              STAGE 4
  SCRAPE                SCAN                 ANALYZE              TRACK
  ──────               ──────               ────────             ──────
  Pull raw odds    →   Match odds to    →   Compute a real   →   Log what you
  from the odds        your real            probability &        bet, check
  provider (SGO)       historical data      EV% for a prop        results,
                                                                    report accuracy
                                                                    & bankroll

  scrapers/             scan_live_             src/mlb_analyzer.py    src/factors/
  sgo_scraper.py         mlb_props.py           src/prop_analyzer.py   prop_tracker.py
                         scan_live_             src/ev_engine.py
                         nfl_props.py
```

`dashboard.py` is the one file that touches all four stages — it's
the steering wheel, not a stage of its own. Everything below is
organized by which stage a script belongs to.

A prop bet moves through these stages like this: raw odds land in
the `live_odds` table (Stage 1) → get matched to a real player and
paired with their opposite side (Stage 2) → get run through the math
to produce a probability and an EV% (Stage 3) → optionally get
logged so we can check later whether the model was right (Stage 4).

---

## 2. Vocabulary — Use These Words From Now On

This is the part you specifically asked for. Precise, consistent
terms, so "the thing that does X" always means the same thing:

| Term | Means |
|---|---|
| **Pull** | Running the scraper (`sgo_scraper.py`, or the dashboard's 🔄 button) to fetch fresh odds. |
| **Scan** | Running `scan_live_mlb_props.py` / `scan_live_nfl_props.py` (or opening Top Picks) — turns raw pulled odds into a ranked list of picks. |
| **Pick** | One fully-analyzed prop: a player, a stat, a line, a recommended side, a probability, an EV%, and a grade. This is what shows up as a card in Top Picks. |
| **Signal** | One individual sub-estimate that feeds into a pick's final probability — e.g. "the Hit Rate signal," "the Park signal." A pick is a blend of several signals. |
| **True probability** | The model's own final estimate of how likely the pick is to hit, after blending every signal. |
| **No-vig probability** (a.k.a. **fair price**) | What the sportsbook's odds imply once you strip out their built-in profit margin (the "vig"). This is the market's honest opinion. |
| **Edge** | True probability minus no-vig probability. Positive edge = we think it's more likely than the market does. |
| **EV%** | Expected Value — the mathematically expected profit or loss per dollar wagered, using true probability against the *actual* odds offered (vig included, since that's the real payout you'd get). This is *the* number that determines whether a bet is worth making. |
| **Grade** | The letter (A/B/C/D/F) that summarizes a pick's strength — mostly driven by EV%, with sample-size and hit-rate sanity checks. |
| **Kelly fraction** | The mathematically "optimal" percent of bankroll to wager on a pick, given its edge and odds. Shown as a *suggestion*, not something the dashboard enforces. |
| **Track** *(verb)* | Logging a pick into the Tracker — with or without real money. |
| **Real bet** | A tracked pick with `bet_placed = True` and a real stake — actual money on the line. |
| **Bonus bet** | A real bet placed using promotional/free-bet credit instead of withdrawable cash. Losing one costs $0 real money; winning one pays profit only (same as any bet). |
| **Pending** | A tracked pick whose real-world outcome hasn't been logged yet. |
| **Resolved** | A tracked pick whose actual result (hit/miss/push) has been logged. |
| **Bankroll** | The per-sportsbook real-money tracking: starting balance + resolved net profit − real pending stakes (bonus-bet stakes don't subtract, since that money was never real cash). |
| **The Tracker** | The whole feedback-loop system — both the Tracker tab in the dashboard and `prop_tracker.py` underneath it. |

---

## 3. Stage 1 — Scrape: `scrapers/sgo_scraper.py`

**Job:** talk to the SportsGameOdds (SGO) API and turn its response
into rows in the `live_odds` table.

**Key functions:**
- `fetch_events(api_key, league)` — asks SGO for every upcoming game
  and its player props.
- `parse_player_props(events, league, books)` — walks through SGO's
  raw JSON and extracts one row per (player, stat, side, book, line).
  This is where SGO's cryptic IDs (`"RAFAEL_DEVERS_1_MLB"`,
  `"LOS_ANGELES_DODGERS_MLB"`) get pulled apart — though turning
  those into *real* names/teams is actually Stage 2's job; this
  function just extracts them as-is.
- `save_to_db(rows)` — writes everything into `live_odds`, tagged
  with a `fetched_at` timestamp so old pulls stay as history rather
  than getting overwritten.
- `run_scrape(league, books, progress_callback)` — the version of
  all of the above that the dashboard's 🔄 button calls directly,
  with live status updates instead of only terminal prints.

**What it does *not* do:** any matching, math, or filtering. This
stage's only job is "get the raw data down safely." Everything
downstream trusts that `live_odds` reflects exactly what SGO said,
unfiltered.

---

## 4. Stage 2 — Scan: `scan_live_mlb_props.py` / `scan_live_nfl_props.py`

**Job:** bridge the gap between SGO's raw, cryptic odds rows and
your real historical database, then hand each fully-identified prop
to the analyzer.

This is the most "plumbing-heavy" part of the system — three real
problems it solves:

1. **Name matching.** SGO's player ID (`"ANDRES_GIMENEZ_1_MLB"`)
   doesn't match your database's name (`"Andrés Giménez"`) exactly.
   `guess_player_name()` converts the ID into a readable guess, then
   `normalize_for_matching()` strips accents/punctuation from both
   sides so cosmetic differences don't cause a miss. Genuine
   nickname-vs-legal-name mismatches (Jazz Chisholm Jr.'s SGO ID uses
   his legal first name) can't be bridged this way and get reported
   as unmatched instead of guessed at.

2. **Team resolution.** SGO tells you the two teams playing, not
   which team a specific player is on. `resolve_team()` parses SGO's
   team-ID format into your database's spelling; `get_player_team()`
   and a fallback `infer_side_from_history()` (checks which team this
   player has faced before, in your own logged game history) figure
   out which side of the matchup the player belongs to.

3. **Pairing over and under.** A prop needs *both* sides' odds to
   compute a fair (no-vig) price. `pair_over_under()` groups raw rows
   by event + player + stat + book + line so both sides end up
   together — scoped to the *specific event*, because a real data
   pull once showed the same real-world game listed as two different
   "events," and pairing across them would have silently mixed up
   unrelated prices.

**The core loop, and the two-sided fix:** for every fully-identified
prop, this stage calls the analyzer **twice** — once for `side="over"`,
once for `side="under"` — and keeps whichever result has the higher
EV%. Earlier versions hardcoded `side="over"`, which meant "under"
was never even evaluated, not correctly ruled out. That's fixed now
(2026-07-09) — see the code comments in this file for the full story.

**Output:** a list of fully-analyzed picks (see Stage 3), each
tagged with a human-readable matchup string and a sportsbook deep
link when one's available.

---

## 5. Stage 3 — Analyze: the actual math

This is the heart of the system, split across three files.

### 5a. `src/ev_engine.py` — the shared math core

Used identically by both sports. Nothing sport-specific lives here.

- **`hit_rate(values, line, side)`** — the simplest possible signal:
  what fraction of the player's past games cleared this line? Just
  counting.
- **`weighted_hit_rate(values, line, side)`** — the same idea, but
  more recent games count more than older ones. (This function had a
  real, since-fixed bug: it was weighting the *oldest* games most
  heavily instead of the most recent ones — see the code comment in
  that function.)
- **`calculate_probability(mean, std_dev, line, stat, side)`** — the
  "statistical model" signal. Uses one of two distributions depending
  on the stat:
  - **Poisson**, for low-count discrete stats (home runs, hits, RBI,
    strikeouts by a batter, stolen bases). Poisson is the standard
    model for "how many times does a rare, countable thing happen in
    a fixed opportunity" — it naturally produces the right shape:
    skewed, non-negative, integer counts.
  - **Normal (bell curve)**, for stats that behave more like a
    continuous range (total bases, passing yards). Uses the player's
    own mean and standard deviation from recent games.
- **`combine_all_signals(...)`** — blends every individual signal
  into one final true probability, using a weighted average. If a
  signal can't produce a trustworthy estimate for this specific prop
  (not enough data), its weight gets redistributed proportionally
  across the *other* signals rather than being faked. (One exception:
  NFL's Prop Tracker signal always returns a real number — a neutral
  0.5 when there's no data — rather than being skippable, because
  this function multiplies it directly with no None-check.)
- **`american_to_implied_prob`, `remove_vig`, `calculate_ev`,
  `kelly_criterion`** — the betting-math formulas: converting
  American odds (+150, -110) into probabilities, stripping the vig
  by using both sides of the market together, computing expected
  value, and computing the Kelly-optimal bet size.

### 5b. `src/mlb_analyzer.py` — MLB's 6 signals

| # | Signal | Weight | What it measures |
|---|---|---|---|
| 1 | Hit rate | 22% | Blend of simple + recency-weighted historical hit rate |
| 2 | Statistical model | 25% | Poisson/Normal probability from the player's mean & std dev |
| 3 | Opponent | 13% | How much of this stat the opposing team allows per game vs. league average |
| 4 | Home/away | 10% | Player's split performance at home vs. on the road |
| 5 | Ballpark factor | 15% | Computed from the *player's own team's* home-vs-road production — no external park-factor dataset needed |
| 6 | Batter-vs-starter | 15% | This batter's specific history against tonight's *probable* starting pitcher — only activates with enough head-to-head games |

Signals 5 and 6 are the two most likely to get skipped (insufficient
data), in which case their weight moves to the other four.

### 5c. `src/prop_analyzer.py` — NFL's 8 signals

| # | Signal | Weight | What it measures |
|---|---|---|---|
| 1 | Hit rate | 18% | Same idea as MLB's |
| 2 | Statistical model | 18% | Same idea as MLB's |
| 3 | Defense-adjusted | 15% | Opponent's defensive strength against this stat, position-adjusted |
| 4 | Prop tracker | 14% | How this player has done in *our own* previously tracked predictions for this stat/line range (not raw game stats — our own prediction history). Reconstructed 2026-07-09; see code comments. |
| 5 | Roster health | 12% | Injury-status multiplier (`src/factors/roster.py`) |
| 6 | Coaching/pace | 10% | Team's play-calling tendency + pace of play (`src/factors/coaching.py`) |
| 7 | Weather | 8% | Weather-condition multiplier for outdoor games (`src/factors/weather.py`) |
| 8 | Officials | 5% | Officiating crew tendency multiplier (`src/factors/officials.py`) |

Signals 5-8 are *multipliers* applied to the base probability rather
than independent probability estimates blended in directly — they
nudge the number up or down rather than voting on it directly.

**NFL has not yet been run against real live odds** — SportsGameOdds
doesn't post NFL props until roughly 1-2 weeks before games (August
preseason onward). The architecture is proven (it mirrors MLB's,
which has processed thousands of real props), but the NFL-specific
signal modules haven't been stress-tested against a real pull yet.

---

## 6. Stage 4 — Track: `src/factors/prop_tracker.py`

**Job:** remember what the model predicted, find out what actually
happened, and report back — both on model accuracy and on real
bankroll.

**Key functions:**
- `save_prediction(...)` — logs a pick. If `bet_placed=True`, this is
  a real wager and needs a `stake`; otherwise it's tracked purely to
  check the model's accuracy. Automatically skips creating a
  duplicate if the exact same real bet was already logged today
  (prevents accidentally double-counting from re-running a script).
- `log_result(row_id, actual_value)` — after the game, records what
  really happened, determines hit/miss/push, and (for real bets)
  computes payout automatically from the odds and stake.
- `edit_prediction(row_id, ...)` — fixes a mistake after the fact
  (wrong player, wrong stake, wrong line) and recomputes
  result/payout if needed so nothing goes stale.
- `get_bankroll_by_book()` — the real bankroll math per sportsbook:
  starting balance + resolved net profit − pending *real* stakes
  (bonus-bet stakes, pending or lost, never subtract from real
  balance).
- `get_player_track_record()`, `get_all_player_track_records()` — how
  often a specific player's tracked picks have actually hit (needs
  3+ logged results before showing anything — one or two results
  isn't a track record yet).
- `model_accuracy_report()` — the overall calibration check: is a
  pick the model rates "70% likely" actually hitting around 70% of
  the time?

---

## 7. `dashboard.py` — the steering wheel

Doesn't compute anything itself — it calls into every stage above
and renders the results. Structure:

- **🔄 Pull Fresh Odds** (top of page) — calls Stage 1 directly.
- **🏈 NFL / ⚾ MLB tabs**, each with:
  - **Top Picks** — calls Stage 2's `scan_mlb_props()` /
    `scan_nfl_props()`, renders the ranked picks as cards.
  - **Player Explorer** — raw historical stats browser, no analysis.
  - **EV Scanner** — manual single-prop entry, calls Stage 3 directly
    for one player/stat/line you type in.
- **📋 Tracker** — the UI for Stage 4: Bankroll, pending predictions,
  logging results, the "Fix a mistake" editor, and accuracy reports.
- **💰 Live Odds** — a raw browser over the `live_odds` table itself
  (Stage 1's output), for when you want to see exactly what was
  pulled without any analysis layered on top.

---

## 8. Supporting scripts (outside the main pipeline)

| Script | Stage it supports | Purpose |
|---|---|---|
| `explain_mlb_prop.py` | Diagnostic | Full signal-by-signal trace for one player/stat, plus raw `live_odds` history — the tool for "why did this pick look wrong?" |
| `build_mlb_db.py` | Feeds Stage 3 | Builds/refreshes MLB's historical tables from the MLB Stats API |
| `refresh_season_data.py` | Feeds Stage 3 | Refreshes NFL's historical tables via `nflreadpy` |
| `validate_data.py` | QA | Data-integrity checks across the whole database |
| `run_mlb_scan.py` / `run_ev_scan.py` | Predates the dashboard | Interactive CLI demo/manual-entry tools — mostly superseded by the dashboard's Top Picks and EV Scanner tabs now |
| `track_result.py` | Stage 4, CLI version | Same job as the Tracker tab's result-logging, from the terminal |

---

## 9. Putting it together — one prop's full journey

1. **Pull**: `sgo_scraper.py` asks SGO for tonight's games, gets back
   raw JSON, saves rows like `("RAFAEL_DEVERS_1_MLB", "hits", "over",
   "fanduel", 0.5, -140)` into `live_odds`.
2. **Scan**: `scan_live_mlb_props.py` reads that row, figures out
   it's really "Rafael Devers" on "Boston Red Sox," finds the
   matching "under" row from the same event, and hands both to the
   analyzer — twice, once per side.
3. **Analyze**: `mlb_analyzer.py` pulls Devers' last 25 games,
   computes his hit rate and mean/std dev for hits, checks how the
   opponent's pitching staff does against hitters, checks his
   home/away split, checks Fenway's park factor, checks his history
   against tonight's starter — blends all six into one true
   probability, compares it to the no-vig price from the -140/+120
   odds, and produces an EV%.
4. **Rank**: whichever side (over or under) had the better EV% is
   the pick. If it clears the dashboard's hit-rate/EV filters, it
   shows up as a card in Top Picks.
5. **Track** *(optional)*: you click the pick, mark it as a real $3
   bet, and it's logged as pending.
6. **Resolve**: after the game, you (or the dashboard) log that
   Devers actually got 2 hits — the Tracker marks it a hit,
   computes the $3 bet's payout from the +120 odds, and updates
   your FanDuel bankroll.

That's the whole system, end to end.
