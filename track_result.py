"""
track_result.py — Log actual game outcomes for model calibration.

After a game is played, run this to record what actually happened.
Over time this builds the feedback loop that improves WinWeave's accuracy.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate

    # Show all predictions awaiting results
    python track_result.py

    # Log a specific result
    python track_result.py --id 42 --value 287.0
"""

import sys
import argparse
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.factors.prop_tracker import (
    log_result,
    model_accuracy_report,
    ensure_prop_results_table,
)

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def show_pending():
    """Shows all predictions that don't have results logged yet."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, player_name, stat, line, side, book,
                   american_odds, opponent, season, week,
                   predicted_prob, ev_percent, grade, analyzed_at
            FROM prop_results
            WHERE result IS NULL
            ORDER BY analyzed_at DESC
        """).fetchall()

        if not rows:
            print("\n  No pending predictions. Run the EV scanner first.")
            return

        print(f"\n{'='*70}")
        print(f"  PENDING RESULTS  ({len(rows)} predictions awaiting outcomes)")
        print(f"{'='*70}")
        print(f"  {'ID':<5} {'Player':<26} {'Stat':<18} {'Side':<6} "
              f"{'Line':<8} {'EV%':<8} {'Grade'}")
        print(f"  {'-'*65}")

        for r in rows:
            sign = "+" if r["ev_percent"] and r["ev_percent"] >= 0 else ""
            ev_str = f"{sign}{r['ev_percent']:.1f}%" \
                if r["ev_percent"] is not None else "—"
            print(f"  {r['id']:<5} {r['player_name']:<26} "
                  f"{r['stat']:<18} {r['side']:<6} "
                  f"{r['line']:<8} {ev_str:<8} {r['grade'] or '—'}")

        print(f"\n  To log a result:")
        print(f"  python track_result.py --id <ID> --value <actual_stat_value>")
        print(f"  Example: python track_result.py --id 1 --value 287.0\n")
    finally:
        conn.close()


def show_accuracy():
    """Shows the full model accuracy report."""
    print(model_accuracy_report())


def main():
    parser = argparse.ArgumentParser(
        description="WinWeave result tracker"
    )
    parser.add_argument("--id",     type=int,   help="Prediction row ID to update")
    parser.add_argument("--value",  type=float, help="Actual stat value")
    parser.add_argument("--report", action="store_true",
                        help="Show model accuracy report")
    args = parser.parse_args()

    if args.report:
        show_accuracy()
    elif args.id and args.value is not None:
        log_result(args.id, args.value)
        # Show updated accuracy after logging
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute(
            "SELECT COUNT(*) FROM prop_results WHERE result IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        print(f"  Total results logged: {total}")
        if total >= 5:
            print(model_accuracy_report())
    else:
        show_pending()


if __name__ == "__main__":
    main()
