"""
refresh_season_data.py — Pull fresh NFL data into winweave.db

WHY THIS EXISTS
----------------
Your original database was built with R in November 2025, mid-way
through the 2025 season. That snapshot never got updated, so every
player's 2025 stats stop at whatever week the DB was built. This
script re-pulls current data so the database reflects the full,
finished season.

HOW THIS WORKS (v2 — rewritten)
--------------------------------
This uses `nflreadpy` — the official nflverse team's own Python
package (a direct port of their R nflreadr package, maintained by
the same people who publish the data). Two earlier approaches were
tried and dropped:

  1. nfl_data_py (a third-party wrapper) pins pandas<2.0, which
     fails to install on newer Python versions because pandas 1.5
     has no prebuilt wheel and can't build from source anymore.
  2. Hand-built download URLs guessing nflverse's GitHub release
     file names directly — this is fragile and got two of three
     URLs wrong.

nflreadpy avoids both problems: it's officially maintained, has no
old pandas pin (it returns Polars DataFrames, converted to pandas
here), and it constructs the correct URLs internally so we don't
have to guess them.

HOW TO RUN
----------
    cd ~/WinWeave
    source .venv/bin/activate
    pip install nflreadpy        # first time only
    python refresh_season_data.py --season 2025

    # Refresh multiple seasons at once:
    python refresh_season_data.py --season 2024 2025

WHAT IT DOES
------------
For each season given:
  1. Downloads fresh weekly player stats (props table equivalent)
  2. Downloads fresh snap counts
  3. Downloads fresh injury reports
  4. Shows a before/after row count comparison
  5. Asks for confirmation before replacing anything
  6. Refuses to apply if the fresh pull looks broken/partial
  7. Deletes old rows for that season, inserts the fresh pull

This does NOT touch pbp, rosters, games, or the advanced tables —
those are far larger and change less often. Ask if you want those
covered too.
"""

import sys
import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"


def check_nflreadpy() -> bool:
    try:
        import nflreadpy  # noqa: F401
        return True
    except ImportError:
        print("\nERROR: nflreadpy is not installed.")
        print("Run:  pip install nflreadpy")
        print("(This is the official nflverse Python package.)\n")
        return False


def to_pandas(df):
    """nflreadpy returns Polars DataFrames — convert to pandas,
    which is what the rest of this project (and SQLite writes) use."""
    if hasattr(df, "to_pandas"):
        return df.to_pandas()
    return df


def get_props_columns(conn) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(props)").fetchall()]


def fetch_weekly_stats(seasons: list[int]):
    import nflreadpy as nfl
    print(f"  Downloading weekly player stats for {seasons} via nflreadpy...")
    df = nfl.load_player_stats(seasons)
    return to_pandas(df)


def fetch_snap_counts(seasons: list[int]):
    import nflreadpy as nfl
    print(f"  Downloading snap counts for {seasons} via nflreadpy...")
    df = nfl.load_snap_counts(seasons)
    return to_pandas(df)


def fetch_injuries(seasons: list[int]):
    import nflreadpy as nfl
    print(f"  Downloading injury reports for {seasons} via nflreadpy...")
    df = nfl.load_injuries(seasons)
    return to_pandas(df)


def refresh_weekly_stats(conn, season: int, dry_run: bool = True):
    """
    Pulls fresh weekly player stats for a season and shows what
    would change. Only writes to the DB if dry_run=False.
    """
    try:
        fresh = fetch_weekly_stats([season])
    except ImportError:
        print("  ERROR: nflreadpy is not installed. Run: pip install nflreadpy")
        return None
    except Exception as e:
        print(f"  ERROR downloading data: {e}")
        return None

    if fresh is None or fresh.empty:
        print(f"  No data returned for {season}. Nothing to do.")
        return None

    db_cols = get_props_columns(conn)
    usable_cols = [c for c in fresh.columns if c in db_cols]
    missing_cols = [c for c in db_cols if c not in fresh.columns
                    and c not in ("headshot_url",)]

    if not usable_cols:
        print(f"  WARNING: none of the downloaded columns match the "
              f"props table schema. Downloaded columns: "
              f"{list(fresh.columns)[:10]}...")
        return None

    fresh = fresh[usable_cols]

    old_count = conn.execute(
        "SELECT COUNT(*) FROM props WHERE season = ?", (season,)
    ).fetchone()[0]
    old_weeks = conn.execute(
        "SELECT COUNT(DISTINCT week) FROM props WHERE season = ?", (season,)
    ).fetchone()[0]
    old_players = conn.execute(
        "SELECT COUNT(DISTINCT player_id) FROM props WHERE season = ?",
        (season,)
    ).fetchone()[0]

    new_count = len(fresh)
    new_weeks = fresh["week"].nunique() if "week" in fresh.columns else 0
    new_players = fresh["player_id"].nunique() if "player_id" in fresh.columns else 0

    print(f"\n  IMPORTANT: this replaces the ENTIRE {season} season "
          f"across every player, not just one.")
    print(f"\n  {'BEFORE':<20} {'AFTER (would be)':<20}")
    print(f"  {'-'*20} {'-'*20}")
    print(f"  {old_count:<20,} {new_count:<20,}  total rows")
    print(f"  {old_weeks:<20} {new_weeks:<20}  distinct weeks")
    print(f"  {old_players:<20,} {new_players:<20,}  distinct players")

    if missing_cols:
        print(f"\n  Note: {len(missing_cols)} DB columns aren't in the "
              f"fresh pull and will be left as NULL for new rows: "
              f"{missing_cols[:5]}{'...' if len(missing_cols) > 5 else ''}")

    # SAFETY FLOOR: refuse to replace a season with a pull that has
    # drastically fewer players than what's already there — guards
    # against a partial/broken download silently wiping out real data.
    if old_players >= 20 and new_players < old_players * 0.7:
        print(f"\n  REFUSING TO APPLY: fresh pull only has {new_players} "
              f"players vs {old_players} currently in the DB for "
              f"{season}. This looks like a partial or broken download, "
              f"not a real update. No changes made.")
        return None

    if dry_run:
        print(f"\n  (dry run — no changes made)")
        return fresh

    conn.execute("DELETE FROM props WHERE season = ?", (season,))
    fresh.to_sql("props", conn, if_exists="append", index=False)
    conn.commit()

    verify_count = conn.execute(
        "SELECT COUNT(*) FROM props WHERE season = ?", (season,)
    ).fetchone()[0]
    print(f"\n  Done. props now has {verify_count:,} rows for {season}.")
    return fresh


def refresh_snap_counts(conn, season: int, dry_run: bool = True):
    try:
        fresh = fetch_snap_counts([season])
    except Exception as e:
        print(f"  Could not download snap counts: {e}")
        return None

    if fresh is None or fresh.empty:
        print("  No snap count data returned.")
        return None

    sc_cols = [r[1] for r in
               conn.execute("PRAGMA table_info(snap_counts)").fetchall()]
    usable = [c for c in fresh.columns if c in sc_cols]
    if not usable:
        print(f"  WARNING: no matching columns for snap_counts. "
              f"Downloaded: {list(fresh.columns)[:10]}...")
        return None
    fresh = fresh[usable]

    old_count = conn.execute(
        "SELECT COUNT(*) FROM snap_counts WHERE season = ?", (season,)
    ).fetchone()[0] if "season" in sc_cols else 0

    print(f"  Before: {old_count:,} rows | After: {len(fresh):,} rows")

    if dry_run:
        print("  (dry run — no changes made)")
        return fresh

    if "season" in sc_cols:
        conn.execute("DELETE FROM snap_counts WHERE season = ?", (season,))
    fresh.to_sql("snap_counts", conn, if_exists="append", index=False)
    conn.commit()
    print("  Done.")
    return fresh


def refresh_injuries(conn, season: int, dry_run: bool = True):
    try:
        fresh = fetch_injuries([season])
    except Exception as e:
        print(f"  Could not download injury data: {e}")
        return None

    if fresh is None or fresh.empty:
        print("  No injury data returned.")
        return None

    inj_cols = [r[1] for r in
                conn.execute("PRAGMA table_info(injuries)").fetchall()]
    usable = [c for c in fresh.columns if c in inj_cols]
    if not usable:
        print(f"  WARNING: no matching columns for injuries. "
              f"Downloaded: {list(fresh.columns)[:10]}...")
        return None
    fresh = fresh[usable]

    old_count = conn.execute(
        "SELECT COUNT(*) FROM injuries WHERE season = ?", (season,)
    ).fetchone()[0] if "season" in inj_cols else 0

    print(f"  Before: {old_count:,} rows | After: {len(fresh):,} rows")

    if dry_run:
        print("  (dry run — no changes made)")
        return fresh

    if "season" in inj_cols:
        conn.execute("DELETE FROM injuries WHERE season = ?", (season,))
    fresh.to_sql("injuries", conn, if_exists="append", index=False)
    conn.commit()
    print("  Done.")
    return fresh


def main():
    parser = argparse.ArgumentParser(description="Refresh WinWeave season data")
    parser.add_argument("--season", type=int, nargs="+", required=True,
                        help="Season(s) to refresh, e.g. --season 2025")
    parser.add_argument("--skip-snaps", action="store_true")
    parser.add_argument("--skip-injuries", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    if not check_nflreadpy():
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    for season in args.season:
        print("\n" + "="*60)
        print(f"  Refreshing season {season}")
        print("="*60)

        refresh_weekly_stats(conn, season, dry_run=True)

        answer = input(f"\n  Apply this update for {season}? "
                       f"Type YES to confirm: ").strip()
        if answer != "YES":
            print(f"  Skipped {season}.")
            continue

        refresh_weekly_stats(conn, season, dry_run=False)

        if not args.skip_snaps:
            refresh_snap_counts(conn, season, dry_run=False)
        if not args.skip_injuries:
            refresh_injuries(conn, season, dry_run=False)

    conn.close()
    print("\n" + "="*60)
    print("  Refresh complete. Run validate_data.py to confirm.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
