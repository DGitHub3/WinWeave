"""
src/factors/coaching.py — Coaching Tendency Factor

Offensive coordinators have measurable tendencies that
directly affect player prop outcomes:

  - Pass-heavy OCs boost WR/TE receiving stats and QB passing stats
  - Run-heavy OCs boost RB rushing stats
  - Target distribution shows which positions get the ball
  - Red zone usage affects TD props

All of this is derivable from the PBP table you already own.

Cache: Built once per session from PBP, takes a few seconds.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"

# In-session cache: {team: {season: coaching_stats}}
_COACHING_CACHE: dict = {}

# Stat types and which coaching tendency affects them most
COACHING_IMPACT = {
    "passing_yards":   "pass_rate",
    "passing_tds":     "pass_rate",
    "receiving_yards": "pass_rate",
    "receiving_tds":   "pass_rate",
    "receptions":      "pass_rate",
    "targets":         "pass_rate",
    "rushing_yards":   "rush_rate",
    "rushing_tds":     "rush_rate",
}


def _load_team_coaching(team: str, season: int) -> dict:
    """
    Calculates offensive tendency stats for a team in a given season
    from the PBP table.

    Returns:
        pass_rate: fraction of scrimmage plays that are passes (0-1)
        rush_rate: fraction of scrimmage plays that are rushes (0-1)
        plays_per_game: pace signal (high pace = more opportunities)
    """
    cache_key = f"{team}_{season}"
    if cache_key in _COACHING_CACHE:
        return _COACHING_CACHE[cache_key]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pbp_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(pbp)").fetchall()]

        if "play_type" not in pbp_cols:
            return {}

        rows = conn.execute("""
            SELECT
                COUNT(*) as total_plays,
                SUM(CASE WHEN play_type = 'pass' THEN 1 ELSE 0 END) as passes,
                SUM(CASE WHEN play_type = 'run'  THEN 1 ELSE 0 END) as runs,
                COUNT(DISTINCT game_id) as games
            FROM pbp
            WHERE posteam = ?
              AND season = ?
              AND play_type IN ('pass', 'run')
              AND (down = 1 OR down = 2)
        """, (team, season)).fetchone()

        if not rows or not rows["total_plays"]:
            _COACHING_CACHE[cache_key] = {}
            return {}

        total = rows["total_plays"]
        passes = rows["passes"] or 0
        runs   = rows["runs"]   or 0
        games  = rows["games"]  or 1

        result = {
            "pass_rate":      passes / total if total > 0 else 0.5,
            "rush_rate":      runs   / total if total > 0 else 0.5,
            "plays_per_game": total  / games if games > 0 else 60,
            "games_analyzed": games,
        }

        _COACHING_CACHE[cache_key] = result
        return result
    finally:
        conn.close()


_LEAGUE_BASE_CACHE: dict = {}

def _league_baselines(season: int) -> dict:
    """
    v2 — SAME-BASIS LEAGUE BASELINES, computed from pbp with the
    IDENTICAL filter _load_team_coaching uses (early downs, pass/run
    only). The old code divided a team's early-down plays per game
    (~38-42) by a hardcoded 65 (a TOTAL-plays number), so every team
    scored ~0.60 and hit the 0.93 floor: the pace factor was a
    constant penalty for the entire league. Same mismatch skewed the
    pass-rate baseline. Apples now divide by apples.
    """
    if season in _LEAGUE_BASE_CACHE:
        return _LEAGUE_BASE_CACHE[season]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN play_type='pass' THEN 1 ELSE 0 END) AS p,
                   COUNT(DISTINCT game_id || posteam) AS team_games
            FROM pbp
            WHERE season = ? AND play_type IN ('pass','run')
              AND (down = 1 OR down = 2)
        """, (season,)).fetchone()
        if not row or not row["total"]:
            out = {"pass_rate": 0.55, "plays_per_game": 40.0}
        else:
            out = {"pass_rate": (row["p"] or 0) / row["total"],
                   "plays_per_game": row["total"] /
                                     max(row["team_games"], 1)}
        _LEAGUE_BASE_CACHE[season] = out
        return out
    finally:
        conn.close()


def _league_pass_rate(season: int) -> float:
    """
    Average NFL pass rate for a season — used as the baseline.
    Approximate values (modern NFL is ~57-60% pass on early downs).
    """
    rates = {
        2020: 0.575, 2021: 0.580, 2022: 0.572,
        2023: 0.582, 2024: 0.578,
    }
    return rates.get(season, 0.578)


def coaching_multiplier(team: str, season: int,
                        stat: str) -> float:
    """
    Returns a multiplier based on the team's offensive coordinator
    tendency relative to league average.

    >1.0 = pass-heavy (favors passing/receiving props)
    <1.0 = run-heavy (favors rushing props, hurts passing)

    Capped at 0.88–1.12.
    """
    tendency_key = COACHING_IMPACT.get(stat)
    if not tendency_key:
        return 1.0

    coaching = _load_team_coaching(team, season)
    if not coaching:
        return 1.0

    team_rate   = coaching.get(tendency_key, 0.5)
    base = _league_baselines(season)["pass_rate"]
    league_rate = base if tendency_key == "pass_rate" else (1 - base)

    if league_rate <= 0:
        return 1.0

    raw = team_rate / league_rate
    return max(0.88, min(1.12, raw))


def pace_factor(team: str, season: int) -> float:
    """
    Teams that run more plays per game give their players more
    opportunities. Returns a multiplier based on pace vs league avg.
    League avg is ~65 plays/game.
    """
    coaching = _load_team_coaching(team, season)
    if not coaching:
        return 1.0

    plays = coaching.get("plays_per_game", 40)
    league_avg = _league_baselines(season)["plays_per_game"] or 40.0

    raw = plays / league_avg
    return max(0.93, min(1.07, raw))


def describe_coaching(team: str, season: int) -> str:
    """Human-readable summary of coaching tendencies."""
    coaching = _load_team_coaching(team, season)
    if not coaching:
        return f"{team} {season}: No coaching data found"

    pass_rate = coaching.get("pass_rate", 0)
    pace      = coaching.get("plays_per_game", 0)
    games     = coaching.get("games_analyzed", 0)

    tendency = "pass-heavy" if pass_rate > 0.60 else \
               "run-heavy"  if pass_rate < 0.52 else "balanced"

    return (f"{team} {season} offense: {tendency} | "
            f"Pass rate: {pass_rate:.1%} | "
            f"Pace: {pace:.1f} plays/game | "
            f"Sample: {games} games")
