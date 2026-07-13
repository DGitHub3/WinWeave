# WinWeave Changelog — July 11–12, 2026
*The final 48 hours of the 12-day v3 build. Roughly half the total
project happened in this window. Preserved from the build
conversation on 2026-07-13.*

---

## July 11

1. **Verification log analyzed** (2,477 lines) — v3 install confirmed:
   `games_started` column populated, X-audit flags firing on the
   right picks, grade distribution finally discriminating.
2. **Longshot hole discovered** — v3's fixed 8-point edge cap let
   +610 stolen-base props grade "A" at +41% EV.
3. **v3.1 calibration shipped** — relative edge cap (25% of the
   smaller market side), longshot model-weight scaling, +15%-EV
   audit gate, and no A-grades on sub-25% markets.
4. **`--since` flag** added to calibration_report.py — separates the
   old model's tracked history from calibrated-era predictions.
5. **NFL stat map patched** with the real SGO oddIDs found by the
   offseason diagnostic pull (`passing_touchdowns`, etc.).
6. **First real auto-resolve committed** — five bets graded,
   including both Quantrill losses the old model recommended.
7. **Game-level markets built end-to-end** — moneylines, run lines,
   and totals extracted from the same API payload (zero extra cost),
   `game_odds` append-only ledger, `game_analyzer.py` with team
   run-rate reconstruction from batting logs, scan CLI, and the
   margin-based tracker encoding that reuses prop_results unchanged.
8. **Series-resolution bug caught by test** — resolver graded the
   wrong game of a series; fixed with exact US-local game dates
   (UTC hour < 8 rule) plus doubleheader guards.
9. **Dashboard integration** — combined MLB feed, the Market Focus
   filter, X-grades excluded from Top Picks, and a pitcher-blind
   model haircut after the live test showed a systematic
   underdog tilt.
10. **Environment lessons** — ImportError traced to browser
    `file(1).py` duplicate downloads; discovered `ls` is aliased to
    eza on XeroLinux.
11. **Real ledger backfilled** — all 39 sportsbook bets reconciled
    into the tracker (`backfill_bets_20260711.py`): net −$3.62 cash,
    strikeouts 7-4, the six-times-PCA conviction pattern exposed.
12. **Accuracy screenshots read properly** — the model's 60–80%
    buckets were already calibrated; the damage came from the
    extremes (the 80%+ bucket went 0-for-2) and human overrides
    (the <50% bucket hit 12%).
13. **Paper-trading system built** on the user's decision to stop
    wagering — `auto_paper_tracker.py` daily loop, auto-resolve
    extended to $0 picks, real/paper scoring flags.

## July 12

14. **NFL pivot** — verdict: nflreadpy needs no replacement;
    `nfl_data_audit.py` built (structural-vs-real NULL analysis);
    NFL auto-resolve branch added (props-table lookup with
    duplicate-row guard); week numbers wired engine → dashboard →
    tracker; `NFL_SEASON_PREP.md` written.
15. **Three latent factor bugs fixed** — stale injuries (a
    prior-season "Out" zeroed players forever), cross-season
    snap-trend pollution, and a pace factor that had been a constant
    0.93 for the whole league due to a baseline basis mismatch.
16. **Usage factor built** (`usage.py`) — target/carry share, snap
    level, red-zone share combined into a star-reliability score;
    wired as the ninth signal with rebalanced weights; proven on a
    star-vs-scrub test (0.91 vs 0.28).
17. **NFL audit run clean** — zero duplicates; Josh Allen's 4,695
    yards explained (playoffs included: data perfect);
    `passing_interceptions` column discovered and aligned across
    engine, scraper, and audit.
18. **Public GitHub push prepared** — hardened .gitignore, honest
    README, new About text; key, ledger, and balances structurally
    blocked from the repo.
19. **Project reorganized** — `private/` for sensitive files
    (unpushable by design), `archive/` for the R era, logs, and
    notes; byte-verified dedupe of the duplicated R scripts.
20. **Dashboard feature batch** — deposit/withdrawal ledger with
    corrected balance math (starting + deposits + profit − pending),
    real/paper accuracy split, the 150-pick calibration countdown
    bar, ✓ tracked checkmarks, one-click bulk paper-track, and
    player headshots sourced entirely from data already owned
    (nflverse headshot_url + MLB static CDN).
21. **Stuck-pending mystery solved** — `update_mlb_results.py`
    fetches game logs for only the players with pending bets and
    grades game bets from official schedule scores; wired into the
    renamed "Fetch results & auto-resolve" button, which now
    explains every skip.
22. **Handoff documents written** — the plain-language Owner's
    Manual and the technical AI Handoff briefing.
23. **Travel automation kit** — three-option guide (with work-laptop
    and Slack cautions), hardened `daily_loop.sh` (dual daily runs,
    overlap lock, logged diary), and `preflight_check.sh` — which
    caught the Arch-doesn't-ship-cron trap before departure.
24. **First autonomous act** — during the live 17:50 cron test,
    WinWeave resolved, scanned, and paper-tracked a B-grade pick
    with no human input. The machine has the watch.
