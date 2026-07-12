"""
queries.py — Reusable player & team queries for WinWeave.

This is the Python rebuild of the old query_helpers.R and the
copy-paste templates from WINWEAVE_v2.0_CHEAT_SHEET. Same logic,
two real improvements:

1. Parameterized queries (the "?" placeholders) instead of pasting
   player names directly into SQL strings. The old R scripts built
   queries with paste0(), which is a SQL injection risk even in a
   personal tool — if a player name ever had a quote in it, like
   "Le'Veon Bell", the old style of query would have broken or
   misbehaved. This version handles that safely by default.

2. Every function returns a pandas DataFrame, so results plug
   straight into the dashboard, a CSV export, or further analysis
   without any reformatting.
"""

from typing import Optional
import pandas as pd
from src.db import get_connection


def get_player_id(player_name: str) -> Optional[str]:
    """Looks up a player's nflverse player_id from the props table."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT player_id FROM props WHERE player_display_name = ? LIMIT 1",
            (player_name,),
        ).fetchone()
    return row["player_id"] if row else None


def get_player_vs_opponent(player_name: str, opponent_team: str, limit: int = 5) -> pd.DataFrame:
    """
    Pulls a player's last N games against a specific opponent.
    This is the core "is the Bears game a good spot for this player"
    query — mirrors the "Last 5 vs SPECIFIC OPPONENT" template from
    the old cheat sheet.
    """
    query = """
        SELECT season, week, opponent_team,
               passing_yards, rushing_yards, receiving_yards, passing_tds
        FROM props
        WHERE player_display_name = ?
          AND opponent_team = ?
        ORDER BY season DESC, week DESC
        LIMIT ?
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(player_name, opponent_team, limit))
    return df


def get_player_season(player_name: str, season: int) -> pd.DataFrame:
    """Full season stat line for a player, week by week."""
    query = """
        SELECT week, opponent_team, passing_yards, rushing_yards,
               receiving_yards, passing_tds, passing_epa
        FROM props
        WHERE player_display_name = ? AND season = ?
        ORDER BY week
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(player_name, season))
    return df


def get_player_snap_pct(player_name: str, limit: int = 3) -> pd.DataFrame:
    """Recent snap percentage trend for a player — a workload signal
    that tends to predict prop performance better than raw stats alone."""
    query = """
        SELECT week, offense_pct
        FROM snap_counts
        WHERE player = ?
        ORDER BY week DESC
        LIMIT ?
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(player_name, limit))
    return df


def get_player_injury_status(player_name: str) -> pd.DataFrame:
    """Most recent injury report entry for a player."""
    query = """
        SELECT week, practice_status
        FROM injuries
        WHERE full_name = ?
        ORDER BY week DESC
        LIMIT 1
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(player_name,))
    return df


def get_team_schedule(team: str, season: int) -> pd.DataFrame:
    """Full season schedule with opponent resolved (home or away)."""
    query = """
        SELECT game_id, gameday,
               CASE WHEN home_team = ? THEN away_team ELSE home_team END AS opponent
        FROM games
        WHERE (home_team = ? OR away_team = ?) AND season = ?
        ORDER BY gameday
    """
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=(team, team, team, season))
    return df


def list_all_players() -> pd.DataFrame:
    """Every player name in the props table — useful for autocomplete
    in the future dashboard search bar."""
    query = "SELECT DISTINCT player_display_name FROM props ORDER BY player_display_name"
    with get_connection() as conn:
        df = pd.read_sql_query(query, conn)
    return df
