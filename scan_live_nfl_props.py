"""
scan_live_nfl_props.py — Closes the loop: real live odds -> EV engine
(NFL version). Mirrors scan_live_mlb_props.py's proven architecture.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python scrapers/sgo_scraper.py --league nfl   # pull fresh odds first
    python scan_live_nfl_props.py

NFL props typically don't appear on SGO until roughly 1-2 weeks
before games (August preseason onward) -- this hasn't yet been
exercised against real live odds for that reason, but shares the
exact same, already-proven MLB architecture and bug fixes.
"""

import sys
import sqlite3
import unicodedata
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.prop_analyzer import analyze_prop

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"

NFL_ABBR_TO_NAME = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
NFL_NAME_TO_ABBR = {v.upper(): k for k, v in NFL_ABBR_TO_NAME.items()}


def resolve_team(sgo_team_id: str) -> str:
    """
    Converts SGO's team ID into the abbreviation your props/rosters
    tables use. Tries two strategies since we don't know which format
    SGO actually uses for NFL, unlike MLB where we've confirmed it's
    full names:

    1. Direct abbreviation match ("DAL" -> "DAL")
    2. Full-name parse, matching MLB's confirmed pattern
       ("DALLAS_COWBOYS_NFL" -> "Dallas Cowboys" -> "DAL")

    Returns the standard 2-4 letter abbreviation your props table
    uses, or "" if neither strategy resolves it.
    """
    key = sgo_team_id.strip().upper()
    if key in NFL_ABBR_TO_NAME:
        return key  # already a valid abbreviation

    name = key[:-4] if key.endswith("_NFL") else key
    full_name = " ".join(w.capitalize() for w in name.split("_"))
    return NFL_NAME_TO_ABBR.get(full_name.upper(), "")


def guess_player_name(sgo_player_id: str) -> str:
    """Identical logic to the proven MLB version — SGO's ID format
    (FIRSTNAME_LASTNAME_NUMBER_LEAGUE) is the same across sports."""
    parts = sgo_player_id.split("_")
    if len(parts) >= 3 and parts[-2].isdigit():
        parts = parts[:-2]
    elif len(parts) >= 2 and parts[-1].isupper() and len(parts[-1]) <= 4:
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts)


def normalize_for_matching(s: str) -> str:
    """Identical to the proven MLB version — strips accents,
    punctuation, and spacing for robust name comparison."""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in ascii_only if c.isalnum()).upper()


def build_player_lookup(conn: sqlite3.Connection) -> dict:
    """Same pattern as MLB, querying props.player_display_name
    (NFL's equivalent of mlb_players.full_name)."""
    lookup = {}
    for (name,) in conn.execute(
        "SELECT DISTINCT player_display_name FROM props "
        "WHERE player_display_name IS NOT NULL"
    ):
        lookup[normalize_for_matching(name)] = name
    return lookup


def find_matching_player(lookup: dict, guess: str) -> str:
    return lookup.get(normalize_for_matching(guess), "")


def get_player_team(conn: sqlite3.Connection, full_name: str) -> str:
    """Queries rosters.team (NFL's equivalent of mlb_players.team),
    most recent season first."""
    row = conn.execute("""
        SELECT team FROM rosters
        WHERE full_name = ? ORDER BY season DESC LIMIT 1
    """, (full_name,)).fetchone()
    return row[0] if row else ""


def infer_side_from_history(conn: sqlite3.Connection, full_name: str,
                            home_abbr: str, away_abbr: str):
    """Same fallback strategy as MLB: if roster data is empty/wrong,
    check the player's own game history — whichever of tonight's two
    teams they've faced before, they must belong to the other one."""
    row = conn.execute("""
        SELECT opponent_team, COUNT(*) as n FROM props
        WHERE player_display_name = ? AND opponent_team IN (?, ?)
        GROUP BY opponent_team ORDER BY n DESC LIMIT 1
    """, (full_name, home_abbr, away_abbr)).fetchone()
    if row:
        most_common_opponent = row[0]
        return home_abbr if most_common_opponent == away_abbr else away_abbr
    return None


def fetch_latest_nfl_odds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Gets the most recent scrape's worth of NFL odds rows."""
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        "SELECT MAX(fetched_at) FROM live_odds WHERE league = 'NFL'"
    ).fetchone()[0]
    if not latest:
        return []
    return conn.execute("""
        SELECT * FROM live_odds
        WHERE league = 'NFL' AND fetched_at = ?
    """, (latest,)).fetchall()


def pair_over_under(rows: list[sqlite3.Row]) -> dict:
    """Same grouping strategy as MLB, including event_id in the key
    to prevent cross-event pairing (see MLB version for the full
    real-world story behind why that matters)."""
    grouped = defaultdict(dict)
    for r in rows:
        key = (r["event_id"], r["player_id"], r["stat_id"], r["book"], r["line"])
        deeplink = r["deeplink"] if "deeplink" in r.keys() else ""
        grouped[key][r["side"]] = {"odds": r["odds"], "deeplink": deeplink}
    return grouped


def format_game_time(starts_at: str) -> str:
    """Same Central-Time conversion as MLB."""
    if not starts_at:
        return "time unknown"
    try:
        dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        return dt_local.strftime("%b %d, %I:%M %p CT")
    except (ValueError, TypeError):
        return starts_at[:10] if len(starts_at) >= 10 else "time unknown"


def local_date(starts_at: str) -> str:
    """Same Central-Time date extraction as MLB."""
    if not starts_at:
        return ""
    try:
        dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        return dt_local.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return starts_at[:10] if len(starts_at) >= 10 else ""


def scan_nfl_props(date_filter: str = None) -> tuple[list, dict]:
    """
    Core scanning pipeline, extracted so both the CLI (below) and the
    dashboard's Top Picks tab can share the exact same logic. Mirrors
    scan_mlb_props() exactly.
    """
    diagnostics = {
        "unmatched_players": set(),
        "unmatched_teams": set(),
        "single_sided_skipped": 0,
        "available_dates": [],
        "error": None,
    }

    if not DB_PATH.exists():
        diagnostics["error"] = f"Database not found at {DB_PATH}"
        return [], diagnostics

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "live_odds" not in tables:
        diagnostics["error"] = "No live_odds table yet. Run the scraper first."
        conn.close()
        return [], diagnostics

    rows = fetch_latest_nfl_odds(conn)
    if not rows:
        diagnostics["error"] = ("No NFL odds found. Run the scraper first "
                                "with --league nfl -- note NFL props typically "
                                "don't appear until ~1-2 weeks before games "
                                "(August preseason onward).")
        conn.close()
        return [], diagnostics

    diagnostics["available_dates"] = sorted(
        {local_date(r["starts_at"]) for r in rows if r["starts_at"]}
    )

    if date_filter:
        rows = [r for r in rows if local_date(r["starts_at"]) == date_filter]
        if not rows:
            diagnostics["error"] = f"No games found on {date_filter} (Central Time)."
            conn.close()
            return [], diagnostics

    grouped = pair_over_under(rows)
    player_lookup = build_player_lookup(conn)

    results = []
    for (event_id, sgo_player_id, stat, book, line), sides in grouped.items():
        if "over" not in sides or "under" not in sides:
            diagnostics["single_sided_skipped"] += 1
            continue

        guess = guess_player_name(sgo_player_id)
        real_name = find_matching_player(player_lookup, guess)
        if not real_name:
            diagnostics["unmatched_players"].add(f"{sgo_player_id} (guessed: {guess})")
            continue

        sample_row = next((r for r in rows
                           if r["player_id"] == sgo_player_id
                           and r["stat_id"] == stat
                           and r["event_id"] == event_id), None)
        if sample_row is None:
            continue
        home_abbr = resolve_team(sample_row["home_team"])
        away_abbr = resolve_team(sample_row["away_team"])
        if not home_abbr or not away_abbr:
            diagnostics["unmatched_teams"].add(
                f"{sample_row['home_team']} / {sample_row['away_team']}")
            continue

        player_team = get_player_team(conn, real_name)
        if player_team == home_abbr:
            opponent = away_abbr
        elif player_team == away_abbr:
            opponent = home_abbr
        else:
            inferred = infer_side_from_history(conn, real_name, home_abbr, away_abbr)
            if inferred is None:
                diagnostics["unmatched_teams"].add(
                    f"{real_name}'s team '{player_team}' not in "
                    f"{{{home_abbr}, {away_abbr}}} (no game history to infer from either)")
                continue
            opponent = inferred

        game_when = format_game_time(sample_row["starts_at"])
        matchup = f"{away_abbr} @ {home_abbr} ({game_when})"

        # Evaluate BOTH sides and keep whichever the market is
        # actually mispricing -- see scan_live_mlb_props.py for the
        # full story: this used to hardcode side="over", meaning
        # "under" was never even considered. Same fix applied here
        # for consistency, even though NFL hasn't seen real live odds
        # yet this off-season.
        candidates = []
        for side in ("over", "under"):
            try:
                r = analyze_prop(
                    player_name=real_name, stat=stat, line=line,
                    over_odds=sides["over"]["odds"],
                    under_odds=sides["under"]["odds"],
                    opponent=opponent, side=side, book=book,
                )
                candidates.append(r)
            except ValueError:
                continue
        if not candidates:
            continue
        r = max(candidates, key=lambda c: c.ev_percent)
        r.matchup = matchup
        r.starts_at = sample_row["starts_at"]  # raw ISO timestamp -- lets
                                                # the Tracker store a real
                                                # game_date instead of none
        r.deeplink = sides["over"]["deeplink"] or sides["under"]["deeplink"]
        results.append(r)

    conn.close()
    return results, diagnostics


def main():
    import argparse
    parser = argparse.ArgumentParser(description="WinWeave live NFL prop scanner")
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()

    results, diag = scan_nfl_props(date_filter=args.date)

    if diag["error"]:
        print(f"\n{diag['error']}\n")
        return

    if diag["available_dates"]:
        print(f"\nGame dates in this pull (Central Time): "
              f"{', '.join(diag['available_dates'])}")

    if not results:
        print("No props could be fully analyzed.")
    else:
        results.sort(key=lambda r: r.ev_percent, reverse=True)
        print(f"{'='*110}")
        print(f"  LIVE NFL PROPS — {len(results)} ANALYZED, SORTED BY EV%")
        print(f"{'='*110}")
        for r in results:
            sign = "+" if r.ev_percent >= 0 else ""
            print(f"  {r.player_name:<20} {r.stat:<18} {r.line:<6} "
                  f"{r.american_odds:<+7} {r.book:<12} {r.true_probability:<7.1%} "
                  f"{sign}{r.ev_percent:<8.2f} {r.grade():<28} {r.matchup}")
        print(f"{'='*110}\n")

    if diag["unmatched_players"]:
        print(f"Could not match {len(diag['unmatched_players'])} player name(s):")
        for p in sorted(diag["unmatched_players"]):
            print(f"  {p}")

    if diag["unmatched_teams"]:
        print(f"Could not resolve {len(diag['unmatched_teams'])} team/roster issue(s):")
        for t in sorted(diag["unmatched_teams"]):
            print(f"  {t}")

    if diag["single_sided_skipped"]:
        print(f"Skipped {diag['single_sided_skipped']} single-sided market(s).")


if __name__ == "__main__":
    main()
