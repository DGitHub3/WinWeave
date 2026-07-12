# WinWeave

**A calibrated sports-betting analytics engine for MLB and NFL player
props and game markets — built to tell the truth about its own edge.**

WinWeave pulls live odds, analyzes every prop through a multi-signal
probability model, anchors each prediction against the no-vig market
price, and then — the part most betting tools skip — tracks every
prediction to resolution and grades itself. If the model isn't beating
the market, WinWeave is designed to say so.

## What it does

- **Live odds ingestion** — player props and game-level markets
  (moneyline, run line / spread, totals) across DraftKings, FanDuel,
  BetMGM, and Caesars, with an append-only odds ledger for
  line-movement history.
- **Multi-signal probability engine** — recency-weighted hit rates,
  distribution models (Normal/Poisson), opponent-defense adjustment
  computed on a consistent per-game basis, roster health (injuries,
  snap trends, depth charts), coaching tendencies and pace from
  play-by-play, weather, referee crews, and a usage/star-reliability
  factor (target share, snap level, red-zone share).
- **Market-anchored calibration** — every raw model probability is
  blended toward the no-vig market number in proportion to sample
  size, edge-capped (absolute *and* relative), and longshot-guarded.
  Claimed edges the market strongly disagrees with are routed to an
  **audit grade** instead of a bet recommendation.
- **Honest grading** — A-grades are rare by design. Implausible EV is
  a red flag, not a jackpot.
- **Self-scoring feedback loop** — every prediction (paper or real)
  is stored, auto-resolved from official stat feeds after games, and
  scored: Brier score vs. the market baseline, calibration curves,
  winner's-curse analysis, and ROI by market type.
- **Paper-trading mode** — a daily one-command loop that logs the
  model's picks with zero money at risk, growing the validation
  sample ~10x faster than betting could.
- **Streamlit dashboard** — top picks with market-focus filtering,
  EV scanner, bankroll/tracker views, and one-click result resolution.

## Data sources

- **MLB** — official MLB Stats API (free, public)
- **NFL** — [nflverse](https://nflverse.nflverse.com/) via `nflreadpy`
  (play-by-play, weekly stats, snap counts, injuries, schedules)
- **Odds** — SportsGameOdds API (bring your own key in `keys.txt`,
  which is gitignored)

## Quick start

```bash
git clone https://github.com/DGitHub3/WinWeave.git
cd WinWeave
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# build local databases (SQLite, ~10-30 min each)
python scrapers/build_mlb_db.py
python refresh_season_data.py            # NFL via nflverse

# add your SportsGameOdds key
echo "SGO_API_KEY=your_key_here" > keys.txt

# daily loop
python scrapers/sgo_scraper.py --league mlb
python auto_paper_tracker.py             # resolve + paper-track
streamlit run dashboard.py               # the dashboard
python calibration_report.py             # the report card
```

## Design philosophy

Sportsbook lines already contain most public information. A realistic
solo model doesn't "beat the books" broadly — it finds small,
verifiable edges in niche markets, and knows when **not** to bet. Every
architectural choice here follows from that: market anchoring over
model confidence, paper validation before capital, audit flags over
excitement, and a report card that compares the model against the
hardest baseline there is — the closing market price itself.

## Status

- **MLB** — fully live: props + game markets, calibrated, paper
  tracker running daily.
- **NFL** — data layer audited and season-ready; live-odds pipeline
  verified against offseason markets; first full-scale test planned
  for the 2026 preseason.

## Disclaimer

WinWeave is a personal research and analytics project, provided as-is
for educational purposes. It is not financial advice, and no output of
this software is a guarantee of profit. Sports betting involves real
financial risk — if you bet, bet responsibly and within your means.
Must be 21+ where applicable. If gambling stops being fun, call or
text 1-800-GAMBLER.
