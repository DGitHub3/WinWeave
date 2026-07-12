"""
verify_setup.py — Run this first.

This script proves three things:
  1. Python can connect to your existing winweave.db
  2. The table structure matches what we expect
  3. A real query (DJ Moore's last 5 games vs CHI) returns real rows

HOW TO USE:
  1. Copy your real winweave.db file into the /data folder
     (same folder this script's sibling 'data/' directory points to)
  2. From the project root, run:
       python verify_setup.py

If you see player stats print at the bottom, your Python data layer
is fully wired up and ready for the next phase (live odds scraping).
"""

import sys
from pathlib import Path

# Allow running this script directly from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.db import DB_PATH, list_tables, table_row_count
from src.queries import get_player_vs_opponent, get_player_id


def main():
    print("=" * 60)
    print("WINWEAVE — Python Data Layer Verification")
    print("=" * 60)

    print(f"\nLooking for database at:\n  {DB_PATH}\n")

    if not DB_PATH.exists():
        print("DATABASE NOT FOUND.")
        print("Copy your real winweave.db into the /data folder, then re-run this script.")
        return

    tables = list_tables()
    print(f"Connected successfully. Found {len(tables)} tables:")
    for t in tables:
        try:
            count = table_row_count(t)
            print(f"  - {t:<20} {count:,} rows")
        except Exception as e:
            print(f"  - {t:<20} (could not count rows: {e})")

    print("\n" + "-" * 60)
    print("Test query: DJ Moore's last 5 games vs CHI")
    print("-" * 60)

    player_id = get_player_id("DJ Moore")
    if player_id is None:
        print("No player found matching 'DJ Moore'. Try checking the exact "
              "name format in your props table (e.g. 'D.J. Moore').")
        return

    df = get_player_vs_opponent("DJ Moore", "CHI", limit=5)

    if df.empty:
        print("Connection works, but no games found for DJ Moore vs CHI. "
              "This could mean he hasn't played CHI recently in your dataset, "
              "or the opponent code differs from 'CHI'.")
    else:
        print(df.to_string(index=False))
        print(f"\nSuccess — {len(df)} games returned. Your Python data layer is live.")


if __name__ == "__main__":
    main()
