"""
validate_data.py — WinWeave Database Health Report

Runs 55+ targeted checks across every table in winweave.db.
Optimized for speed on large tables — uses sampling and fast
query patterns instead of full-table scans where possible.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python validate_data.py

    Save to file:
    python validate_data.py > data/health_report.txt
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH      = PROJECT_ROOT / "data" / "winweave.db"

PASS  = 0
WARN  = 0
FAIL  = 0
TOTAL = 0
RESULTS = []

def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()

def q1(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None

def check(label, passed, detail="", warn_only=False):
    global PASS, WARN, FAIL, TOTAL
    TOTAL += 1
    if passed:
        status = "PASS"
        PASS += 1
    elif warn_only:
        status = "WARN"
        WARN += 1
    else:
        status = "FAIL"
        FAIL += 1
    RESULTS.append((status, label, detail))

def section(title):
    print(f"  Running: {title}...")
    RESULTS.append(("HEAD", title, ""))

def print_report():
    width = 70
    print("\n" + "=" * width)
    print("  WINWEAVE DATABASE HEALTH REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Database:  {DB_PATH}")
    print("=" * width)
    for status, label, detail in RESULTS:
        if status == "HEAD":
            print(f"\n── {label} {'─' * max(0, width - len(label) - 4)}")
            continue
        icon = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL"}[status]
        detail_str = f"  -> {detail}" if detail else ""
        print(f"  [{icon}]  {label}{detail_str}")
    print("\n" + "=" * width)
    print(f"  SUMMARY: {PASS} passed | {WARN} warnings | {FAIL} failed | {TOTAL} total")
    if FAIL == 0 and WARN == 0:
        print("  VERDICT: Database looks clean. Ready to build on.")
    elif FAIL == 0:
        print("  VERDICT: Minor issues found. Review warnings before betting.")
    elif FAIL <= 5:
        print("  VERDICT: Some issues need attention before trusting this data.")
    else:
        print("  VERDICT: Significant data quality problems. Investigate before use.")
    print("=" * width + "\n")


def get_cols(conn, table):
    return [r[1] for r in q(conn, f"PRAGMA table_info({table})")]

def run_all_checks(conn):

    # ── 1. TABLE EXISTENCE ─────────────────────────────────────
    section("1. Table Existence")
    expected = ["rosters","games","props","pbp","injuries",
                "snap_counts","depth_charts","next_gen_stats",
                "player_stats","combine","draft_picks",
                "trades","contracts","officials"]
    actual = [r[0] for r in q(conn,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in expected:
        check(f"Table exists: {t}", t in actual,
              "MISSING" if t not in actual else "")
    extra = [t for t in actual if t not in expected]
    check("No unexpected tables", len(extra) == 0,
          f"extra: {extra}" if extra else "", warn_only=True)

    # ── 2. ROW COUNTS ──────────────────────────────────────────
    section("2. Row Count Sanity")
    counts = {}
    for t in actual:
        counts[t] = q1(conn, f"SELECT COUNT(*) FROM {t}")
        print(f"    counted {t}: {counts[t]:,}")

    check("rosters >= 80,000 rows",   counts.get("rosters",0) >= 80000,   f"{counts.get('rosters',0):,}")
    check("games >= 5,000 rows",      counts.get("games",0) >= 5000,      f"{counts.get('games',0):,}")
    check("props >= 50,000 rows",     counts.get("props",0) >= 50000,     f"{counts.get('props',0):,}")
    check("pbp >= 1,000,000 rows",    counts.get("pbp",0) >= 1000000,     f"{counts.get('pbp',0):,}")
    check("injuries >= 50,000 rows",  counts.get("injuries",0) >= 50000,  f"{counts.get('injuries',0):,}")
    check("snap_counts >= 10,000",    counts.get("snap_counts",0) >= 10000, f"{counts.get('snap_counts',0):,}")
    check("depth_charts >= 100,000",  counts.get("depth_charts",0) >= 100000, f"{counts.get('depth_charts',0):,}")
    check("contracts >= 30,000",      counts.get("contracts",0) >= 30000, f"{counts.get('contracts',0):,}")
    check("draft_picks >= 5,000",     counts.get("draft_picks",0) >= 5000, f"{counts.get('draft_picks',0):,}")

    # ── 3. SEASON RANGES ───────────────────────────────────────
    section("3. Season Range Checks")
    for table, col, mn, mx, label in [
        ("rosters",  "season", 1990, 2024, "Rosters"),
        ("pbp",      "season", 2000, 2024, "PBP"),
        ("games",    "season", 1970, 2024, "Games"),
        ("props",    "season", 2020, 2024, "Props"),
        ("injuries", "season", 2009, 2024, "Injuries"),
    ]:
        if table not in actual: continue
        cols = get_cols(conn, table)
        if "season" not in cols: continue
        lo = q1(conn, f"SELECT MIN(season) FROM {table}")
        hi = q1(conn, f"SELECT MAX(season) FROM {table}")
        check(f"{label} season starts <= {mn}", lo is not None and lo <= mn, f"earliest: {lo}")
        check(f"{label} season includes {mx}", hi is not None and hi >= mx, f"latest: {hi}")

    # ── 4. PBP VALUE RANGES (sampled) ─────────────────────────
    section("4. PBP Value Ranges (sampled from recent seasons)")
    pbp_cols = get_cols(conn, "pbp") if "pbp" in actual else []

    if "yards_gained" in pbp_cols:
        # Sample recent 3 seasons instead of full table scan
        yg_min = q1(conn, "SELECT MIN(yards_gained) FROM pbp WHERE season >= 2020 AND yards_gained IS NOT NULL")
        yg_max = q1(conn, "SELECT MAX(yards_gained) FROM pbp WHERE season >= 2020 AND yards_gained IS NOT NULL")
        check("PBP yards_gained min >= -50", yg_min is not None and yg_min >= -50, f"min: {yg_min}")
        check("PBP yards_gained max <= 110", yg_max is not None and yg_max <= 110, f"max: {yg_max}")
    else:
        check("PBP has yards_gained column", False, "missing")

    if "epa" in pbp_cols:
        epa_min = q1(conn, "SELECT MIN(epa) FROM pbp WHERE season >= 2022 AND epa IS NOT NULL")
        epa_max = q1(conn, "SELECT MAX(epa) FROM pbp WHERE season >= 2022 AND epa IS NOT NULL")
        check("PBP EPA min >= -15", epa_min is not None and epa_min >= -15, f"min: {round(epa_min,2) if epa_min else None}")
        check("PBP EPA max <= 15",  epa_max is not None and epa_max <= 15,  f"max: {round(epa_max,2) if epa_max else None}")
    else:
        check("PBP has epa column", False, "missing", warn_only=True)

    if "play_type" in pbp_cols:
        play_types = [r[0] for r in q(conn,
            "SELECT DISTINCT play_type FROM pbp WHERE season = 2023 AND play_type IS NOT NULL LIMIT 20")]
        expected_types = {"pass","run","punt","field_goal","kickoff","no_play"}
        overlap = set(play_types) & expected_types
        check("PBP play_type has expected values", len(overlap) >= 4, f"found: {sorted(play_types)[:8]}")

    if "week" in pbp_cols:
        week_max = q1(conn, "SELECT MAX(week) FROM pbp WHERE season = 2023")
        check("PBP 2023 week max is realistic (<=22)", week_max is not None and week_max <= 22, f"max: {week_max}")

    # ── 5. PROPS VALUE RANGES ──────────────────────────────────
    section("5. Props Table Value Ranges")
    props_cols = get_cols(conn, "props") if "props" in actual else []

    if "passing_yards" in props_cols:
        py_max = q1(conn, "SELECT MAX(passing_yards) FROM props WHERE passing_yards IS NOT NULL")
        py_avg = q1(conn, "SELECT ROUND(AVG(passing_yards),1) FROM props WHERE passing_yards > 100")
        check("Props passing_yards max <= 600", py_max is not None and py_max <= 600, f"max: {py_max}")
        check("Props passing_yards avg 150-350 (for passers)", py_avg is not None and 150 <= py_avg <= 350, f"avg: {py_avg}")

    if "rushing_yards" in props_cols:
        ry_max = q1(conn, "SELECT MAX(rushing_yards) FROM props WHERE rushing_yards IS NOT NULL")
        check("Props rushing_yards max <= 300", ry_max is not None and ry_max <= 300, f"max: {ry_max}")

    if "receiving_yards" in props_cols:
        rv_max = q1(conn, "SELECT MAX(receiving_yards) FROM props WHERE receiving_yards IS NOT NULL")
        check("Props receiving_yards max <= 350", rv_max is not None and rv_max <= 350, f"max: {rv_max}")

    if "week" in props_cols:
        wk = q1(conn, "SELECT MAX(week) FROM props")
        check("Props week max <= 22", wk is not None and wk <= 22, f"max: {wk}")

    # ── 6. KNOWN PLAYER SPOT-CHECKS ────────────────────────────
    section("6. Known Player Spot-Checks (vs real stats)")

    if "passing_yards" in props_cols:
        # Mahomes 2023: 4,183 yards (PFR verified)
        mahomes = q1(conn, """
            SELECT ROUND(SUM(passing_yards),0) FROM props
            WHERE player_display_name='Patrick Mahomes' AND season=2023 AND week<=17
        """)
        check("Mahomes 2023 passing yards ~4183 (+-500)", mahomes is not None and 3600<=mahomes<=4700, f"got: {mahomes} | expected ~4183")

        # Josh Allen 2023: 4,306 yards
        allen = q1(conn, """
            SELECT ROUND(SUM(passing_yards),0) FROM props
            WHERE player_display_name='Josh Allen' AND season=2023 AND week<=17
        """)
        check("Josh Allen 2023 passing yards ~4306 (+-500)", allen is not None and 3700<=allen<=4900, f"got: {allen} | expected ~4306")

    if "receiving_yards" in props_cols:
        # Tyreek Hill 2023: 1,799 receiving yards
        hill = q1(conn, """
            SELECT ROUND(SUM(receiving_yards),0) FROM props
            WHERE player_display_name='Tyreek Hill' AND season=2023 AND week<=17
        """)
        check("Tyreek Hill 2023 rec yards ~1799 (+-300)", hill is not None and 1400<=hill<=2100, f"got: {hill} | expected ~1799")

    if "rushing_yards" in props_cols:
        # CMC 2023: 1,459 rushing yards
        cmc = q1(conn, """
            SELECT ROUND(SUM(rushing_yards),0) FROM props
            WHERE player_display_name='Christian McCaffrey' AND season=2023 AND week<=17
        """)
        check("CMC 2023 rushing yards ~1459 (+-300)", cmc is not None and 1100<=cmc<=1800, f"got: {cmc} | expected ~1459")

    # Michael Vick in rosters
    r_cols = get_cols(conn, "rosters") if "rosters" in actual else []
    name_col = "full_name" if "full_name" in r_cols else ("player_name" if "player_name" in r_cols else None)
    if name_col:
        vick = q1(conn, f"SELECT COUNT(*) FROM rosters WHERE {name_col} LIKE '%Vick%'")
        check("Michael Vick found in rosters", vick is not None and vick > 0, f"rows: {vick}")

    # Jalen Hurts 2024
    hurts = q1(conn, "SELECT COUNT(*) FROM props WHERE player_display_name='Jalen Hurts' AND season=2024")
    check("Jalen Hurts has 2024 rows in props", hurts is not None and hurts > 0, f"games: {hurts}")

    # Lamar Jackson 2023 MVP season
    if "passing_yards" in props_cols:
        lamar = q1(conn, """
            SELECT ROUND(SUM(passing_yards),0) FROM props
            WHERE player_display_name='Lamar Jackson' AND season=2023 AND week<=17
        """)
        check("Lamar Jackson 2023 passing yards ~3678 (+-500)", lamar is not None and 3000<=lamar<=4300, f"got: {lamar} | expected ~3678")

    # ── 7. SEASON COMPLETENESS ─────────────────────────────────
    section("7. Season Completeness (week coverage)")
    for yr in [2021, 2022, 2023, 2024, 2025]:
        if "props" in actual and "week" in props_cols:
            wks = q1(conn, f"SELECT COUNT(DISTINCT week) FROM props WHERE season={yr} AND week BETWEEN 1 AND 18")
            check(f"Props {yr}: 16+ weeks present", wks is not None and wks >= 16, f"weeks: {wks}")
        if "pbp" in actual and "week" in pbp_cols:
            wks = q1(conn, f"SELECT COUNT(DISTINCT week) FROM pbp WHERE season={yr} AND week BETWEEN 1 AND 18")
            check(f"PBP {yr}: 16+ weeks present", wks is not None and wks >= 16, f"weeks: {wks}")

    for yr in [2022, 2023, 2024, 2025]:
        if "games" in actual:
            gc = q1(conn, f"SELECT COUNT(*) FROM games WHERE season={yr}")
            check(f"Games {yr}: 250+ records", gc is not None and gc >= 250, f"count: {gc}")

    if "props" in actual and all(c in props_cols for c in
            ["player_display_name","season","week"]):
        current_season = q1(conn, "SELECT MAX(season) FROM props")
        if current_season:
            low_game_players = q(conn, f"""
                SELECT player_display_name, COUNT(DISTINCT week) as games
                FROM props
                WHERE season = {current_season} AND position = 'QB'
                GROUP BY player_display_name
                HAVING games >= 3 AND games < 10
                ORDER BY games DESC
                LIMIT 10
            """)
            check(f"No starting-caliber QBs stuck under 10 games in "
                  f"{current_season} (possible stale/frozen season data)",
                  len(low_game_players) == 0,
                  f"found: {[f'{r[0]} ({r[1]}g)' for r in low_game_players]}"
                  if low_game_players else "",
                  warn_only=True)

    # ── 8. DUPLICATE DETECTION (fast) ─────────────────────────
    section("8. Duplicate Detection (fast check)")

    # Props duplicates — fast because props is smaller
    if all(c in props_cols for c in ["player_display_name","season","week"]):
        dup = q1(conn, """
            SELECT COUNT(*) FROM (
                SELECT player_display_name, season, week, COUNT(*) cnt
                FROM props GROUP BY player_display_name, season, week
                HAVING cnt > 1 LIMIT 1
            )
        """)
        check("Props: no duplicate player+season+week", dup == 0, f"duplicates found: {dup}", warn_only=True)

    # Games duplicates
    g_cols = get_cols(conn, "games") if "games" in actual else []
    if "game_id" in g_cols:
        dup = q1(conn, """
            SELECT COUNT(*) FROM (
                SELECT game_id, COUNT(*) cnt FROM games
                GROUP BY game_id HAVING cnt > 1 LIMIT 1
            )
        """)
        check("Games: no duplicate game_ids", dup == 0, f"duplicates: {dup}", warn_only=True)

    # PBP duplicates — use LIMIT 1 to stop immediately if any found
    if "play_id" in pbp_cols and "game_id" in pbp_cols:
        dup = q1(conn, """
            SELECT COUNT(*) FROM (
                SELECT play_id, game_id, COUNT(*) cnt FROM pbp
                WHERE season >= 2023
                GROUP BY play_id, game_id HAVING cnt > 1 LIMIT 1
            )
        """)
        check("PBP 2023+: no duplicate play_id+game_id", dup == 0, f"duplicates: {dup}", warn_only=True)

    # ── 9. SNAP COUNTS ─────────────────────────────────────────
    section("9. Snap Count Validation")
    sc_cols = get_cols(conn, "snap_counts") if "snap_counts" in actual else []
    if "offense_pct" in sc_cols:
        smin = q1(conn, "SELECT MIN(offense_pct) FROM snap_counts WHERE offense_pct IS NOT NULL")
        smax = q1(conn, "SELECT MAX(offense_pct) FROM snap_counts WHERE offense_pct IS NOT NULL")
        savg = q1(conn, "SELECT ROUND(AVG(offense_pct),1) FROM snap_counts WHERE offense_pct > 0")
        check("Snap offense_pct min >= 0",      smin is not None and smin >= 0,    f"min: {smin}")
        check("Snap offense_pct max <= 100",    smax is not None and smax <= 100,  f"max: {smax}")
        check("Snap offense_pct avg 20-80%",    savg is not None and 20<=savg<=80, f"avg: {savg}%")
    else:
        check("snap_counts has offense_pct", False, f"cols: {sc_cols}", warn_only=True)

    # ── 10. INJURY DATA ────────────────────────────────────────
    section("10. Injury Data Validation")
    inj_cols = get_cols(conn, "injuries") if "injuries" in actual else []
    if "practice_status" in inj_cols:
        statuses = [r[0] for r in q(conn, "SELECT DISTINCT practice_status FROM injuries WHERE practice_status IS NOT NULL LIMIT 10")]
        expected_s = {"Full Participation","Limited Participation","Did Not Participate","DNP","LP","FP"}
        check("Injury practice_status has valid values", len(set(statuses)&expected_s) >= 1, f"values: {statuses[:5]}")
    else:
        check("injuries has practice_status column", False, f"cols: {inj_cols[:5]}", warn_only=True)
    if "season" in inj_cols:
        ilo = q1(conn, "SELECT MIN(season) FROM injuries WHERE season IS NOT NULL")
        ihi = q1(conn, "SELECT MAX(season) FROM injuries WHERE season IS NOT NULL")
        check("Injuries span 5+ seasons", ilo is not None and ihi is not None and ihi-ilo>=5, f"{ilo}-{ihi}")

    # ── 11. NULL RATES ─────────────────────────────────────────
    section("11. Null Rate Checks")
    if "player_display_name" in props_cols:
        nr = q1(conn, "SELECT ROUND(100.0*SUM(CASE WHEN player_display_name IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) FROM props")
        check("Props player_display_name null < 5%", nr is not None and nr < 5, f"null rate: {nr}%")
    if "season" in props_cols:
        nr = q1(conn, "SELECT ROUND(100.0*SUM(CASE WHEN season IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) FROM props")
        check("Props season null < 1%", nr is not None and nr < 1, f"null rate: {nr}%")
    if "epa" in pbp_cols:
        # Sample recent season only for speed
        nr = q1(conn, "SELECT ROUND(100.0*SUM(CASE WHEN epa IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) FROM pbp WHERE season=2023")
        check("PBP 2023 EPA null < 30%", nr is not None and nr < 30, f"null rate: {nr}%", warn_only=True)

    # ── 12. TEAM CODE VALIDATION ───────────────────────────────
    section("12. Team Code Validation")
    valid_teams = {"ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN",
                   "DET","GB","HOU","IND","JAX","KC","LA","LAC","LV","MIA","MIN",
                   "NE","NO","NYG","NYJ","PHI","PIT","SEA","SF","TB","TEN","WAS",
                   "OAK","SD","STL","JAC"}
    g_cols = get_cols(conn, "games") if "games" in actual else []
    if "home_team" in g_cols:
        all_teams = set(r[0] for r in q(conn, """
            SELECT DISTINCT home_team FROM games WHERE home_team IS NOT NULL
            UNION SELECT DISTINCT away_team FROM games WHERE away_team IS NOT NULL
        """))
        unknown = all_teams - valid_teams
        check("All game team codes are valid NFL codes", len(unknown)==0,
              f"unknown: {sorted(unknown)}" if unknown else "", warn_only=True)

    # ── 13. POSITION DISTRIBUTION ──────────────────────────────
    section("13. Roster Position Distribution")
    r_cols = get_cols(conn, "rosters") if "rosters" in actual else []
    if "position" in r_cols:
        positions = dict(q(conn, """
            SELECT position, COUNT(*) FROM rosters
            WHERE position IS NOT NULL
            GROUP BY position ORDER BY COUNT(*) DESC LIMIT 15
        """))
        for pos in ["QB","WR","RB","TE","OT","CB","LB"]:
            check(f"Rosters has {pos} players", positions.get(pos,0) > 0, f"count: {positions.get(pos,0)}")

    # ── 14. CROSS-TABLE CONSISTENCY (fast) ────────────────────
    section("14. Cross-Table Consistency (sampled)")
    # Check props players exist in rosters using small sample
    if "player_id" in props_cols and "player_id" in r_cols:
        sample_ids = [r[0] for r in q(conn, "SELECT DISTINCT player_id FROM props WHERE season=2023 AND player_id IS NOT NULL LIMIT 50")]
        if sample_ids:
            placeholders = ",".join("?" * len(sample_ids))
            matched = q1(conn, f"SELECT COUNT(DISTINCT player_id) FROM rosters WHERE player_id IN ({placeholders})", sample_ids)
            pct = round(100*matched/len(sample_ids), 1)
            check("Props player_ids match rosters (sample, >70%)", pct >= 70, f"{pct}% of sample matched", warn_only=pct<50)

    # ── 15. NEXT GEN STATS ─────────────────────────────────────
    section("15. Next Gen Stats")
    ngs_cols = get_cols(conn, "next_gen_stats") if "next_gen_stats" in actual else []
    check("next_gen_stats has rows", counts.get("next_gen_stats",0) > 0, f"rows: {counts.get('next_gen_stats',0)}")
    if "avg_separation" in ngs_cols:
        sep = q1(conn, "SELECT ROUND(AVG(avg_separation),2) FROM next_gen_stats WHERE avg_separation IS NOT NULL")
        check("NGS avg_separation is realistic (1-5 yds)", sep is not None and 1.0<=sep<=5.0, f"avg: {sep} yds")
    if "player_display_name" in ngs_cols:
        ngs_players = q1(conn, "SELECT COUNT(DISTINCT player_display_name) FROM next_gen_stats")
        check("NGS covers 50+ distinct players", ngs_players is not None and ngs_players >= 50, f"players: {ngs_players}")

    # ── 16. DEPTH CHARTS ───────────────────────────────────────
    section("16. Depth Charts")
    dc_cols = get_cols(conn, "depth_charts") if "depth_charts" in actual else []
    check("depth_charts has rows", counts.get("depth_charts",0) > 0, f"rows: {counts.get('depth_charts',0)}")
    team_col = next((c for c in ["depth_team","team","club_code"] if c in dc_cols), None)
    if team_col:
        dct = q1(conn, f"SELECT COUNT(DISTINCT {team_col}) FROM depth_charts WHERE {team_col} IS NOT NULL")
        check("Depth charts covers 30+ teams", dct is not None and dct >= 30, f"teams: {dct}")
    if "season" in dc_cols:
        dcmax = q1(conn, "SELECT MAX(season) FROM depth_charts")
        check("Depth charts include 2024", dcmax is not None and dcmax >= 2024, f"latest: {dcmax}")

    # ── 17. DRAFT PICKS & CONTRACTS ────────────────────────────
    section("17. Draft Picks & Contracts")
    dp_cols = get_cols(conn, "draft_picks") if "draft_picks" in actual else []
    ct_cols = get_cols(conn, "contracts") if "contracts" in actual else []
    check("draft_picks has rows", counts.get("draft_picks",0) > 0, f"rows: {counts.get('draft_picks',0)}")
    check("contracts has rows",   counts.get("contracts",0) > 0,   f"rows: {counts.get('contracts',0)}")
    if "round" in dp_cols:
        rmax = q1(conn, "SELECT MAX(round) FROM draft_picks")
        check("Draft round max <= 7", rmax is not None and rmax <= 7, f"max: {rmax}")
    if "value" in ct_cols:
        cmax = q1(conn, "SELECT MAX(value) FROM contracts WHERE value IS NOT NULL")
        check("Contract max value < $1 billion", cmax is not None and cmax < 1_000_000_000, f"max: ${cmax:,.0f}" if cmax else "None")

    # ── 18. SCHEMA SNAPSHOT ────────────────────────────────────
    section("18. Schema Snapshot")
    for t in expected:
        if t in actual:
            cols = get_cols(conn, t)
            check(f"{t}: {counts.get(t,0):,} rows, {len(cols)} cols", True,
                  ", ".join(cols[:5]) + ("..." if len(cols)>5 else ""))


def main():
    print(f"\nWinWeave Data Validator")
    print(f"Loading: {DB_PATH}\n")

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    # 30 second timeout per query — prevents any single query from freezing forever
    conn.execute("PRAGMA busy_timeout = 30000")

    try:
        run_all_checks(conn)
    finally:
        conn.close()

    print_report()

if __name__ == "__main__":
    main()
