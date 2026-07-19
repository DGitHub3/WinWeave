# AI HANDOFF — WinWeave technical briefing
*For the next assistant working on this project. Written 2026-07-12 by
the previous assistant at the end of a 12-day build. Read fully before
changing anything. The user will upload files from the project when
you ask; you cannot see their machine.*

## 1. What this is
Personal sports-betting analytics (MLB live, NFL season-ready for
Sept 9). Python + SQLite (`data/winweave.db`) + Streamlit dashboard.
Odds from SportsGameOdds API; MLB results from statsapi.mlb.com; NFL
data from nflverse via nflreadpy. The system is in **paper-trading
validation**: no real bets until `calibration_report.py --paper-only
--since 2026-07-12` shows the model beating the market Brier baseline
over 150+ graded picks.

## 2. Architecture (root scripts stay at root — they path to ./data)
- `src/calibration.py` — **the heart. Protect it.** Market-anchored
  blending (w = n/(n+30), max 0.60), edge caps (min of 8 pts absolute,
  25% relative of the smaller market side), longshot weight scaling
  below 20% market prob, AUDIT flag at ≥15 pt raw disagreement,
  empirical-Bayes hit-rate shrinkage (K=20), quarter-Kelly capped at
  2% bankroll.
- `src/ev_engine.py` — math + PropAnalysis dataclass. 9-signal
  weights (usage added 2026-07-12). grade(): X if EV>15% or audit;
  A needs EV≥6, n≥20, uncapped, market prob ≥0.25.
- `src/prop_analyzer.py` (NFL) / `src/mlb_analyzer.py` — analyzers.
  MLB pitcher samples filter `games_started>=1 AND outs_recorded>=7`
  (openers excluded — the "Quantrill bug": relief outings in starter
  samples produced a 94% claim vs market 45%; the bet lost).
- `src/game_analyzer.py` — MLB game markets (ML/RL/totals). Team run
  rates reconstructed from mlb_batting via majority-vote team
  identity; model sample haircut min(n,12) because it's pitcher-blind.
- `src/factors/` — 9 signals. usage.py = target/carry share + snap
  level + red-zone share (schema-adaptive, degrades to neutral).
  prop_tracker.py = the feedback loop (save/resolve/bankroll).
- Root: dashboard.py (Streamlit), scan_live_{mlb_props,nfl_props,
  game_markets}.py, auto_paper_tracker.py (daily paper loop),
  update_mlb_results.py (targeted results fetch — the resolve button
  calls it), calibration_report.py, nfl_data_audit.py.

## 3. INVARIANTS — breaking these silently corrupts money math
1. **Game-market tracker encoding**: game bets store side='over' on a
   margin/total number. moneyline: line=0.0, actual=margin;
   run_line: line=−(handicap); total_runs: normal over/under.
   player_name = team bet on (home team for totals), opponent = other
   club. `GameAnalysis.tracker_encoding()` is the single source of
   truth — never save raw home/away sides to prop_results.
2. **game_date is US-local**, derived via `local_game_date()` (UTC
   start hour < 8 ⇒ previous day). analyzed_at is UTC. Resolution
   matches exact local date.
3. **One-candidate-or-pending** everywhere in auto-resolve. Never
   "first matching game" — series and doubleheaders make that grade
   the wrong game (proven by test).
4. **Paper picks (bet_placed=0) DO auto-resolve** and count toward
   calibration; only bankroll math filters on bet_placed=1.
5. **Bankroll**: current = starting + net deposits (bankroll_
   transactions table) + net profit − pending real stakes. Bonus-bet
   losses cost $0 cash; pending bonus stakes don't reduce balance.
6. **Grades are safety rails**: X-grades are excluded from Top Picks
   and must never be presented as bets. Do not loosen calibration
   constants on <100 samples of evidence.
7. `results` column is TEXT hit/miss/push/void; numeric lives in
   actual_value. Pushes/voids excluded from accuracy math.

## 4. Schema facts that will bite you
- props (NFL, nflverse weekly): **no `interceptions` column** — it's
  `passing_interceptions`. 22 weeks/season = playoffs included
  (Josh Allen 2023 sums 4,695, correct). Duplicates were fixed;
  resolver treats dup rows as ambiguous. `headshot_url` exists.
- snap_counts.offense_pct is a **0–1 fraction**; 2025 coverage only
  (2024 backfill pending).
- mlb_pitching has `games_started` **only after** the 2026-07-11
  builder; the starts filter is schema-adaptive by design — keep it so.
- prop_results: season/week populated for NFL bets (week 0 = MLB).
- live_odds and game_odds are **append-only** (line-history / future
  CLV data — do not dedupe them).

## 5. User environment & working style (matters a lot)
- XeroLinux; **`ls` is aliased to eza** (-t flag differs — avoid
  clever shell one-liners). Browser saves re-downloads as
  `file(1).py`: ALWAYS warn about stale downloads and provide grep
  verification lines (`grep -c "def new_thing" file.py` → expect 1)
  after every install. Pasted multi-line blocks keep executing after
  a failed step — put `git status` checkpoints before commits.
- **Deliver whole files** via file attachments (never diffs), with
  exact `cp` install commands, a `__pycache__` clear, and "what you
  should see" expectations. Compile-check and end-to-end test against
  a synthetic DB before shipping — this workflow caught ~6 real bugs
  pre-delivery. Confirm you ACTUALLY attached files (a past turn
  claimed a file that wasn't attached).
- Repo github.com/DGitHub3/WinWeave is **PUBLIC**. `private/` and
  keys.txt are gitignored; never let ledgers/keys near a commit. SGO
  key was rotated once already; "API KEYS" file in private/ is
  unverified — nudge if relevant.
- The user is engaged and learning; explain the *why* of fixes,
  celebrate genuine wins, and hold the line on betting discipline:
  paper-only until the calibration gate passes, never endorse betting
  X-grades or overriding the model, and promote promo/bonus EV as the
  one guaranteed edge. Their history: real-cash net was ≈ −$3.6 with
  the only profit from a bonus bet; strikeouts is their strongest
  market signal so far.

## 6. Open roadmap (in priority order)
1. Watch calibration bar → at 150 graded, run the report; tune
   MAX_MODEL_WEIGHT per §"Tuning" in src/calibration.py.
2. CLV tracker (append-only odds ledgers already hold the data:
   compare bet odds vs last pre-game fetch; ~50 bets proves an edge).
3. NFL: 2024 snap backfill; injuries whitespace TRIM; usage-factor
   backtest vs 2024-25; August preseason odds pull to verify NFL stat
   map; port game-level markets to NFL (easier: games table stores
   scores directly).
4. Team logos on game-market cards (user will supply PNGs by team).
5. Dashboard cosmetics: signal-breakdown table still shows old
   hardcoded weights; MLB v2 game model (blend probable starter).

## 7. Where evidence lives
`archive/logs/` holds every verification transcript. The tracked-bet
history in prop_results is ground truth for all calibration claims.
When the user reports a bug, ask for terminal output first — every
major fix in this project started from a log they pasted.
