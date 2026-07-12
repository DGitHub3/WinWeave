"""
db.py — WinWeave database connection helper.

Connects to your EXISTING winweave.db SQLite database.
This does not rebuild or duplicate anything — it just opens
a connection to the database you already built with R.

SQLite files are language-agnostic. Python can read the exact
same .db file that download_and_build.R and add_advanced_data.R
created. Nothing about your 1.25M+ rows of historical data needs
to be touched or re-downloaded.
"""

import sqlite3
from pathlib import Path
from contextlib import contextmanager

# Path to the database, relative to the project root.
# Drop your real winweave.db into the /data folder and this
# will find it automatically.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "winweave.db"


@contextmanager
def get_connection(db_path: Path = DB_PATH):
    """
    Opens a connection to winweave.db and guarantees it closes,
    even if an error happens mid-query.

    Usage:
        with get_connection() as conn:
            cursor = conn.execute("SELECT 1")
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"Could not find database at {db_path}\n"
            f"Copy your real winweave.db into the /data folder, "
            f"or pass a custom path to get_connection(db_path=...)."
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    try:
        yield conn
    finally:
        conn.close()


def list_tables(db_path: Path = DB_PATH) -> list[str]:
    """Returns every table name currently in the database."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


def table_row_count(table_name: str, db_path: Path = DB_PATH) -> int:
    """Returns the row count for a given table. Useful for sanity-checking
    that your database loaded correctly after a refresh."""
    with get_connection(db_path) as conn:
        # table_name is never user input in practice here, but we still
        # validate it against the real table list to avoid injection.
        valid_tables = list_tables(db_path)
        if table_name not in valid_tables:
            raise ValueError(f"'{table_name}' is not a real table. Found: {valid_tables}")
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return row["n"]
