"""
scan_live_game_markets.py — WinWeave game-level market scanner (MLB).

Reads the latest game_odds snapshot (written by sgo_scraper.py, which
extracts game markets from the SAME events payload as player props —
zero extra API cost), analyzes every moneyline, run line, and total
through the calibrated team model, and prints a graded board.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python scrapers/sgo_scraper.py --league mlb   # refresh odds first
    python scan_live_game_markets.py
    python scan_live_game_markets.py --track      # interactive tracking

EXPECTATIONS CHECK: game markets (especially moneylines) are the most
efficient prices in sports. A healthy board here is overwhelmingly
F-grades with occasional C/B. If this scanner shows a page of A's,
something upstream broke — trust the suspicion, not the EV column.
"""

import argparse
import sqlite3
from pathlib import Path

from src.game_analyzer import (
    analyze_game_market, match_team_name, save_game_prediction,
)

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def load_latest_game_odds() -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute("""
            SELECT MAX(fetched_at) FROM game_odds WHERE league = 'MLB'
        """).fetchone()[0]
        if not latest:
            return []
        return conn.execute("""
            SELECT * FROM game_odds
            WHERE league = 'MLB' AND fetched_at = ?
        """, (latest,)).fetchall()
    finally:
        conn.close()


def pair_and_analyze(rows: list[sqlite3.Row], diag: dict = None) -> list:
    """
    Groups rows into two-sided markets per (event, market, book, line
    where relevant), removes vig, analyzes both sides, keeps each
    side's result (the board shows the best; both are computed).
    """
    groups: dict = {}
    for r in rows:
        if r["market"] == "moneyline":
            key = (r["event_id"], "moneyline", r["book"])
        elif r["market"] == "total":
            key = (r["event_id"], "total", r["book"], r["line"])
        else:  # run_line: sides have mirrored lines (-1.5 / +1.5)
            key = (r["event_id"], "run_line", r["book"], abs(r["line"]))
        groups.setdefault(key, []).append(r)

    results, skipped_onesided = [], 0
    unmatched_teams: set = set()
    for key, sides in groups.items():
        if len(sides) != 2:
            skipped_onesided += 1
            continue
        a, b = sides
        home = match_team_name(a["home_team"])
        away = match_team_name(a["away_team"])
        if not home or not away:
            if not home:
                unmatched_teams.add(a["home_team"])
            if not away:
                unmatched_teams.add(a["away_team"])
            continue
        for this, other in ((a, b), (b, a)):
            res = analyze_game_market(
                market=this["market"], home_team=home, away_team=away,
                side=this["side"], line=this["line"],
                side_odds=this["odds"], other_odds=other["odds"],
                book=this["book"], starts_at=this["starts_at"] or "",
                deeplink=this["deeplink"] or "")
            if res:
                results.append(res)

    if skipped_onesided:
        print(f"  Skipped {skipped_onesided} one-sided market(s) — "
              f"can't remove vig without both sides.")
    if diag is not None:
        diag["single_sided_skipped"] = skipped_onesided
        diag["unmatched_teams"] = sorted(unmatched_teams)
    return results


def scan_game_markets():
    """
    Dashboard entry point — same (results, diag) contract as
    scan_mlb_props()/scan_nfl_props(), so render_top_picks can treat
    game markets exactly like another prop feed.
    """
    from src.game_analyzer import local_game_date
    diag = {"error": None, "available_dates": [],
            "single_sided_skipped": 0, "unmatched_players": [],
            "unmatched_teams": []}
    rows = load_latest_game_odds()
    if not rows:
        diag["error"] = ("No game-market odds yet — use Pull Fresh "
                        "Odds (MLB) first; game markets are extracted "
                        "automatically alongside player props.")
        return [], diag
    results = pair_and_analyze(rows, diag)
    diag["available_dates"] = sorted({
        d for d in (local_game_date(r["starts_at"] or "") for r in rows)
        if d})
    return results, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", action="store_true",
                    help="interactively track picks after the scan")
    ap.add_argument("--all", action="store_true",
                    help="show every side (default hides F grades)")
    args = ap.parse_args()

    rows = load_latest_game_odds()
    if not rows:
        print("No game-market odds found. Run "
              "'python scrapers/sgo_scraper.py --league mlb' first — "
              "game markets are extracted automatically alongside props.")
        return

    print(f"\nAnalyzing {len(rows)} game-market lines from the latest "
          f"pull ({rows[0]['fetched_at'][:16]} UTC)...")
    results = pair_and_analyze(rows)
    results.sort(key=lambda r: r.ev_percent, reverse=True)

    shown = [r for r in results
             if args.all or not r.grade().startswith("F")]

    w = 118
    print("=" * w)
    print(f"  GAME-LEVEL MARKETS — {len(results)} analyzed, "
          f"{len(shown)} shown (use --all for every side)")
    print("=" * w)
    print(f"  {'#':>3} {'Matchup':34}{'Bet':26}{'Odds':>7} {'Book':11}"
          f"{'True%':>7}{'Fair%':>7}{'EV%':>8}  Grade")
    print("  " + "-" * (w - 4))
    for i, r in enumerate(shown, 1):
        matchup = f"{r.away_team} @ {r.home_team}"[:33]
        print(f"  {i:>3} {matchup:34}{r.describe()[:25]:26}"
              f"{r.american_odds:>+7d} {r.book:11}"
              f"{r.true_probability:>6.1%}{r.no_vig_probability:>7.1%}"
              f"{r.ev_percent:>+8.2f}  {r.grade()[:48]}")
    print("=" * w)
    if not shown:
        print("  Nothing above F grade — normal for game markets, "
              "which are the sharpest lines books offer.")

    if args.track and shown:
        print("\nTrack picks: enter a row #, then bet amount "
              "(0 = tracking only). Blank to finish.")
        while True:
            choice = input("  Row # (or Enter to finish): ").strip()
            if not choice:
                break
            try:
                r = shown[int(choice) - 1]
            except (ValueError, IndexError):
                print("  Not a valid row number.")
                continue
            stake_in = input(f"  Stake for {r.describe()} "
                             f"({r.american_odds:+d})? [$0]: ").strip()
            try:
                stake = float(stake_in) if stake_in else 0.0
            except ValueError:
                stake = 0.0
            rid = save_game_prediction(r, bet_placed=stake > 0,
                                       stake=stake)
            print(f"  Tracked as prop_results id {rid} "
                  f"({'bet $%.2f' % stake if stake > 0 else 'no stake'}). "
                  f"Auto-resolve will grade it after the game.")


if __name__ == "__main__":
    main()
