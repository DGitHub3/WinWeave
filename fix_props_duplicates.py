"""
fix_props_duplicates.py — Fix duplicate rows in props table.

Uses player_id (NFL GSIS ID) + season + week as the unique key,
NOT player_display_name. This is important because multiple players
can share the same name (e.g. two different Chris Jones players in
2020). player_id is the official NFL unique identifier and is the
correct deduplication key.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python fix_props_duplicates.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def investigate(conn):
    print("\n── INVESTIGATING DUPLICATES ─────────────────────────────")

    total = conn.execute("SELECT COUNT(*) FROM props").fetchone()[0]
    print(f"  Total rows in props:          {total:,}")

    # Duplicate count using player_id (correct unique key)
    dup_combos = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT player_id, season, week, COUNT(*) cnt
            FROM props
            WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week
            HAVING cnt > 1
        )
    """).fetchone()[0]
    print(f"  Duplicate player_id+season+week combos: {dup_combos:,}")

    extra_rows = conn.execute("""
        SELECT SUM(cnt - 1) FROM (
            SELECT COUNT(*) cnt
            FROM props
            WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week
            HAVING cnt > 1
        )
    """).fetchone()[0] or 0
    print(f"  Extra rows to be removed:     {extra_rows:,}")
    print(f"  Rows remaining after fix:     {total - extra_rows:,}")

    # Show top offenders — using player_id so we see who they really are
    print("\n  Top 10 most duplicated (by player_id):")
    rows = conn.execute("""
        SELECT p.player_id, p.player_display_name, p.position,
               p.season, p.week, COUNT(*) cnt
        FROM props p
        WHERE p.player_id IS NOT NULL
        GROUP BY p.player_id, p.season, p.week
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"    {r[1]:<28} id={r[0]}  pos={r[2]:<4} "
              f"s={r[3]} w={r[4]:>2}  ({r[5]}x)")

    # Also check for null player_id rows
    null_ids = conn.execute(
        "SELECT COUNT(*) FROM props WHERE player_id IS NULL"
    ).fetchone()[0]
    if null_ids > 0:
        print(f"\n  NOTE: {null_ids:,} rows have NULL player_id "
              f"(will not be deduplicated)")

    return total, extra_rows


def verify_players(conn, label):
    checks = [
        ("Josh Allen",          "passing_yards",  2023, 3700, 4900,  4306),
        ("Patrick Mahomes",     "passing_yards",  2023, 3600, 4700,  4183),
        ("Tyreek Hill",         "receiving_yards",2023, 1400, 2100,  1799),
        ("Christian McCaffrey", "rushing_yards",  2023, 1100, 1800,  1459),
    ]
    print(f"\n  Spot-checks [{label}]:")
    all_ok = True
    for name, stat, season, lo, hi, expected in checks:
        val = conn.execute(f"""
            SELECT ROUND(SUM({stat}), 0) FROM props
            WHERE player_display_name = ? AND season = ? AND week <= 17
        """, (name, season)).fetchone()[0]
        ok = val is not None and lo <= val <= hi
        status = "OK  " if ok else "FAIL"
        print(f"    [{status}] {name:<26} {stat}: "
              f"{val} (expected ~{expected})")
        if not ok:
            all_ok = False
    return all_ok


def fix_duplicates(conn):
    """
    Keep only the row with the lowest rowid for each
    player_id + season + week combination.
    Rows with NULL player_id are left untouched.
    """
    conn.execute("""
        DELETE FROM props
        WHERE player_id IS NOT NULL
          AND rowid NOT IN (
            SELECT MIN(rowid)
            FROM props
            WHERE player_id IS NOT NULL
            GROUP BY player_id, season, week
          )
    """)
    conn.commit()


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    print("\n" + "="*60)
    print("  WinWeave — Props Duplicate Fix (player_id safe version)")
    print("="*60)

    total_before, extra_rows = investigate(conn)

    if extra_rows == 0:
        print("\n  No duplicates found. Props table is already clean.")
        conn.close()
        return

    print("\n── BEFORE FIX ───────────────────────────────────────────")
    verify_players(conn, "BEFORE")

    print("\n── CONFIRMATION ─────────────────────────────────────────")
    print(f"  Deduplication key: player_id + season + week")
    print(f"  Rows before:  {total_before:,}")
    print(f"  Rows after:   {total_before - extra_rows:,}")
    print(f"  Will remove:  {extra_rows:,} rows")
    print()
    answer = input("  Type YES to confirm, anything else cancels: ").strip()

    if answer != "YES":
        print("  Cancelled. No changes made.")
        conn.close()
        return

    print("\n── APPLYING FIX ─────────────────────────────────────────")
    print("  Removing duplicates...")
    fix_duplicates(conn)

    total_after = conn.execute("SELECT COUNT(*) FROM props").fetchone()[0]
    print(f"  Done. Rows after: {total_after:,}")

    print("\n── AFTER FIX ────────────────────────────────────────────")
    all_ok = verify_players(conn, "AFTER")

    print("\n── RESULT ───────────────────────────────────────────────")
    if all_ok:
        print("  All spot-checks passed. Props table is clean.")
        print("  You are safe to proceed to the EV engine.")
    else:
        print("  Some spot-checks still failing.")
        print("  Paste this output back for further investigation.")

    print("="*60 + "\n")
    conn.close()


if __name__ == "__main__":
    main()
