"""
src/factors/roster.py — Roster Health Factor

Combines three signals from your existing database:

1. Injury status: is the player limited/out? Is supporting cast hurt?
   (e.g. if the starting LT is out, QB passing yards may drop)

2. Snap percentage trend: is the player's usage going UP or DOWN?
   A WR with 80% → 65% → 58% snap share trend is a red flag
   regardless of what the line says.

3. Depth chart position: is the player still starting?
   A depth chart demotion since last week matters.

All data comes from your existing injuries, snap_counts,
and depth_charts tables.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"

# Injury status severity mapping
# Lower number = more severe (more reduction in multiplier)
INJURY_SEVERITY = {
    "Did Not Participate In Practice":    0.65,  # Very likely to miss or be limited
    "Did Not Participate in Practice":    0.65,
    "DNP":                                0.65,
    "Limited Participation in Practice":  0.85,  # Will play but reduced
    "Limited Participation":              0.85,
    "LP":                                 0.85,
    "Questionable":                       0.90,
    "Full Participation in Practice":     1.00,  # No impact
    "Full Participation":                 1.00,
    "FP":                                 1.00,
    "Out (Definitely Will Not Play)":     0.00,  # Should not be in props
    "Out":                                0.00,
}


def get_injury_multiplier(player_name: str,
                           current_week: int = None,
                           current_season: int = None) -> tuple[float, str]:
    """
    Returns (multiplier, status_description) based on injury report.

    multiplier 1.0 = healthy
    multiplier 0.0 = player is out (should not bet this prop)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        inj_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(injuries)").fetchall()]

        name_col = "full_name" if "full_name" in inj_cols else None
        if not name_col:
            return 1.0, "No injury data available"

        # Build query filtering by week/season if provided
        season_clause = f"AND season = {current_season}" \
            if current_season else ""
        week_clause = f"AND week <= {current_week}" \
            if current_week else ""

        row = conn.execute(f"""
            SELECT practice_status FROM injuries
            WHERE {name_col} = ?
              {season_clause}
              {week_clause}
            ORDER BY season DESC, week DESC
            LIMIT 1
        """, (player_name,)).fetchone()

        if not row:
            return 1.0, "Not on injury report (healthy)"

        # v2 FIX — STALE INJURIES: without a season filter this grabs
        # the most recent report EVER, so a player listed "Out" in
        # week 18 of LAST season carried a 0.0 multiplier forever
        # (and every offseason analysis used year-old statuses).
        # A report only counts if it's from the season being analyzed.
        if current_season and "season" in inj_cols:
            row_season = conn.execute(f"""
                SELECT season FROM injuries
                WHERE {name_col} = ? {season_clause} {week_clause}
                ORDER BY season DESC, week DESC LIMIT 1
            """, (player_name,)).fetchone()
            if row_season and row_season[0] != current_season:
                return 1.0, "No current-season injury report (healthy)"

        status = row["practice_status"]
        if status:
            status = status.strip()

        multiplier = INJURY_SEVERITY.get(status, 1.0)
        return multiplier, status or "Unknown status"
    finally:
        conn.close()


def get_snap_trend(player_name: str,
                   n_weeks: int = 6) -> tuple[float, str]:
    """
    Analyzes snap percentage trend over recent weeks.

    Returns (multiplier, description):
      - Strong upward trend: 1.05
      - Neutral/stable: 1.00
      - Strong downward trend: 0.90

    Snap percentage is stored as decimal (0.85 = 85%).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sc_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(snap_counts)").fetchall()]

        if "offense_pct" not in sc_cols:
            return 1.0, "No snap data available"

        # v2 FIX — CROSS-SEASON POLLUTION: the old ORDER BY week DESC
        # ignored season, so week numbers from different years
        # interleaved (2024 week 18 sorted above 2025 week 3), making
        # the "trend" compare arbitrary games. Season now leads the
        # sort when the column exists.
        season_sort = "season DESC, " if "season" in sc_cols else ""
        rows = conn.execute(f"""
            SELECT week, offense_pct
            FROM snap_counts
            WHERE player = ?
              AND offense_pct IS NOT NULL
              AND offense_pct > 0
            ORDER BY {season_sort}week DESC
            LIMIT ?
        """, (player_name, n_weeks)).fetchall()

        if not rows:
            return 1.0, "Player not found in snap_counts"

        snaps = [r["offense_pct"] * 100 for r in rows]  # convert to %
        avg_recent = sum(snaps[:3]) / len(snaps[:3]) if len(snaps) >= 3 else snaps[0]
        avg_older  = sum(snaps[3:]) / len(snaps[3:]) if len(snaps) > 3 else avg_recent

        trend_pct = avg_recent - avg_older

        if trend_pct > 8:
            mult = 1.05
            desc = f"RISING snap share: {avg_recent:.0f}% (was {avg_older:.0f}%)"
        elif trend_pct < -8:
            mult = 0.92
            desc = f"FALLING snap share: {avg_recent:.0f}% (was {avg_older:.0f}%)"
        else:
            mult = 1.0
            desc = f"Stable snap share: {avg_recent:.0f}%"

        # Absolute level matters too — below 50% is concerning
        if avg_recent < 50:
            mult = min(mult, 0.90)
            desc += f" — LOW snap share warning (<50%)"

        return mult, desc
    finally:
        conn.close()


def get_depth_chart_position(player_name: str,
                              team: str) -> tuple[int, str]:
    """
    Returns (depth_position, description).
    depth_position 1 = starter, 2 = backup, etc.
    Returns (1, "Not in depth chart system") if not found.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        dc_cols = [r[1] for r in
            conn.execute("PRAGMA table_info(depth_charts)").fetchall()]

        name_col = "player_name" if "player_name" in dc_cols else None
        if not name_col:
            return 1, "No depth chart data"

        depth_col = next(
            (c for c in ["depth_chart_position", "depth_position", "position_rank"]
             if c in dc_cols), None
        )
        team_col = next(
            (c for c in ["team", "club_code", "depth_team"]
             if c in dc_cols), None
        )

        if not depth_col or not team_col:
            return 1, "Depth chart columns not found"

        row = conn.execute(f"""
            SELECT {depth_col} as depth_pos
            FROM depth_charts
            WHERE {name_col} LIKE ?
              AND {team_col} = ?
            ORDER BY dt DESC
            LIMIT 1
        """, (f"%{player_name}%", team)).fetchone()

        if not row or row["depth_pos"] is None:
            return 1, f"Not found in depth chart for {team}"

        depth = int(row["depth_pos"])
        if depth == 1:
            return 1, "Starter (depth position 1)"
        elif depth == 2:
            return 2, "Backup (depth position 2)"
        else:
            return depth, f"Depth position {depth}"
    finally:
        conn.close()


def roster_multiplier(player_name: str, team: str,
                      season: int = None,
                      week: int = None) -> tuple[float, dict]:
    """
    Combined roster health multiplier from all three signals.
    v2: accepts season/week so the injury signal can enforce
    current-season-only staleness rules (see get_injury_multiplier).

    Returns (multiplier, detail_dict) where detail_dict has
    all three sub-signals for display in the analysis.
    """
    inj_mult,  inj_desc  = get_injury_multiplier(player_name, week, season)
    snap_mult, snap_desc = get_snap_trend(player_name)
    depth_pos, depth_desc = get_depth_chart_position(player_name, team)

    # Depth chart: penalty for non-starters
    depth_mult = 1.0 if depth_pos == 1 else \
                 0.85 if depth_pos == 2 else 0.75

    # If player is listed OUT, override everything
    if inj_mult == 0.0:
        combined = 0.0
    else:
        # Weighted combination
        combined = (
            inj_mult  * 0.45 +  # injury status most impactful
            snap_mult * 0.35 +  # snap trend very important
            depth_mult * 0.20   # depth chart confirmatory
        )

    details = {
        "injury_status":  inj_desc,
        "injury_mult":    inj_mult,
        "snap_trend":     snap_desc,
        "snap_mult":      snap_mult,
        "depth_chart":    depth_desc,
        "depth_mult":     depth_mult,
        "combined":       combined,
    }

    return combined, details
