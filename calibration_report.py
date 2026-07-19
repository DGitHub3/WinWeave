"""
calibration_report.py — Is WinWeave actually predictive? Prove it.

Reads the prop_results table (the feedback loop you already built)
and answers the only question that matters: when the model says X%,
does the thing happen X% of the time?

Runs entirely on data you already collect. Safe: read-only.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python calibration_report.py            # full report
    python calibration_report.py --min 1    # include even tiny samples

WHAT IT PRINTS:
  1. Brier score + log loss vs. two dumb baselines:
       - "always 50%"  (coin-flip baseline)
       - "just use the book's no-vig number" (market baseline)
     If WinWeave doesn't beat the MARKET baseline, the model is
     adding noise, not signal — anchor harder (lower MAX_MODEL_WEIGHT
     in src/calibration.py).
  2. Calibration table: predictions bucketed by claimed probability
     vs. how often they actually hit. The 90%+ bucket is where the
     Quantrill-class errors will show up as a giant miss.
  3. Model-vs-market gap analysis: win rate of picks grouped by how
     far the model strayed from the market. If the far-from-market
     group wins LESS, that is winner's curse, measured on your own
     money.
  4. ROI by grade and by stat type — which markets (rbi? strikeouts?)
     the model is genuinely good at, so you can restrict scanning.
"""

import argparse
import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def american_to_implied(odds: int) -> float:
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def profit_per_unit(odds: int) -> float:
    """Net profit on a 1-unit stake if the bet wins."""
    return (100 / -odds) if odds < 0 else (odds / 100)


def load_rows(conn, since=None):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prop_results)")}
    needed = {"predicted_prob", "result", "line", "side", "american_odds"}
    missing = needed - cols
    if missing:
        raise SystemExit(f"prop_results is missing columns: {missing}")

    has_stake = "stake" in cols and "bet_placed" in cols
    # SCHEMA NOTE (corrected after reading the real prop_tracker.py):
    # `result` is TEXT — 'hit' / 'miss' / 'push' / 'void' — and the
    # numeric outcome lives in `actual_value`. We use the tracker's
    # own verdict directly rather than re-deriving it, and exclude
    # pushes (ties) and voids (cancelled bets) from accuracy math.
    rows = conn.execute("""
        SELECT id, player_name, stat, side, line, american_odds,
               predicted_prob, ev_percent, grade, result
               {extra}
        FROM prop_results
        WHERE result IN ('hit', 'miss')
          AND predicted_prob IS NOT NULL
          AND american_odds IS NOT NULL
          {since}
    """.format(extra=", bet_placed, stake" if has_stake else "",
               since=f"AND analyzed_at >= '{since}'" if since else "")
    ).fetchall()
    return rows, has_stake


def hit(row) -> int:
    """1 if the tracker graded this prediction a hit, else 0."""
    return 1 if row["result"] == "hit" else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=1,
                    help="min graded results required per bucket to print")
    ap.add_argument("--real-only", action="store_true",
                    help="score only real-money bets (bet_placed=1)")
    ap.add_argument("--paper-only", action="store_true",
                    help="score only paper-tracked picks (bet_placed=0)")
    ap.add_argument("--since", type=str, default=None,
                    help="only include predictions analyzed on/after this "
                         "date (YYYY-MM-DD). Use your v3 install date to "
                         "score the calibrated model separately from the "
                         "old one's tracked history.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows, has_stake = load_rows(conn, args.since)
    conn.close()

    if has_stake and args.real_only:
        rows = [r for r in rows if r["bet_placed"]]
    elif has_stake and args.paper_only:
        rows = [r for r in rows if not r["bet_placed"]]

    # MIXED-ERA GUARD: X-graded or parlay rows can only come from the
    # pre-calibration betting era. If they're in the sample and no
    # era filter was given, this report is scoring ancient history
    # blended with the current model — loudly say so.
    if not (args.paper_only or args.real_only or args.since):
        old_era = [r for r in rows
                   if (r["grade"] or "").startswith("X")
                   or r["stat"] == "parlay"]
        if old_era:
            print("\n  " + "!" * 62)
            print("  !!  WARNING: this sample MIXES the pre-calibration")
            print(f"  !!  betting era ({len(old_era)}+ old rows detected) with")
            print("  !!  current model predictions. The scores below do NOT")
            print("  !!  measure the current model. For the real report card:")
            print("  !!      python calibration_report.py --paper-only --since 2026-07-12")
            print("  " + "!" * 62)

    graded = []
    for r in rows:
        p = float(r["predicted_prob"])
        p = p / 100.0 if p > 1.0 else p       # tolerate pct-stored values
        graded.append((r, p, hit(r)))

    n = len(graded)
    print(f"\n{'='*66}\n  WINWEAVE CALIBRATION REPORT — {n} graded predictions"
          f"\n{'='*66}")
    if n == 0:
        print("  No graded results yet. Log outcomes with track_result.py "
              "first.\n  Everything else in this report needs real outcomes "
              "to mean anything.")
        return
    if n < 30:
        print(f"  ⚠ Only {n} results — treat everything below as noisy "
              "until ~100+.")

    # ── 1. Brier / log loss vs baselines ───────────────────────────
    def brier(pairs):
        return sum((p - h) ** 2 for p, h in pairs) / len(pairs)

    def logloss(pairs):
        eps = 1e-9
        return -sum(h * math.log(max(p, eps)) +
                    (1 - h) * math.log(max(1 - p, eps))
                    for p, h in pairs) / len(pairs)

    model_pairs = [(p, h) for _, p, h in graded]
    market_pairs = [(american_to_implied(r["american_odds"]), h)
                    for r, _, h in graded]   # implied (with vig) — a
                                             # slightly-too-easy baseline;
                                             # beating it is table stakes
    coin_pairs = [(0.5, h) for _, _, h in graded]

    print(f"\n  SCORES (lower = better)")
    print(f"  {'':22}{'Brier':>10}{'Log loss':>12}")
    print(f"  {'WinWeave model':22}{brier(model_pairs):>10.4f}"
          f"{logloss(model_pairs):>12.4f}")
    print(f"  {'Market (implied)':22}{brier(market_pairs):>10.4f}"
          f"{logloss(market_pairs):>12.4f}")
    print(f"  {'Coin flip (50%)':22}{brier(coin_pairs):>10.4f}"
          f"{logloss(coin_pairs):>12.4f}")
    if brier(model_pairs) > brier(market_pairs):
        print("  → Model is LOSING to the raw market number. It is "
              "currently\n    subtracting information. Lower "
              "MAX_MODEL_WEIGHT / raise MARKET_ANCHOR_K.")
    else:
        print("  → Model beats the raw market baseline. Signal exists.")

    # ── 2. Calibration buckets ──────────────────────────────────────
    print(f"\n  CALIBRATION — claimed probability vs. reality")
    print(f"  {'Claimed':>10} {'N':>5} {'Actual hit rate':>16}   verdict")
    buckets = [(0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, .8),
               (.8, .9), (.9, 1.01)]
    for lo, hi in buckets:
        b = [(p, h) for p, h in model_pairs if lo <= p < hi]
        if len(b) < args.min:
            continue
        act = sum(h for _, h in b) / len(b)
        mid = (lo + min(hi, 1.0)) / 2
        gap = act - mid
        verdict = ("✓ ok" if abs(gap) < 0.10 else
                   "⚠ OVERCONFIDENT" if gap < 0 else "⚠ underconfident")
        print(f"  {lo:>4.0%}–{min(hi,1.0):<4.0%} {len(b):>5} "
              f"{act:>15.0%}   {verdict} ({gap:+.0%})")

    # ── 3. Winner's-curse check: distance from market vs outcome ───
    print(f"\n  MODEL-VS-MARKET GAP — do far-from-market picks really win?")
    print(f"  {'|model − market|':>18} {'N':>5} {'Hit rate':>10} "
          f"{'Avg claimed':>12}")
    gap_buckets = [(0, .05), (.05, .10), (.10, .20), (.20, 1.0)]
    for lo, hi in gap_buckets:
        b = [(p, h) for (r, p, h) in graded
             if lo <= abs(p - american_to_implied(r["american_odds"])) < hi]
        if len(b) < args.min:
            continue
        act = sum(h for _, h in b) / len(b)
        claimed = sum(p for p, _ in b) / len(b)
        print(f"  {lo:>7.0%}–{hi:<7.0%} {len(b):>5} {act:>10.0%} "
              f"{claimed:>12.0%}")
    print("  → If hit rate FALLS as the gap grows while 'claimed' rises,\n"
          "    that is winner's curse: your biggest 'edges' are your "
          "biggest errors.")

    # ── 4. ROI by grade and by stat ─────────────────────────────────
    def roi_table(keyfn, label):
        groups = {}
        for r, p, h in graded:
            groups.setdefault(keyfn(r), []).append((r, h))
        print(f"\n  ROI BY {label} (flat 1u stakes)")
        print(f"  {'':24}{'N':>5}{'Wins':>6}{'Units':>9}{'ROI':>8}")
        for k in sorted(groups, key=lambda k: str(k)):
            g = groups[k]
            if len(g) < args.min:
                continue
            units = sum(profit_per_unit(r["american_odds"]) if h else -1
                        for r, h in g)
            wins = sum(h for _, h in g)
            print(f"  {str(k)[:23]:24}{len(g):>5}{wins:>6}"
                  f"{units:>+9.2f}{units/len(g):>8.1%}")

    roi_table(lambda r: (r["grade"] or "?")[:1], "GRADE")
    roi_table(lambda r: r["stat"], "STAT TYPE")

    print(f"\n{'='*66}\n  Re-run after every ~25 new logged results. Tune "
          "src/calibration.py\n  constants only on 100+ samples.\n")


if __name__ == "__main__":
    main()
