"""
explain_mlb_prop.py — Diagnostic trace for a single player/stat.

When a prop's EV% looks implausible (e.g. +800 odds on something the
model thinks hits 53% of the time — no real sportsbook would ever
leave a mispricing that large sitting on their board), this shows
every raw ingredient that went into the number: every live_odds row
for that player/stat exactly as stored, plus every individual signal
value the EV engine computed and how they combined.

This exists specifically to answer "is this a scraper/pairing bug,
or a math engine bug, or a genuinely rare real find" with actual
data instead of guessing.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python explain_mlb_prop.py "Bo Bichette" total_bases
"""

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.mlb_analyzer import analyze_mlb_prop, get_player_games, MLB_STATS

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def show_raw_odds_rows(conn: sqlite3.Connection, guess_fragment: str, stat: str):
    """
    Prints every live_odds row matching this player/stat, exactly as
    stored — every book, every side, every line, AND every scrape
    timestamp. This is the raw truth: if something looks wrong here,
    it's a scraper/pairing issue, not a math engine issue.

    IMPORTANT: live_odds is append-only by design (every scrape adds
    new rows, never overwrites) so real, current-market lines can
    stay tracked over time. That means a single player/stat can
    legitimately show many rows with different odds — those aren't
    duplicates, they're the same market at different points in time
    as it moved. This dump now shows fetched_at explicitly so that's
    visible instead of looking like corrupted data. The ACTUAL
    scan/ranking logic (scan_mlb_props) already scopes itself to only
    the single most recent fetched_at snapshot — this historical
    mixing only ever affected how this diagnostic tool displayed
    data, never the real Top Picks calculation.
    """
    rows = conn.execute("""
        SELECT player_id, book, side, line, odds, deeplink, fetched_at, event_id
        FROM live_odds
        WHERE league = 'MLB' AND stat_id = ?
          AND player_id LIKE ?
        ORDER BY fetched_at DESC, book, line, side
    """, (stat, f"%{guess_fragment.upper().replace(' ', '_')}%")).fetchall()

    if not rows:
        print(f"  No raw live_odds rows found matching '{guess_fragment}' "
              f"/ '{stat}'. Try a broader name fragment.")
        return []

    distinct_pulls = sorted({r[6] for r in rows}, reverse=True)
    print(f"\n  RAW live_odds ROWS ({len(rows)} found across "
          f"{len(distinct_pulls)} separate scrape(s) — see fetched_at "
          f"column below):")
    print(f"  {'player_id':<24} {'book':<11} {'side':<6} {'line':<6} "
          f"{'odds':<7} {'has_link':<9} {'fetched_at (UTC)'}")
    print(f"  {'-'*90}")
    for r in rows:
        has_link = "yes" if r[5] else "NO"
        print(f"  {r[0]:<24} {r[1]:<11} {r[2]:<6} {r[3]:<6} "
              f"{r[4]:<+7} {has_link:<9} {r[6]}")
    if len(distinct_pulls) > 1:
        print(f"\n  NOTE: rows span {len(distinct_pulls)} different scrape "
              f"times: {', '.join(distinct_pulls)}")
        print(f"  Only the MOST RECENT one ({distinct_pulls[0]}) is what "
              f"the actual Top Picks scan uses. Rows from older scrapes "
              f"below are historical record, not live prices anymore.")
    return rows


def show_signal_trace(player_name: str, stat: str, line: float,
                      over_odds: int, under_odds: int,
                      opponent: str, book: str,
                      is_home, opposing_starter):
    """Full breakdown of every signal that fed into the true probability."""
    try:
        r = analyze_mlb_prop(
            player_name=player_name, stat=stat, line=line,
            over_odds=over_odds, under_odds=under_odds,
            opponent=opponent, side="over", book=book,
            is_home=is_home, opposing_starter=opposing_starter,
        )
    except ValueError as e:
        print(f"  Could not analyze: {e}")
        return

    print(f"\n  HISTORICAL DATA USED:")
    table, model, _ = MLB_STATS[stat]
    games = get_player_games(player_name, stat, table, n_games=25)
    values = [g["v"] for g in games]
    print(f"  Last {len(values)} game values: {values}")
    print(f"  Mean: {r.mean_stat:.3f}  |  Std dev: {r.std_stat:.3f}  "
          f"|  Model type: {model}")

    print(f"\n  SIGNAL BREAKDOWN:")
    print(f"  Hit rate:      {r.hit_rate_signal:.1%}  "
          f"(weight {r.weights_used.get('hit_rate',0):.0%})")
    print(f"  Model:         {r.model_signal:.1%}  "
          f"(weight {r.weights_used.get('model',0):.0%})")
    print(f"  Opponent:      {r.opponent_signal:.1%}  "
          f"(weight {r.weights_used.get('opponent',0):.0%})  "
          f"[{opponent} allows {r.opp_allows:.2f}/gm]")
    print(f"  Home/Away:     {r.home_away_signal:.1%}  "
          f"(weight {r.weights_used.get('home_away',0):.0%})")
    if r.park_signal is not None:
        print(f"  Park:          {r.park_signal:.1%}  "
              f"(weight {r.weights_used.get('park',0):.0%})  "
              f"[factor {r.park_factor:.2f}x]")
    else:
        print(f"  Park:          skipped")
    if r.bvp_signal is not None:
        print(f"  Vs Starter:    {r.bvp_signal:.1%}  "
              f"(weight {r.weights_used.get('bvp',0):.0%})  "
              f"[{r.bvp_sample} games vs {opposing_starter}]")
    else:
        print(f"  Vs Starter:    skipped")

    print(f"\n  RESULT:")
    print(f"  True probability:  {r.true_probability:.1%}")
    print(f"  Odds used:         over={over_odds:+d}  under={under_odds:+d}")
    print(f"  Book implied:      {r.implied_probability:.1%} (with vig)")
    print(f"  Book fair:         {r.no_vig_probability:.1%} (no vig)")
    print(f"  EV%:               {r.ev_percent:+.2f}%")


def main():
    if len(sys.argv) < 3:
        print("Usage: python explain_mlb_prop.py \"Player Name\" stat_name")
        print(f"Valid stats: {', '.join(MLB_STATS.keys())}")
        sys.exit(1)

    player_fragment = sys.argv[1]
    stat = sys.argv[2]

    if stat not in MLB_STATS:
        print(f"Unknown stat '{stat}'. Valid options: {', '.join(MLB_STATS.keys())}")
        sys.exit(1)

    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*70}")
    print(f"  DIAGNOSTIC TRACE: {player_fragment} / {stat}")
    print(f"{'='*70}")

    rows = show_raw_odds_rows(conn, player_fragment, stat)

    if rows:
        # Find the exact real player name in mlb_players for a clean
        # analysis pass using the first matching row's data
        real_name_row = conn.execute(
            "SELECT full_name FROM mlb_players WHERE full_name LIKE ? LIMIT 1",
            (f"%{player_fragment}%",)
        ).fetchone()
        if real_name_row:
            real_name = real_name_row[0]
            print(f"\n  Matched database player: {real_name}")

            # Group ONLY the most recent scrape's rows by (book, line) —
            # matching exactly what the real scan_mlb_props() uses. Mixing
            # in older scrapes here would show a stale pairing that
            # contradicts the timestamped dump above it.
            latest_fetched_at = max(r["fetched_at"] for r in rows)
            latest_rows = [r for r in rows if r["fetched_at"] == latest_fetched_at]
            print(f"\n  DISTINCT (book, line) PAIRS FOUND "
                  f"(most recent scrape only, {latest_fetched_at}):")
            pairs = {}
            for r in latest_rows:
                key = (r["book"], r["line"])
                pairs.setdefault(key, {})[r["side"]] = r["odds"]
            for (book, line), sides in sorted(pairs.items()):
                complete = "over" in sides and "under" in sides
                print(f"    book={book:<12} line={line:<6} "
                      f"over={sides.get('over','—'):<8} "
                      f"under={sides.get('under','—'):<8} "
                      f"{'[COMPLETE PAIR]' if complete else '[INCOMPLETE]'}")

    conn.close()
    print(f"\n{'='*70}")
    print("  Compare the raw rows above against the analyzed output from")
    print("  scan_live_mlb_props.py. If a single (book, line) pair shows")
    print("  wildly mismatched over/under odds, or if multiple different")
    print("  'line' values exist for what should be one market, that's")
    print("  the scraper/pairing layer — not the math engine.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
