"""
sgo_scraper.py — SportsGameOdds live odds scraper for WinWeave.

Pulls live player prop lines from SportsGameOdds for NFL or MLB and
writes them into a live_odds table in your existing winweave.db.
Both sports share the same table — the dashboard's Live Odds tab
already reads from it, so MLB props show up there automatically.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python scrapers/sgo_scraper.py              # NFL (default)
    python scrapers/sgo_scraper.py --league mlb # MLB

FIRST TIME SETUP:
    Your API key needs to be in a file called keys.txt inside
    your WinWeave folder, formatted exactly like this:

        SGO_API_KEY=paste_your_key_here

HONEST NOTE ON MLB STAT NAMES:
    NFL's stat-ID names (passing_yards, interceptions, etc.) are
    confirmed from SportsGameOdds' own documentation. MLB's are our
    best guess based on common naming conventions, NOT confirmed
    the same way. If the MLB pull comes back with zero matches
    despite real odds being available, this script will print the
    raw stat names it actually found so we can fix the guess list
    with real data instead of guessing again.
"""

import sys
import argparse
import requests
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH      = PROJECT_ROOT / "data" / "winweave.db"
KEYS_FILE    = PROJECT_ROOT / "keys.txt"

# ── API config ────────────────────────────────────────────────
SGO_BASE_URL = "https://api.sportsgameodds.com/v2"

# Bookmakers available on the free tier
TARGET_BOOKS = ["fanduel", "draftkings", "betmgm", "caesars"]
# espnbet removed: as of Dec 1 2025, DraftKings became ESPN's official
# odds/sportsbook provider (Penn Entertainment partnership ended), so
# "espnbet" prices are no longer an independent source -- they're
# effectively DraftKings odds under a different label during the
# 2026 rollout, making them redundant (and occasionally stale) rather
# than a genuinely separate line to compare against.

# Confirmed from SportsGameOdds documentation
NFL_STATS = [
    "passing_yards", "rushing_yards", "receiving_yards",
    "receptions", "passing_tds", "rushing_tds", "receiving_tds",
]
NFL_STAT_MAP = {s: s for s in NFL_STATS}  # SGO names already match ours
# CONFIRMED from the 2026-07-11 offseason diagnostic pull: SGO's NFL
# feed also emits these IDs, which don't match our internal names.
# ("touchdowns" = anytime TD and "defense_interceptions" = team
# defense — neither maps to a single-player over/under stat we model,
# so they stay unmapped on purpose.)
NFL_STAT_MAP["passing_touchdowns"]    = "passing_tds"
NFL_STAT_MAP["passing_interceptions"] = "interceptions"

# CONFIRMED from a real diagnostic pull (2026-07-03) — SGO's actual
# MLB stat IDs use a "batting_"/"pitching_" prefix with camelCase,
# not the snake_case guess this file originally shipped with.
# Left side = SGO's real name, right side = our internal MLB_STATS key.
MLB_STAT_MAP = {
    "batting_hits":          "hits",
    "batting_homeRuns":      "home_runs",
    "batting_totalBases":    "total_bases",
    "batting_RBI":           "rbi",
    "batting_basesOnBalls":  "walks",
    "batting_stolenBases":   "stolen_bases",
    "batting_strikeouts":    "batter_strikeouts",
    "pitching_strikeouts":   "strikeouts",
    "pitching_outs":         "outs_recorded",
    "pitching_basesOnBalls": "walks_allowed",
    "pitching_earnedRuns":   "earned_runs",
    "pitching_hits":         "hits_allowed",
    # Not mapped (not single-number over/under props in our engine):
    #   batting_doubles, batting_triples, batting_singles,
    #   batting_firstHomeRun, batting_hits+runs+rbi (combo prop),
    #   fantasyScore, pitching_pitchesThrown, pitching_win, points
}

LEAGUE_CONFIG = {
    "nfl": {"api_league_id": "NFL", "stat_map": NFL_STAT_MAP},
    "mlb": {"api_league_id": "MLB", "stat_map": MLB_STAT_MAP},
}


def load_api_key() -> str:
    """
    Reads SGO_API_KEY from keys.txt in your WinWeave folder.
    Format expected:
        SGO_API_KEY=your_key_here
    """
    if not KEYS_FILE.exists():
        print(f"ERROR: Could not find keys.txt at {KEYS_FILE}")
        print("Create a file called keys.txt in your WinWeave folder.")
        print("Put this inside it:  SGO_API_KEY=your_key_here")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("SGO_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key

    print("ERROR: SGO_API_KEY not found in keys.txt")
    print("Make sure keys.txt contains a line like:  SGO_API_KEY=your_key_here")
    sys.exit(1)


def fetch_events(api_key: str, league: str) -> list[dict]:
    """
    Fetches upcoming events with player prop odds from SGO for
    the given league.

    Key facts about this call:
    - One event = 1 object consumed (per SGO pricing)
    - All markets for that game come inside that one object
    - oddsAvailable=true skips games with no odds yet
    """
    api_league_id = LEAGUE_CONFIG[league]["api_league_id"]
    print(f"Fetching upcoming {api_league_id} events from SportsGameOdds...")

    url = f"{SGO_BASE_URL}/events"
    headers = {"x-api-key": api_key}
    params = {
        "leagueID": api_league_id,
        "oddsAvailable": "true",
        "limit": 50,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            print("ERROR: API key rejected (401). Check your key in keys.txt.")
        elif response.status_code == 429:
            print("ERROR: Rate limit hit (429). Wait a minute and try again.")
        else:
            print(f"ERROR: HTTP {response.status_code} — {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("ERROR: Could not connect to SportsGameOdds. Check your internet.")
        sys.exit(1)

    data = response.json()
    events = data.get("data", [])
    print(f"  Found {len(events)} upcoming {api_league_id} events with odds available.")
    return events


def get_usage_info(api_key: str) -> dict:
    """
    Structured version of the SGO usage check -- returns a dict
    instead of printing, so callers like the dashboard's Refresh
    button can show it however fits their UI. check_usage() (below)
    is the CLI-printing wrapper around this for backward compatibility.

    Returns {"tier": str, "remaining": int|None, "max": int|str|None,
    "current": int|None, "error": str|None}. "max" can be the literal
    string "unlimited" on some tiers.
    """
    url = f"{SGO_BASE_URL}/account/usage"
    headers = {"x-api-key": api_key}
    params = {"apiKey": api_key}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.exceptions.RequestException as e:
        return {"tier": None, "remaining": None, "max": None,
                "current": None, "error": f"Request failed: {e}"}

    if response.status_code != 200:
        return {"tier": None, "remaining": None, "max": None,
                "current": None,
                "error": f"Usage check returned HTTP {response.status_code}"}

    try:
        payload = response.json()
    except ValueError:
        return {"tier": None, "remaining": None, "max": None,
                "current": None, "error": "Could not parse usage response"}

    if not payload.get("success"):
        return {"tier": None, "remaining": None, "max": None,
                "current": None,
                "error": payload.get("error", "unknown error")}

    data = payload.get("data", {})
    tier = data.get("tier", "unknown")
    monthly = data.get("rateLimits", {}).get("per-month", {})
    max_entities = monthly.get("max-entities")
    current_entities = monthly.get("current-entities")

    if max_entities == "unlimited":
        return {"tier": tier, "remaining": None, "max": "unlimited",
                "current": current_entities, "error": None}
    if isinstance(max_entities, (int, float)) and isinstance(current_entities, (int, float)):
        return {"tier": tier, "remaining": max_entities - current_entities,
                "max": max_entities, "current": current_entities, "error": None}
    return {"tier": tier, "remaining": None, "max": None, "current": None,
            "error": f"Unexpected response shape: {data.get('rateLimits')}"}


def check_usage(api_key: str):
    """
    Prints your remaining API quota (CLI use). See get_usage_info()
    for the structured version used by the dashboard.
    """
    info = get_usage_info(api_key)
    if info["error"]:
        print(f"  (Usage check: {info['error']} — not critical, continuing.)")
    elif info["max"] == "unlimited":
        print(f"  SGO usage ({info['tier']} tier): unlimited monthly objects.")
    else:
        print(f"  SGO usage ({info['tier']} tier): {info['remaining']:,} of "
              f"{info['max']:,} monthly objects remaining "
              f"({info['current']:,} used).")


def dedupe_events(events: list[dict]) -> list[dict]:
    """
    Removes exact-duplicate event listings by eventID before processing.

    A real diagnostic pull showed the same event appearing 3 times in
    the raw API response with identical odds each time — this collapses
    that back down to one copy. This does NOT merge different event
    IDs that happen to represent the same real-world game (that's a
    separate, harder problem — see the event-scoping fix in
    scan_live_mlb_props.py's pair_over_under()).
    """
    seen = set()
    unique = []
    for e in events:
        eid = e.get("eventID")
        if eid and eid not in seen:
            seen.add(eid)
            unique.append(e)
        elif not eid:
            unique.append(e)  # no ID to dedupe on, keep it
    removed = len(events) - len(unique)
    if removed > 0:
        print(f"  Removed {removed} exact-duplicate event listing(s).")
    return unique


def parse_game_markets(events: list[dict], league: str,
                       books: list[str] = None) -> tuple[list[dict], set]:
    """
    Extracts GAME-LEVEL markets from the same raw SGO event data that
    parse_player_props reads — moneylines, run lines (spreads), and
    game totals. These arrive inside the exact same event objects as
    the player props, so pulling them consumes ZERO additional API
    objects.

    SGO oddID format for game markets (entity is home/away/all
    instead of a player ID):
        points-home-game-ml-home      moneyline, home side
        points-away-game-ml-away      moneyline, away side
        points-home-game-sp-home      spread / run line, home side
        points-away-game-sp-away      spread / run line, away side
        points-all-game-ou-over       game total, over
        points-all-game-ou-under      game total, under

    Returns (rows, bet_types_seen) — the second value feeds a
    diagnostic if SGO's real structure differs from the above, same
    honest-fallback pattern as the player-prop parser.
    """
    books = books if books is not None else TARGET_BOOKS
    events = dedupe_events(events)
    rows = []
    bet_types_seen: set = set()
    fetched_at = datetime.now(timezone.utc).isoformat()

    for event in events:
        event_id  = event.get("eventID", "")
        starts_at = event.get("status", {}).get("startsAt", "")
        home_team = event.get("teams", {}).get("home", {}).get("teamID", "")
        away_team = event.get("teams", {}).get("away", {}).get("teamID", "")
        odds_dict = event.get("odds", {})

        for odd_id, odd_data in odds_dict.items():
            parts = odd_id.split("-")
            if len(parts) < 5:
                continue
            stat_id, entity, period_id, bet_type, side = \
                parts[0], parts[1], parts[2], parts[3], parts[4]

            # Game-level = entity is home/away/all, full game only
            if entity not in ("home", "away", "all") or period_id != "game":
                continue
            bet_types_seen.add(f"{stat_id}-{entity}-{bet_type}")

            if bet_type == "ml" and entity in ("home", "away"):
                market, mkt_side, needs_line = "moneyline", entity, False
            elif bet_type == "sp" and entity in ("home", "away"):
                market, mkt_side, needs_line = "run_line", entity, True
            elif bet_type == "ou" and entity == "all" \
                    and side in ("over", "under"):
                market, mkt_side, needs_line = "total", side, True
            else:
                continue

            by_book = odd_data.get("byBookmaker", {})
            for book_id in books:
                bd = by_book.get(book_id, {})
                if not bd.get("available", False):
                    continue
                odds = bd.get("odds")
                if odds is None:
                    continue
                # SGO exposes the number as 'spread' for sp markets
                # and 'overUnder' for totals; read both defensively.
                line = bd.get("spread") if market == "run_line" \
                    else bd.get("overUnder")
                if needs_line and line is None:
                    continue

                rows.append({
                    "event_id":   event_id,
                    "starts_at":  starts_at,
                    "home_team":  home_team,
                    "away_team":  away_team,
                    "market":     market,
                    "side":       mkt_side,
                    "book":       book_id,
                    "line":       float(line) if line is not None else 0.0,
                    "odds":       int(odds),
                    "deeplink":   bd.get("deeplink", "") or "",
                    "fetched_at": fetched_at,
                    "league":     league.upper(),
                })

    return rows, bet_types_seen


def create_game_odds_table(conn: sqlite3.Connection):
    """
    game_odds — game-level sibling of live_odds. Append-only for the
    same reason: the fetch history doubles as a line-movement ledger
    (and later, closing-line-value data).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS game_odds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL,
            starts_at   TEXT,
            home_team   TEXT,
            away_team   TEXT,
            market      TEXT NOT NULL,   -- moneyline | run_line | total
            side        TEXT NOT NULL,   -- home | away | over | under
            book        TEXT NOT NULL,
            line        REAL NOT NULL,   -- 0.0 for moneyline
            odds        INTEGER NOT NULL,
            deeplink    TEXT DEFAULT '',
            fetched_at  TEXT NOT NULL,
            league      TEXT DEFAULT 'MLB'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_game_odds_event
        ON game_odds (event_id, market, fetched_at)
    """)
    conn.commit()


def save_game_odds(rows: list[dict]):
    """Writes parsed game-market rows into the game_odds table."""
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    create_game_odds_table(conn)
    conn.executemany("""
        INSERT INTO game_odds
            (event_id, starts_at, home_team, away_team, market, side,
             book, line, odds, deeplink, fetched_at, league)
        VALUES
            (:event_id, :starts_at, :home_team, :away_team, :market,
             :side, :book, :line, :odds, :deeplink, :fetched_at, :league)
    """, rows)
    conn.commit()
    conn.close()


def parse_player_props(events: list[dict], league: str,
                       books: list[str] = None) -> tuple[list[dict], set]:
    """
    Extracts player prop lines from raw SGO event data.

    SGO oddID format for player props:
        {statID}-{PLAYER_ID}-game-ou-over
        {statID}-{PLAYER_ID}-game-ou-under

    books: which bookmakers to pull. Defaults to all of TARGET_BOOKS.
    Pass a subset (e.g. ["fanduel"]) to scan only that book -- useful
    when you only have an account at one sportsbook.

    Returns (matched_rows, all_stat_ids_seen). The second value lets
    the caller show a diagnostic if our guessed stat names (MLB
    especially) don't match what SGO actually uses.
    """
    books = books if books is not None else TARGET_BOOKS
    events = dedupe_events(events)
    stat_map = LEAGUE_CONFIG[league]["stat_map"]
    rows = []
    all_stat_ids_seen: set = set()
    fetched_at = datetime.now(timezone.utc).isoformat()

    for event in events:
        event_id   = event.get("eventID", "")
        starts_at  = event.get("status", {}).get("startsAt", "")
        home_team  = event.get("teams", {}).get("home", {}).get("teamID", "")
        away_team  = event.get("teams", {}).get("away", {}).get("teamID", "")
        odds_dict  = event.get("odds", {})

        for odd_id, odd_data in odds_dict.items():
            # oddID format: statID-playerID-periodID-betTypeID-sideID
            parts = odd_id.split("-")
            if len(parts) < 5:
                continue

            raw_stat_id = parts[0]
            player_id   = parts[1]
            period_id   = parts[2]
            bet_type    = parts[3]
            side        = parts[4]

            if player_id not in ("home", "away", "all"):
                all_stat_ids_seen.add(raw_stat_id)

            # Only want player props (ou = over/under), full game period
            if bet_type != "ou" or period_id != "game":
                continue

            # Canonicalize SGO's real name to our internal MLB_STATS key
            internal_stat = stat_map.get(raw_stat_id)
            if internal_stat is None:
                continue

            # Skip non-player entities (home/away/all are team/game level)
            if player_id in ("home", "away", "all"):
                continue

            # Pull lines from each bookmaker we care about
            by_book = odd_data.get("byBookmaker", {})
            for book_id in books:
                book_data = by_book.get(book_id, {})
                if not book_data.get("available", False):
                    continue

                line = book_data.get("overUnder")
                odds = book_data.get("odds")
                deeplink = book_data.get("deeplink", "") or ""

                if line is None or odds is None:
                    continue

                rows.append({
                    "event_id":   event_id,
                    "starts_at":  starts_at,
                    "home_team":  home_team,
                    "away_team":  away_team,
                    "player_id":  player_id,
                    "stat_id":    internal_stat,
                    "side":       side,
                    "book":       book_id,
                    "line":       float(line),
                    "odds":       int(odds),
                    "deeplink":   deeplink,
                    "fetched_at": fetched_at,
                    "league":     league.upper(),
                })

    return rows, all_stat_ids_seen


def create_live_odds_table(conn: sqlite3.Connection):
    """
    Creates the live_odds table in winweave.db if it doesn't exist.
    This table stores every snapshot of odds we pull — we never
    overwrite, only append. This lets us track line movement over time.

    Also handles migrating an existing table (created before MLB
    support existed) by adding the 'league' column if it's missing,
    without touching any existing rows.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_odds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL,
            starts_at   TEXT,
            home_team   TEXT,
            away_team   TEXT,
            player_id   TEXT NOT NULL,
            stat_id     TEXT NOT NULL,
            side        TEXT NOT NULL,
            book        TEXT NOT NULL,
            line        REAL NOT NULL,
            odds        INTEGER NOT NULL,
            fetched_at  TEXT NOT NULL
        )
    """)

    existing_cols = {r[1] for r in
        conn.execute("PRAGMA table_info(live_odds)").fetchall()}
    if "league" not in existing_cols:
        conn.execute(
            "ALTER TABLE live_odds ADD COLUMN league TEXT DEFAULT 'NFL'"
        )
    if "deeplink" not in existing_cols:
        conn.execute(
            "ALTER TABLE live_odds ADD COLUMN deeplink TEXT DEFAULT ''"
        )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_live_odds_player
        ON live_odds (player_id, stat_id)
    """)
    conn.commit()


def save_to_db(rows: list[dict]):
    """Writes parsed prop rows into the live_odds table."""
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Make sure winweave.db is in your ~/WinWeave/data/ folder.")
        sys.exit(1)

    # timeout=30 (vs. the 5s default): if another process briefly holds
    # a write lock (e.g. a previous run that got force-killed mid-write
    # and left something uncommitted), wait and retry instead of
    # failing fast. WAL mode also makes concurrent access far more
    # forgiving in general -- worth having regardless of what actually
    # caused any specific hang.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    create_live_odds_table(conn)

    conn.executemany("""
        INSERT INTO live_odds
            (event_id, starts_at, home_team, away_team,
             player_id, stat_id, side, book, line, odds, deeplink,
             fetched_at, league)
        VALUES
            (:event_id, :starts_at, :home_team, :away_team,
             :player_id, :stat_id, :side, :book, :line, :odds, :deeplink,
             :fetched_at, :league)
    """, rows)

    conn.commit()
    conn.close()


def print_diagnostic(all_stat_ids_seen: set, league: str):
    """
    When zero rows match our target stat list but SGO returned real
    odds data, show what stat IDs actually exist so we can fix the
    guess list with real data instead of guessing again.
    """
    if not all_stat_ids_seen:
        return
    known_raw_names = set(LEAGUE_CONFIG[league]["stat_map"].keys())
    unmatched = all_stat_ids_seen - known_raw_names
    if unmatched:
        print(f"\n  DIAGNOSTIC — stat IDs found that we don't currently "
              f"track for {league.upper()}:")
        for s in sorted(unmatched)[:30]:
            print(f"    {s}")
        print(f"\n  If real prop stats are in this list, share it back "
              f"so the TARGET_STATS list can be corrected.")


def print_summary(rows: list[dict], league: str):
    """Prints a clean summary of what was pulled."""
    if not rows:
        print(f"\nNo {league.upper()} player prop lines found.")
        if league == "nfl":
            print("This is normal during the NFL offseason — no games "
                  "scheduled yet.")
        return

    games: dict[str, set] = {}
    books_seen: set = set()
    stats_seen: set = set()

    for r in rows:
        key = f"{r['away_team']} @ {r['home_team']} ({r['starts_at'][:10]})"
        games.setdefault(key, set()).add(r["player_id"])
        books_seen.add(r["book"])
        stats_seen.add(r["stat_id"])

    print(f"\n{'='*55}")
    print(f"  WinWeave — {league.upper()} Live Odds Pull Complete")
    print(f"{'='*55}")
    print(f"  Total prop lines saved : {len(rows):,}")
    print(f"  Games covered          : {len(games)}")
    print(f"  Books                  : {', '.join(sorted(books_seen))}")
    print(f"  Prop types             : {', '.join(sorted(stats_seen))}")
    print(f"\n  Games:")
    for game, players in sorted(games.items()):
        print(f"    {game} — {len(players)} players")
    print(f"{'='*55}")
    print(f"  Data saved to: {DB_PATH}")
    print(f"{'='*55}\n")


def run_scrape(league: str, books: list[str] = None,
               progress_callback=None) -> dict:
    """
    Core scraping pipeline, extracted so both the CLI (main, below)
    and the dashboard's Refresh Odds button can share the exact same
    logic instead of the dashboard shelling out to a subprocess or
    duplicating this code.

    progress_callback: optional function(str) called at each real
    stage boundary. A real pull can genuinely take 30-90+ seconds for
    a busy day (50 events × 4 books can mean many thousands of prop
    rows to parse in pure Python) -- without any feedback during that
    stretch, "slow but working" and "actually frozen" look identical
    to whoever's watching. This makes the difference visible.

    Returns a structured summary dict instead of printing, so a
    caller like Streamlit can render it however fits its UI:
        {
            "success": bool, "error": str | None,
            "league": str, "books": list[str],
            "n_events": int, "n_rows": int,
            "usage_remaining": int | None, "usage_max": int | None,
            "games": list[str],  # human-readable game descriptions
        }
    """
    def report(msg):
        if progress_callback:
            progress_callback(msg)

    books = books if books is not None else TARGET_BOOKS
    invalid = [b for b in books if b not in TARGET_BOOKS]
    if invalid:
        return {"success": False,
                "error": f"Unknown book(s) {invalid} -- valid options "
                         f"are {TARGET_BOOKS}",
                "league": league, "books": books, "n_events": 0,
                "n_rows": 0, "usage_remaining": None, "usage_max": None,
                "games": []}

    report("Checking API usage...")
    api_key = load_api_key()
    usage = get_usage_info(api_key)

    report(f"Fetching upcoming {league.upper()} events...")
    events = fetch_events(api_key, league)
    if not events:
        msg = f"No {league.upper()} events found."
        if league == "nfl":
            msg += " Preseason starts in August — try again then."
        return {"success": True, "error": msg, "league": league,
                "books": books, "n_events": 0, "n_rows": 0,
                "usage_remaining": usage.get("remaining"),
                "usage_max": usage.get("max"), "games": []}

    report(f"Found {len(events)} events — parsing prop lines "
           f"(this is the slow part on a busy day, can take a "
           f"minute or so)...")
    rows, all_stat_ids_seen = parse_player_props(events, league, books=books)

    game_rows, _ = parse_game_markets(events, league, books=books)
    report(f"Parsed {len(rows):,} prop lines + {len(game_rows):,} "
           f"game-market lines — saving to database...")
    save_to_db(rows)
    save_game_odds(game_rows)

    if league == "mlb":
        report("Resolving team names...")
        # Late import avoids a circular dependency at module load time
        # (scan_live_mlb_props doesn't import sgo_scraper, so this is
        # safe, but importing lazily here keeps sgo_scraper usable
        # standalone without pulling in the whole scan module).
        from scan_live_mlb_props import resolve_team
        games = sorted({
            f"{resolve_team(r['away_team'])} @ {resolve_team(r['home_team'])} "
            f"({r['starts_at'][:10]})"
            for r in rows
        })
    else:
        games = sorted({f"{r['away_team']} @ {r['home_team']} "
                        f"({r['starts_at'][:10]})" for r in rows})

    return {
        "success": True, "error": None, "league": league, "books": books,
        "n_events": len(events), "n_rows": len(rows),
        "n_game_rows": len(game_rows),
        "usage_remaining": usage.get("remaining"), "usage_max": usage.get("max"),
        "games": games,
    }


def main():
    parser = argparse.ArgumentParser(description="WinWeave live odds scraper")
    parser.add_argument("--league", choices=["nfl", "mlb"], default="nfl",
                        help="Which league to pull odds for (default: nfl)")
    parser.add_argument("--books", type=str, default=None,
                        help="Comma-separated list of books to scan, e.g. "
                             "'fanduel' or 'fanduel,draftkings'. Default: "
                             f"all of {TARGET_BOOKS}. Useful when you only "
                             "have an account at one sportsbook.")
    args = parser.parse_args()
    league = args.league
    books = ([b.strip() for b in args.books.split(",")]
            if args.books else TARGET_BOOKS)
    invalid = [b for b in books if b not in TARGET_BOOKS]
    if invalid:
        print(f"Unknown book(s) {invalid} -- valid options are {TARGET_BOOKS}")
        return

    print("\n" + "="*55)
    print(f"  WinWeave — SGO Scraper ({league.upper()})")
    if books != TARGET_BOOKS:
        print(f"  Scanning only: {', '.join(books)}")
    print("="*55 + "\n")

    api_key = load_api_key()
    check_usage(api_key)

    events = fetch_events(api_key, league)

    if not events:
        print(f"No {league.upper()} events found.")
        if league == "nfl":
            print("Preseason starts in August — run this again then.")
        return

    print("Parsing player prop lines...")
    rows, all_stat_ids_seen = parse_player_props(events, league, books=books)
    print(f"  Extracted {len(rows):,} prop lines across "
          f"{len(books)} book(s).")

    print("Parsing game-level markets (moneyline / run line / total)...")
    game_rows, bet_types_seen = parse_game_markets(events, league, books=books)
    print(f"  Extracted {len(game_rows):,} game-market lines "
          f"(zero extra API cost — same events payload).")
    if not game_rows and bet_types_seen:
        print("  DIAGNOSTIC — game-level oddID patterns actually seen "
              "(stat-entity-betType):")
        for b in sorted(bet_types_seen)[:20]:
            print(f"    {b}")
        print("  Share this back so the game-market parser can be "
              "corrected with real data.")

    if not rows:
        print_diagnostic(all_stat_ids_seen, league)

    print("Saving to winweave.db...")
    save_to_db(rows)
    save_game_odds(game_rows)

    print_summary(rows, league)


if __name__ == "__main__":
    main()
