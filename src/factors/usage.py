"""
src/factors/usage.py — Usage / Star-Reliability Factor (NFL)

The intuition this formalizes: "a team's star is a reliable constant
in a game where anything can happen." What makes a player a reliable
constant is not his stat average — it's his CLAIM on the offense:

  1. TARGET SHARE (or carry share for RB rushing props) — the
     player's slice of his team's passing (rushing) volume, last 6
     games. A 28% target-share player gets fed in every game script;
     a 12% player's stats are script luck. From the props table.
  2. SNAP SHARE LEVEL — floor of opportunity, season-aware, from
     snap_counts (stored as 0-1 fractions).
  3. RED-ZONE SHARE — the player's slice of team plays inside the
     20, from pbp. The strongest predictor of TD props in existence.

These combine into a usage-stability score (0-1) and a multiplier:
high-usage players get a modest boost (their averages are LOAD-
BEARING); low-usage players get docked (their averages are noise).
Caps are deliberately tight (0.90-1.06) — usage refines the
projection, it doesn't replace it. QBs return neutral: a starting
QB's "usage" is definitionally ~100%.

Everything is schema-adaptive and degrades to neutral (1.0) when a
table or column is missing, so this factor can never crash a scan.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"

_USAGE_CACHE: dict = {}

RECEIVING_STATS = {"receiving_yards", "receptions", "targets",
                   "receiving_tds"}
RUSHING_STATS   = {"rushing_yards", "carries", "rushing_tds"}
TD_STATS        = {"receiving_tds", "rushing_tds"}


def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _team_col(cols: set) -> Optional[str]:
    for c in ("team", "recent_team", "team_abbr"):
        if c in cols:
            return c
    return None


def get_volume_share(player_name: str, team: str, stat: str,
                     season: int, n_games: int = 6) -> Optional[float]:
    """
    Player's share of team volume: targets share for receiving props,
    carries share for rushing props. None if not computable.
    """
    vol_col = "targets" if stat in RECEIVING_STATS else \
              "carries" if stat in RUSHING_STATS else None
    if vol_col is None:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pc = _cols(conn, "props")
        tc = _team_col(pc)
        if vol_col not in pc or not tc:
            return None
        # player's recent weeks (this season, latest n)
        weeks = [r[0] for r in conn.execute(f"""
            SELECT week FROM props
            WHERE player_display_name = ? AND season = ?
              AND {vol_col} IS NOT NULL
            ORDER BY week DESC LIMIT ?
        """, (player_name, season, n_games)).fetchall()]
        if not weeks:
            return None
        ph = ",".join("?" * len(weeks))
        player_vol = conn.execute(f"""
            SELECT COALESCE(SUM({vol_col}), 0) FROM props
            WHERE player_display_name = ? AND season = ?
              AND week IN ({ph})
        """, (player_name, season, *weeks)).fetchone()[0]
        team_vol = conn.execute(f"""
            SELECT COALESCE(SUM({vol_col}), 0) FROM props
            WHERE {tc} = ? AND season = ? AND week IN ({ph})
        """, (team, season, *weeks)).fetchone()[0]
        if not team_vol:
            return None
        return player_vol / team_vol
    finally:
        conn.close()


def get_snap_level(player_name: str, season: int,
                   n_games: int = 4) -> Optional[float]:
    """Average snap share (0-1) over recent games, season-aware."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sc = _cols(conn, "snap_counts")
        if "offense_pct" not in sc or "player" not in sc:
            return None
        season_clause = "AND season = ?" if "season" in sc else ""
        params = [player_name] + ([season] if season_clause else [])
        rows = conn.execute(f"""
            SELECT offense_pct FROM snap_counts
            WHERE player = ? {season_clause}
              AND offense_pct IS NOT NULL AND offense_pct > 0
            ORDER BY {'season DESC, ' if 'season' in sc else ''}week DESC
            LIMIT ?
        """, (*params, n_games)).fetchall()
        if not rows:
            return None
        vals = [r[0] for r in rows]
        # tolerate percent-stored data defensively
        vals = [v / 100 if v > 1.5 else v for v in vals]
        return sum(vals) / len(vals)
    finally:
        conn.close()


def get_redzone_share(player_name: str, team: str, stat: str,
                      season: int) -> Optional[float]:
    """
    Player's share of the team's red-zone opportunities (targets for
    receiving stats, carries for rushing) from pbp. None when pbp
    lacks the player-attribution columns.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pb = _cols(conn, "pbp")
        if stat in RECEIVING_STATS:
            pcol, ptype = "receiver_player_name", "pass"
        elif stat in RUSHING_STATS:
            pcol, ptype = "rusher_player_name", "run"
        else:
            return None
        needed = {pcol, "posteam", "season", "yardline_100", "play_type"}
        if not needed.issubset(pb):
            return None
        row = conn.execute(f"""
            SELECT COUNT(*) AS team_plays,
                   SUM(CASE WHEN {pcol} = ? THEN 1 ELSE 0 END) AS mine
            FROM pbp
            WHERE posteam = ? AND season = ?
              AND play_type = ? AND yardline_100 <= 20
        """, (player_name, team, season, ptype)).fetchone()
        if not row or not row["team_plays"]:
            return None
        return (row["mine"] or 0) / row["team_plays"]
    finally:
        conn.close()


def usage_multiplier(player_name: str, team: Optional[str],
                     position: Optional[str], stat: str,
                     season: int) -> tuple[float, str, Optional[float]]:
    """
    Returns (multiplier, description, stability_score).
    stability_score in 0-1 (None when nothing was computable);
    multiplier capped 0.90-1.06.
    """
    key = (player_name, team, stat, season)
    if key in _USAGE_CACHE:
        return _USAGE_CACHE[key]

    if (position or "") == "QB" or stat.startswith("passing") \
            or stat in ("completions", "attempts", "interceptions"):
        out = (1.0, "QB — usage structurally ~100%", None)
        _USAGE_CACHE[key] = out
        return out
    if not team:
        out = (1.0, "team unresolved — usage neutral", None)
        _USAGE_CACHE[key] = out
        return out

    vol  = get_volume_share(player_name, team, stat, season)
    snap = get_snap_level(player_name, season)
    rz   = get_redzone_share(player_name, team, stat, season) \
        if stat in TD_STATS or stat in RECEIVING_STATS \
        or stat in RUSHING_STATS else None

    parts, weights = [], []
    if vol is not None:
        # 25%+ share of team volume = elite; normalize on 0-30%
        parts.append(min(vol / 0.30, 1.0)); weights.append(0.5)
    if snap is not None:
        parts.append(min(snap / 0.85, 1.0)); weights.append(0.3)
    if rz is not None:
        parts.append(min(rz / 0.30, 1.0))
        # red-zone share matters double for TD props
        weights.append(0.4 if stat in TD_STATS else 0.2)
    if not parts:
        out = (1.0, "no usage data — neutral", None)
        _USAGE_CACHE[key] = out
        return out

    score = sum(p * w for p, w in zip(parts, weights)) / sum(weights)

    if score >= 0.75:
        mult, tag = 1.06, "ELITE usage — averages are load-bearing"
    elif score >= 0.55:
        mult, tag = 1.02, "solid usage"
    elif score >= 0.35:
        mult, tag = 1.00, "moderate usage"
    else:
        mult, tag = 0.90, "LOW usage — averages are script-dependent"

    bits = []
    if vol is not None:
        kind = "target" if stat in RECEIVING_STATS else "carry"
        bits.append(f"{kind} share {vol:.0%}")
    if snap is not None:
        bits.append(f"snaps {snap:.0%}")
    if rz is not None:
        bits.append(f"RZ share {rz:.0%}")
    desc = f"{tag} ({', '.join(bits)})"
    out = (mult, desc, score)
    _USAGE_CACHE[key] = out
    return out
