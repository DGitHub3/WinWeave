"""
src/factors/officials.py — Referee Crew Tendency Factor

Some referee crews produce significantly more yards and points
than others due to their penalty and game-pace tendencies.
A crew that throws fewer flags and allows more physical play
tends to favor OVER bets on yards and TDs.

Data source: officials table (already in winweave.db) joined
to games and pbp for per-game averages.

We cache crew stats in a dict on first call to avoid repeated
expensive queries during a single scan session.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"

# In-memory cache: {referee_name: {stat: value}}
_CREW_CACHE: dict = {}
_LEAGUE_AVGS: dict = {}


def _load_crew_stats() -> dict[str, dict]:
    """
    Calculates per-head-referee averages from the officials table
    joined to the games table.

    Returns: {referee_name: {total_yards_avg, plays_avg, penalty_rate}}
    """
    global _CREW_CACHE, _LEAGUE_AVGS
    if _CREW_CACHE:
        return _CREW_CACHE

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        # Find head referees (position = "Referee" in officials table)
        officials_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(officials)").fetchall()]
        games_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(games)").fetchall()]

        pos_col = "position" if "position" in officials_cols else None
        if not pos_col:
            return {}

        # Build per-head-ref game stats using the games table columns available
        # nflverse games table has: home_score, away_score, total_yards etc.
        has_total_yards = "total_yards" in games_cols
        has_home_score  = "home_score" in games_cols

        if not has_home_score:
            return {}

        rows = conn.execute(f"""
            SELECT
                o.official_name,
                COUNT(DISTINCT o.game_id) as games,
                AVG(g.home_score + g.away_score) as avg_points,
                {'AVG(g.total_yards)' if has_total_yards else 'NULL'} as avg_yards
            FROM officials o
            JOIN games g ON g.game_id = o.game_id
            WHERE o.position = 'Referee'
              AND g.season >= 2018
            GROUP BY o.official_name
            HAVING COUNT(DISTINCT o.game_id) >= 10
            ORDER BY avg_points DESC
        """).fetchall()

        if not rows:
            return {}

        # Calculate league averages across all crews
        all_points = [r["avg_points"] for r in rows
                      if r["avg_points"] is not None]
        _LEAGUE_AVGS["avg_points"] = sum(all_points) / len(all_points) \
            if all_points else 47.0

        if has_total_yards:
            all_yards = [r["avg_yards"] for r in rows
                         if r["avg_yards"] is not None]
            _LEAGUE_AVGS["avg_yards"] = sum(all_yards) / len(all_yards) \
                if all_yards else 700.0

        # Store per-referee stats
        for row in rows:
            _CREW_CACHE[row["official_name"]] = {
                "games":      row["games"],
                "avg_points": row["avg_points"],
                "avg_yards":  row["avg_yards"],
            }

        return _CREW_CACHE
    finally:
        conn.close()


def get_game_referee(game_id: str) -> Optional[str]:
    """Gets the head referee name for a specific game."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        officials_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(officials)").fetchall()]
        if "position" not in officials_cols:
            return None

        row = conn.execute("""
            SELECT official_name FROM officials
            WHERE game_id = ? AND position = 'Referee'
            LIMIT 1
        """, (game_id,)).fetchone()
        return row["official_name"] if row else None
    finally:
        conn.close()


def get_referee_for_team_week(team: str, season: int,
                               week: int) -> Optional[str]:
    """Gets the head referee for a team's game in a specific week."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT game_id FROM games
            WHERE (home_team = ? OR away_team = ?)
              AND season = ? AND week = ?
            LIMIT 1
        """, (team, team, season, week)).fetchone()

        if not row:
            return None
        return get_game_referee(row["game_id"])
    finally:
        conn.close()


def official_multiplier(referee_name: Optional[str],
                        stat: str) -> float:
    """
    Returns a multiplier based on the referee crew's historical
    tendency to produce high or low scoring/yardage games.

    >1.0 = crew historically allows more yards/points (favors overs)
    <1.0 = crew historically allows fewer yards/points (favors unders)

    Capped at 0.92–1.08 — officials matter but aren't dominant.
    """
    if not referee_name:
        return 1.0

    crew_stats = _load_crew_stats()
    if not crew_stats or referee_name not in crew_stats:
        return 1.0

    crew   = crew_stats[referee_name]
    league = _LEAGUE_AVGS

    if not league:
        return 1.0

    # Use points as proxy for offensive-friendly crew
    crew_pts   = crew.get("avg_points")
    league_pts = league.get("avg_points", 47.0)

    if crew_pts is None or league_pts <= 0:
        return 1.0

    raw = crew_pts / league_pts
    # Cap at 8% adjustment in either direction
    return max(0.92, min(1.08, raw))


def describe_crew(referee_name: Optional[str]) -> str:
    """Human-readable summary of crew tendency."""
    if not referee_name:
        return "Referee not found in database"

    crew_stats = _load_crew_stats()
    if not crew_stats or referee_name not in crew_stats:
        return f"Referee '{referee_name}' — no historical data"

    crew = crew_stats[referee_name]
    mult = official_multiplier(referee_name, "passing_yards")
    pts  = crew.get("avg_points", 0)
    games = crew.get("games", 0)

    tendency = "offense-friendly" if mult > 1.03 else \
               "defense-friendly" if mult < 0.97 else "neutral"

    return (f"Ref: {referee_name} | "
            f"Avg pts/game: {pts:.1f} | "
            f"Tendency: {tendency} ({mult:.2f}x) | "
            f"Sample: {games} games")
