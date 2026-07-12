"""
auto_paper_tracker.py — WinWeave's automatic paper-trading loop.

One command, run once a day, no money involved:

  1. AUTO-RESOLVE  — grades yesterday's pending picks (paper AND
     real) from the freshly rebuilt MLB tables.
  2. SCAN          — analyzes today's player props + game markets
     from the latest odds pull.
  3. PAPER-TRACK   — logs every qualifying pick to prop_results with
     bet_placed=0 and no stake. These are the model's official
     predictions of record: graded automatically tomorrow, scored by
     calibration_report.py, costing $0.

This is how the calibration sample grows 10x faster than betting
ever could: ~20-60 paper picks per day instead of 3-5 wagers, with
zero bankroll risk while the model earns (or fails to earn) trust.

DAILY ROUTINE (5 minutes):
    python scrapers/build_mlb_db.py            # refresh game results
    python scrapers/sgo_scraper.py --league mlb # refresh odds
    python auto_paper_tracker.py               # resolve + log picks

WHAT QUALIFIES (defaults, see flags):
    - positive EV, grade A/B/C (X-audit and F excluded)
    - deduped to one row per prop (best-priced book)
    - skipped if an identical pick is already pending (re-running is
      safe; it will not double-log)

FLAGS:
    --min-ev 2.0        only log picks at or above this EV%%
    --include-all       log EVERY analyzed side, including F grades.
                        More data for the calibration curve, at the
                        cost of a much bigger tracker. Good once the
                        routine is established.
    --dry-run           show what would be logged without writing
    --no-resolve        skip the auto-resolve step

REVIEWING THE RECORD:
    python calibration_report.py --paper-only --since 2026-07-12
    python calibration_report.py --real-only
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.factors.prop_tracker import (
    auto_resolve_pending_bets, save_prediction, ensure_prop_results_table,
)
from scan_live_mlb_props import scan_mlb_props
from scan_live_game_markets import scan_game_markets


def qualifying(results, min_ev: float, include_all: bool):
    """Dedupe to best book per prop; filter by grade policy."""
    best: dict = {}
    for r in results:
        key = (r.player_name, r.stat, r.line, r.side)
        cur = best.get(key)
        if cur is None or r.ev_percent > cur.ev_percent:
            best[key] = r
    picks = list(best.values())
    if include_all:
        return picks
    out = []
    for r in picks:
        g = r.grade()
        if g.startswith(("X", "F")):
            continue
        if r.ev_percent < min_ev:
            continue
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ev", type=float, default=2.0)
    ap.add_argument("--include-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-resolve", action="store_true")
    args = ap.parse_args()

    ensure_prop_results_table()
    print(f"\n{'='*62}\n  WINWEAVE PAPER TRACKER — "
          f"{datetime.now(timezone.utc).isoformat()[:16]} UTC\n{'='*62}")

    # 1) Grade whatever finished
    if not args.no_resolve:
        res = auto_resolve_pending_bets(dry_run=args.dry_run)
        n_amb = len(res["skipped_ambiguous"])
        print(f"  Auto-resolve: {len(res['resolved'])} graded, "
              f"{len(res['skipped_no_data'])} awaiting data"
              f"{f', {n_amb} ambiguous (resolve manually)' if n_amb else ''}")

    # 2) Scan everything
    prop_results, prop_diag = scan_mlb_props()
    game_results, game_diag = scan_game_markets()
    err = prop_diag.get("error") if prop_diag.get("error") \
        and game_diag.get("error") else None
    if err:
        print(f"  {err}")
        return
    results = list(prop_results) + list(game_results)
    print(f"  Scanned: {len(prop_results)} prop sides + "
          f"{len(game_results)} game-market sides")

    # 3) Log qualifying picks as paper predictions
    picks = qualifying(results, args.min_ev, args.include_all)
    logged = skipped_dupe = 0
    for r in picks:
        if hasattr(r, "tracker_encoding"):   # game markets
            enc = r.tracker_encoding()
        else:
            from scan_live_mlb_props import local_date
            enc = {"player_name": r.player_name, "stat": r.stat,
                   "line": r.line, "side": r.side,
                   "opponent": getattr(r, "opponent", "") or "",
                   "game_date": local_date(
                       getattr(r, "starts_at", None) or "") or None}
        rid = save_prediction(
            **enc, book=r.book, american_odds=r.american_odds,
            season=datetime.now().year, week=0,
            predicted_prob=r.true_probability,
            ev_percent=r.ev_percent,
            kelly_fraction=getattr(r, "kelly_fraction", 0.0),
            grade=r.grade(), bet_placed=False, stake=None,
        ) if not args.dry_run else None
        # save_prediction dedupes pending identicals itself (returns
        # the existing id); count separately for the summary
        if args.dry_run:
            logged += 1
        elif rid:
            logged += 1

    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"  Paper-tracked: {logged} qualifying picks{mode} "
          f"(grades A/B/C, EV >= {args.min_ev:.1f}%"
          f"{', ALL sides' if args.include_all else ''})")
    if picks and not args.dry_run:
        by_grade: dict = {}
        for r in picks:
            by_grade[r.grade()[0]] = by_grade.get(r.grade()[0], 0) + 1
        print(f"  Grade mix: " + ", ".join(
            f"{k}: {v}" for k, v in sorted(by_grade.items())))
    print(f"{'='*62}")
    print("  Tomorrow: rebuild MLB tables, rerun this, and the picks")
    print("  above become graded calibration data — at $0 risked.")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
