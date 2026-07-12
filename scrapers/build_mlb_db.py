"""
build_mlb_db.py — Builds MLB player game-log tables in winweave.db

Data source: the official MLB Stats API (statsapi.mlb.com).
It is free, public, and requires NO API key — this is the same
feed MLB.com itself uses. No scraping, no ToS problems.

Creates three tables inside your existing winweave.db:
  mlb_players   — player directory (id, name, position, team)
  mlb_batting   — per-game batting logs (hits, total bases, HRs...)
  mlb_pitching  — per-game pitching logs (strikeouts, outs, ER...)

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python scrapers/build_mlb_db.py                  # current season
    python scrapers/build_mlb_db.py --seasons 2025 2026

First run takes roughly 15-30 minutes (one API call per player,
with a polite delay so we're a good citizen). Re-running is safe:
it wipes and rebuilds the MLB tables only — your NFL tables are
never touched.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

DB_PATH  = Path(__file__).resolve().parent.parent / "data" / "winweave.db"
API_BASE = "https://statsapi.mlb.com/api/v1"
DELAY    = 0.4   # seconds between API calls — be polite


def api_get(path: str) -> dict:
    """GET a JSON payload from the MLB Stats API with basic retry."""
    url = f"{API_BASE}{path}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if attempt == 2:
                print(f"    ! failed: {path} ({e})")
                return {}
            time.sleep(2 * (attempt + 1))
    return {}


def create_tables(conn: sqlite3.Connection):
    conn.execute("DROP TABLE IF EXISTS mlb_players")
    conn.execute("DROP TABLE IF EXISTS mlb_batting")
    conn.execute("DROP TABLE IF EXISTS mlb_pitching")

    conn.execute("""
        CREATE TABLE mlb_players (
            player_id   INTEGER PRIMARY KEY,
            full_name   TEXT NOT NULL,
            position    TEXT,
            team        TEXT,
            team_id     INTEGER
        )""")
    conn.execute("""
        CREATE TABLE mlb_batting (
            player_id    INTEGER,
            full_name    TEXT,
            season       INTEGER,
            game_date    TEXT,
            opponent     TEXT,
            is_home      INTEGER,
            at_bats      INTEGER,
            hits         INTEGER,
            doubles      INTEGER,
            triples      INTEGER,
            home_runs    INTEGER,
            total_bases  INTEGER,
            rbi          INTEGER,
            runs         INTEGER,
            walks        INTEGER,
            strikeouts   INTEGER,
            stolen_bases INTEGER,
            vs_starter_id   INTEGER,
            vs_starter_name TEXT
        )""")
    conn.execute("""
        CREATE TABLE mlb_pitching (
            player_id     INTEGER,
            full_name     TEXT,
            season        INTEGER,
            game_date     TEXT,
            opponent      TEXT,
            is_home       INTEGER,
            outs_recorded INTEGER,
            strikeouts    INTEGER,
            hits_allowed  INTEGER,
            earned_runs   INTEGER,
            walks_allowed INTEGER,
            batters_faced INTEGER,
            games_started INTEGER
        )""")
    conn.execute("CREATE INDEX idx_mlb_bat_player  ON mlb_batting  (full_name, season)")
    conn.execute("CREATE INDEX idx_mlb_pit_player  ON mlb_pitching (full_name, season)")
    conn.execute("CREATE INDEX idx_mlb_bat_opp     ON mlb_batting  (opponent, season)")
    conn.execute("CREATE INDEX idx_mlb_pit_opp     ON mlb_pitching (opponent, season)")
    conn.execute("CREATE INDEX idx_mlb_bat_starter ON mlb_batting  (full_name, vs_starter_name)")
    conn.commit()


def fetch_season_players(season: int) -> list[dict]:
    """All MLB players rostered in a season."""
    print(f"  Fetching player directory for {season}...")
    data = api_get(f"/sports/1/players?season={season}")
    players = data.get("people", [])
    print(f"    {len(players)} players found.")
    return players


def fetch_all_teams(season: int) -> list[dict]:
    """List of all 30 MLB teams for a season."""
    data = api_get(f"/teams?sportId=1&season={season}")
    return data.get("teams", [])


def fetch_team_roster(team_id: int, season: int) -> list[dict]:
    """Confirmed roster for one specific team."""
    data = api_get(f"/teams/{team_id}/roster?season={season}")
    return data.get("roster", [])


def build_team_map(season: int) -> dict:
    """
    Returns {player_id: team_name}, built by walking every team's own
    roster endpoint individually.

    WHY THIS EXISTS: a real run showed 73+ players (entire games'
    worth) with an empty team field, even for obvious everyday
    starters like Bryce Harper and Bobby Witt Jr. The bulk player
    directory (/sports/1/players) apparently doesn't reliably
    populate currentTeam for every player — bench and depth players
    seem to be affected most. Each team's own /roster endpoint is
    the source of truth for "who is actually on this team right now"
    and reliably covers every rostered player, so this is used to
    fill in the gaps the bulk directory leaves behind.
    """
    print(f"  Fetching confirmed team rosters for {season} "
          f"(more reliable than the bulk player directory)...")
    teams = fetch_all_teams(season)
    team_map = {}
    for i, team in enumerate(teams, 1):
        team_id = team.get("id")
        team_name = team.get("name", "")
        if not team_id:
            continue
        roster = fetch_team_roster(team_id, season)
        for entry in roster:
            person = entry.get("person", {})
            pid = person.get("id")
            if pid:
                team_map[pid] = team_name
        time.sleep(DELAY)
        if i % 10 == 0:
            print(f"    {i}/{len(teams)} team rosters fetched...")
    print(f"    Built reliable team assignments for {len(team_map)} "
          f"players across {len(teams)} teams.")
    return team_map


def ip_to_outs(ip_str) -> int:
    """MLB innings-pitched strings like '6.2' mean 6 innings + 2 outs."""
    try:
        s = str(ip_str)
        whole, _, frac = s.partition(".")
        return int(whole) * 3 + (int(frac) if frac else 0)
    except (ValueError, TypeError):
        return 0


def fetch_game_logs(player_id: int, season: int, group: str) -> list[dict]:
    """Per-game logs for one player. group = 'hitting' or 'pitching'."""
    data = api_get(f"/people/{player_id}/stats?stats=gameLog"
                   f"&season={season}&group={group}")
    for block in data.get("stats", []):
        return block.get("splits", [])
    return []


def fetch_schedule_probables(season: int) -> dict:
    """
    Pulls the full season schedule with announced probable starting
    pitchers, in one API call. Returns a dict keyed by
    (game_date, home_team_name, away_team_name) so individual batting
    rows (which already know date/opponent/is_home) can look up who
    they were facing.

    CAVEAT: "probable" pitcher is what MLB announces before the game.
    It's usually who actually starts, but a small fraction of games
    see a late scratch/replacement that this won't catch. Good enough
    as the basis for a batter-vs-starter signal without needing a
    separate API call per individual game (which would be far slower).
    """
    print(f"  Fetching {season} schedule with probable pitchers...")
    data = api_get(f"/schedule?sportId=1&season={season}&gameType=R"
                   f"&hydrate=probablePitcher")
    lookup = {}
    for date_block in data.get("dates", []):
        game_date = date_block.get("date", "")
        for game in date_block.get("games", []):
            teams = game.get("teams", {})
            home  = teams.get("home", {})
            away  = teams.get("away", {})
            home_team = (home.get("team") or {}).get("name", "")
            away_team = (away.get("team") or {}).get("name", "")
            home_prob = home.get("probablePitcher") or {}
            away_prob = away.get("probablePitcher") or {}
            lookup[(game_date, home_team, away_team)] = {
                "home_probable_id":   home_prob.get("id"),
                "home_probable_name": home_prob.get("fullName", ""),
                "away_probable_id":   away_prob.get("id"),
                "away_probable_name": away_prob.get("fullName", ""),
            }
    print(f"    {len(lookup)} scheduled games found.")
    return lookup


def get_opposing_starter(probables: dict, game_date: str, team: str,
                         opponent: str, is_home: bool):
    """
    Given a batting row's own context, returns (starter_id, starter_name)
    for whoever was announced as the OPPOSING starting pitcher.

    If the batter's team is home, they face the away team's starter.
    If the batter's team is away, they face the home team's starter.
    """
    if is_home:
        key, side = (game_date, team, opponent), "away_probable"
    else:
        key, side = (game_date, opponent, team), "home_probable"

    entry = probables.get(key)
    if not entry:
        return None, ""
    return entry.get(f"{side}_id"), entry.get(f"{side}_name", "")



def build(seasons: list[int]):
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Your NFL database must exist first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)

    bat_rows, pit_rows, player_rows = [], [], []
    seen_players = set()

    for season in seasons:
        probables = fetch_schedule_probables(season)
        team_map = build_team_map(season)
        players = fetch_season_players(season)
        n = len(players)

        for i, p in enumerate(players, 1):
            pid   = p.get("id")
            name  = p.get("fullName", "")
            pos   = (p.get("primaryPosition") or {}).get("abbreviation", "")
            # Prefer the reliable per-team roster lookup; fall back to
            # the bulk directory's currentTeam only if a player somehow
            # isn't on any team's roster (e.g. free agent, retired mid-list)
            team  = team_map.get(pid) or (p.get("currentTeam") or {}).get("name", "")
            tid   = (p.get("currentTeam") or {}).get("id")

            if pid and pid not in seen_players:
                player_rows.append((pid, name, pos, team, tid))
                seen_players.add(pid)

            if i % 100 == 0:
                print(f"    {season}: {i}/{n} players processed "
                      f"({len(bat_rows)} bat rows, {len(pit_rows)} pit rows)")

            is_pitcher = pos == "P"
            groups = ["pitching"] if is_pitcher else ["hitting"]
            # Two-way players (e.g. Ohtani-types) get both
            if pos in ("TWP",):
                groups = ["hitting", "pitching"]

            for group in groups:
                splits = fetch_game_logs(pid, season, group)
                time.sleep(DELAY)

                for s in splits:
                    stat = s.get("stat", {})
                    opp  = (s.get("opponent") or {}).get("name", "")
                    home = 1 if s.get("isHome") else 0
                    gdate = s.get("date", "")

                    if group == "hitting":
                        hits    = int(stat.get("hits", 0) or 0)
                        doubles = int(stat.get("doubles", 0) or 0)
                        triples = int(stat.get("triples", 0) or 0)
                        hrs     = int(stat.get("homeRuns", 0) or 0)
                        singles = hits - doubles - triples - hrs
                        tb      = singles + 2*doubles + 3*triples + 4*hrs
                        starter_id, starter_name = get_opposing_starter(
                            probables, gdate, team, opp, bool(home))
                        bat_rows.append((
                            pid, name, season, gdate, opp, home,
                            int(stat.get("atBats", 0) or 0), hits,
                            doubles, triples, hrs, tb,
                            int(stat.get("rbi", 0) or 0),
                            int(stat.get("runs", 0) or 0),
                            int(stat.get("baseOnBalls", 0) or 0),
                            int(stat.get("strikeOuts", 0) or 0),
                            int(stat.get("stolenBases", 0) or 0),
                            starter_id, starter_name,
                        ))
                    else:
                        pit_rows.append((
                            pid, name, season, gdate, opp, home,
                            ip_to_outs(stat.get("inningsPitched", "0")),
                            int(stat.get("strikeOuts", 0) or 0),
                            int(stat.get("hits", 0) or 0),
                            int(stat.get("earnedRuns", 0) or 0),
                            int(stat.get("baseOnBalls", 0) or 0),
                            int(stat.get("battersFaced", 0) or 0),
                            # games_started: 1 if this outing was a
                            # start, 0 if relief. This is THE column
                            # that lets the analyzer separate starter
                            # samples from bullpen work (the Quantrill
                            # 2026 swingman problem: 17 appearances,
                            # only 2 starts — a sample mean of ~2 IP
                            # while the books priced him as a starter).
                            int(stat.get("gamesStarted", 0) or 0),
                        ))

    print("\n  Writing to database...")
    conn.executemany("INSERT INTO mlb_players  VALUES (?,?,?,?,?)", player_rows)
    conn.executemany("INSERT INTO mlb_batting  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", bat_rows)
    conn.executemany("INSERT INTO mlb_pitching VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", pit_rows)
    conn.commit()

    print(f"\n  DONE.")
    print(f"    mlb_players : {len(player_rows):,} rows")
    print(f"    mlb_batting : {len(bat_rows):,} rows")
    print(f"    mlb_pitching: {len(pit_rows):,} rows")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="+", type=int,
                        default=[date.today().year],
                        help="Seasons to pull, e.g. --seasons 2025 2026")
    args = parser.parse_args()

    print("="*60)
    print("  WinWeave — MLB Database Builder")
    print(f"  Seasons: {args.seasons}")
    print("  Source: MLB Stats API (free, official, no key needed)")
    print("="*60)
    build(args.seasons)
