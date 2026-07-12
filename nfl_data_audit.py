"""
nfl_data_audit.py — NFL season-prep data audit (2026-08 readiness).

The general validator (validate_data.py) says WHETHER tables are
healthy. This audit answers the specific question that matters for
Sept 9: is the data that prop_analyzer v4 actually consumes clean
enough to bet on? It digs into exactly the six problems the July 1
health report surfaced, plus the "empty slots" question.

KEY CONCEPT — STRUCTURAL vs REAL NULLs:
  The props table is one row per player per week with ~115 stat
  columns. A QB's receiving_yards is NULL because QBs don't catch
  passes — that's STRUCTURAL and correct. A WR's receiving_yards
  being NULL is REAL dirt. This audit computes null rates only
  within the positions each stat applies to, so the numbers mean
  something.

HOW TO RUN:
    cd ~/WinWeave && source .venv/bin/activate
    python nfl_data_audit.py > data/nfl_audit_$(date +%Y%m%d).txt
    (then share the output file)
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"

# stat -> position groups it structurally applies to
STAT_POSITIONS = {
    "passing_yards":   ("QB",),
    "attempts":        ("QB",),
    "completions":     ("QB",),
    "passing_tds":     ("QB",),
    "passing_interceptions": ("QB",),
    "rushing_yards":   ("QB", "RB"),
    "carries":         ("RB",),
    "receiving_yards": ("WR", "TE", "RB"),
    "receptions":      ("WR", "TE", "RB"),
    "targets":         ("WR", "TE", "RB"),
}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    p = print
    p("=" * 68)
    p(f"  NFL DATA AUDIT — {datetime.now():%Y-%m-%d %H:%M}")
    p("=" * 68)

    # ── 1. THE DUPLICATE LANDMINE ─────────────────────────────────
    p("\n── 1. DUPLICATES (the Josh Allen 7,894-yard bug) ──")
    dup = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT player_id, season, week, COUNT(*) c FROM props
            WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week HAVING c > 1)
    """).fetchone()[0]
    extra = conn.execute("""
        SELECT COALESCE(SUM(c - 1), 0) FROM (
            SELECT COUNT(*) c FROM props
            WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week HAVING c > 1)
    """).fetchone()[0]
    p(f"  duplicate player+season+week combos: {dup:,}  "
      f"(extra rows: {extra:,})")
    p("  -> MUST be 0 before the season. Run fix_props_duplicates.py "
      "if not." if dup else "  ✅ clean")
    ja = conn.execute("""
        SELECT SUM(passing_yards) FROM props
        WHERE player_display_name = 'Josh Allen' AND season = 2023
          AND position = 'QB'
    """).fetchone()[0]
    p(f"  Josh Allen 2023 passing yards (expect ~4,306): {ja}")
    if dup:
        worst = conn.execute("""
            SELECT player_display_name, season, week, COUNT(*) c
            FROM props WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week
            ORDER BY c DESC LIMIT 5""").fetchall()
        for w in worst:
            p(f"    worst: {w[0]} {w[1]} wk{w[2]} x{w[3]}")

    # ── 2. STRUCTURAL vs REAL NULLS ("the empty slots") ──────────
    p("\n── 2. NULL RATES *within applicable positions* ──")
    props_cols = {r[1] for r in conn.execute("PRAGMA table_info(props)")}
    missing = [s for s in STAT_POSITIONS if s not in props_cols]
    if missing:
        p(f"  ⚠ expected stat columns NOT in props: {missing}")
        for m in missing:
            cands = sorted(c for c in props_cols
                           if m.split('_')[-1] in c.lower())
            p(f"    '{m}' candidates present: {cands or 'none'}")
        p("  -> these names must be aligned across the scraper map,")
        p("     LEAGUE_AVERAGES, and choose_model before the season.")
    p(f"  {'stat':18}{'positions':16}{'rows':>8}{'real null%':>12}")
    for stat, positions in STAT_POSITIONS.items():
        if stat not in props_cols:
            continue
        ph = ",".join("?" * len(positions))
        row = conn.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN {stat} IS NULL THEN 1 ELSE 0 END)
            FROM props
            WHERE position IN ({ph}) AND season >= 2023
        """, positions).fetchone()
        n, nulls = row[0], row[1] or 0
        rate = (nulls / n * 100) if n else 0
        flag = "  <- REAL GAP" if rate > 10 else ""
        p(f"  {stat:18}{'/'.join(positions):16}{n:>8,}{rate:>11.1f}%{flag}")
    p("  (a QB's NULL receiving_yards never appears here — that's "
      "the structural\n   kind of empty slot, and it is correct, "
      "not dirty)")

    # ── 3. SNAP COUNTS: scale + coverage ─────────────────────────
    p("\n── 3. SNAP COUNTS ──")
    r = conn.execute("""SELECT MIN(offense_pct), MAX(offense_pct),
        AVG(offense_pct), COUNT(*) FROM snap_counts
        WHERE offense_pct IS NOT NULL""").fetchone()
    p(f"  offense_pct min/max/avg: {r[0]} / {r[1]} / {r[2]:.3f} "
      f"({r[3]:,} rows)")
    scale = "FRACTION (0-1)" if (r[1] or 0) <= 1.001 else "PERCENT (0-100)"
    p(f"  -> stored as {scale}. Anything comparing offense_pct to "
      f"numbers like 60\n     must "
      f"{'multiply by 100 first' if scale.startswith('FRACTION') else 'use it as-is'}.")
    seasons = conn.execute("""SELECT season, COUNT(*) FROM snap_counts
        GROUP BY season ORDER BY season""").fetchall()
    p("  coverage by season: " +
      ", ".join(f"{s}: {c:,}" for s, c in seasons))
    p("  -> the roster/snap-trend factor needs 2024+2025 at minimum; "
      "backfill via\n     refresh_season_data.py if a season is "
      "missing or thin (<12,000 rows).")

    # ── 4. INJURIES: hygiene + freshness ──────────────────────────
    p("\n── 4. INJURIES ──")
    vals = conn.execute("""SELECT practice_status, COUNT(*) FROM injuries
        WHERE season >= 2023 GROUP BY practice_status
        ORDER BY 2 DESC LIMIT 8""").fetchall()
    for v, c in vals:
        shown = repr(v) if (v or "").strip() != (v or "") else v
        p(f"  {shown!s:52} {c:>8,}")
    junk = conn.execute("""SELECT COUNT(*) FROM injuries
        WHERE TRIM(COALESCE(practice_status,'')) = ''
           OR practice_status LIKE '%' || CHAR(10) || '%'""").fetchone()[0]
    p(f"  whitespace/blank status rows: {junk:,} "
      f"{'<- clean these (treated as healthy by the factor)' if junk else '✅'}")
    latest = conn.execute("SELECT MAX(season) FROM injuries").fetchone()[0]
    p(f"  latest injury season: {latest} "
      f"{'<- must reach 2026 by week 1' if (latest or 0) < 2026 else '✅'}")

    # ── 5. SEASON COMPLETENESS for the analyzer window ───────────
    p("\n── 5. RECENT-SEASON COMPLETENESS (analyzer uses last 2) ──")
    for season in (2024, 2025):
        wk = conn.execute("""SELECT COUNT(DISTINCT week), COUNT(*)
            FROM props WHERE season = ?""", (season,)).fetchone()
        p(f"  props {season}: {wk[0]} weeks, {wk[1]:,} rows")
    opp_null = conn.execute("""SELECT AVG(CASE WHEN opponent_team IS NULL
        THEN 1.0 ELSE 0 END) * 100 FROM props
        WHERE season >= 2024""").fetchone()[0]
    p(f"  opponent_team null rate (2024+): {opp_null:.1f}%  "
      f"{'<- defense factor breaks on these rows' if opp_null > 2 else '✅'}")

    # ── 6. NEXT GEN STATS completeness ────────────────────────────
    p("\n── 6. NEXT GEN STATS ──")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(next_gen_stats)")]
    p(f"  {len(cols)} columns; has avg_separation: "
      f"{'yes' if 'avg_separation' in cols else 'NO'}; "
      f"has avg_time_to_throw: "
      f"{'yes' if 'avg_time_to_throw' in cols else 'NO'}")
    p("  (5,649 rows + missing receiving columns = likely only ONE of "
      "the three NGS\n   feeds was imported. Not blocking — no factor "
      "consumes NGS yet — but note\n   for the usage-factor build.)")

    # ── 7. READINESS VERDICT ──────────────────────────────────────
    p("\n" + "=" * 68)
    blockers = []
    if dup:
        blockers.append(f"{dup} duplicate combos (run fix_props_duplicates.py)")
    if (latest or 0) < 2025:
        blockers.append("injuries stale")
    p("  BLOCKERS: " + ("; ".join(blockers) if blockers else
      "none — data layer is season-ready pending weekly refresh cadence"))
    p("=" * 68)
    conn.close()


if __name__ == "__main__":
    main()
