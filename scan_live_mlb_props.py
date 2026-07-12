"""
scan_live_mlb_props.py — Closes the loop: real live odds -> EV engine.

Reads whatever's in live_odds (league='MLB'), matches SGO's player-ID
format against your real mlb_players table, resolves the opponent
team, pairs up over/under lines, and runs every one through
analyze_mlb_prop() automatically. Prints results sorted by EV%.

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    python scrapers/sgo_scraper.py --league mlb   # pull fresh odds first
    python scan_live_mlb_props.py

TWO BRIDGING PROBLEMS THIS SOLVES (and their honest limitations):

1. SGO's player_id looks like "RAFAEL_DEVERS_1_MLB" — not the real
   name your database uses ("Rafael Devers"). This script converts
   the ID into a name guess, then normalizes both sides (stripping
   accents, periods, apostrophes, hyphens) before comparing — this
   fixes cosmetic mismatches like "Andrés Giménez" vs "Andres
   Gimenez" or "Logan O'Hoppe" vs "Logan Ohoppe" automatically.
   It CANNOT fix genuine nickname-vs-legal-name mismatches — SGO's
   ID for Jazz Chisholm Jr. uses his legal first name ("Jasrado"),
   a different word entirely, not just different formatting. Cases
   like that are reported as unmatched rather than silently guessed.

2. The odds row only tells you the two teams playing, as SGO's own
   team-name format (e.g. "LOS_ANGELES_DODGERS_MLB"), not which
   team the specific player is on. This script parses that format
   into your database's team-name spelling to resolve it. Two teams
   (St. Louis, and the Athletics during their relocation) needed
   manual special-casing; everything else parses generically.

Any player or team that can't be resolved is reported clearly at
the end rather than silently dropped, the same pattern used
throughout this project — so we can fix gaps with real data
instead of guessing twice.
"""

import sys
import sqlite3
import unicodedata
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.mlb_analyzer import analyze_mlb_prop, MLB_STATS

DB_PATH = Path(__file__).resolve().parent / "data" / "winweave.db"

# CORRECTED based on a real diagnostic pull (2026-07-04). SGO's MLB
# team IDs are NOT abbreviations (that was wrong) — they're full team
# names in SCREAMING_SNAKE_CASE with a trailing _MLB, e.g.
# "LOS_ANGELES_DODGERS_MLB" -> "Los Angeles Dodgers". Simple parsing
# handles 28 of 30 teams; two need special-casing.
SGO_TEAM_NAME_FIXES = {
    "STLOUIS_CARDINALS_MLB": "St. Louis Cardinals",
    "OAKLAND_ATHLETICS_MLB": "Athletics",  # MLB's API currently lists
                                            # them with no city name
                                            # during their relocation
}


def resolve_team(sgo_team_id: str) -> str:
    """
    Converts SGO's full-name team ID format into the team name your
    mlb_players.team column uses (sourced from MLB's own Stats API).
    """
    key = sgo_team_id.strip().upper()
    if key in SGO_TEAM_NAME_FIXES:
        return SGO_TEAM_NAME_FIXES[key]

    name = key[:-4] if key.endswith("_MLB") else key
    return " ".join(w.capitalize() for w in name.split("_"))


def guess_player_name(sgo_player_id: str) -> str:
    """
    Converts SGO's player_id format into a display-name guess.
    "RAFAEL_DEVERS_1_MLB" -> "Rafael Devers"

    SGO's format is FIRSTNAME_LASTNAME_NUMBER_LEAGUE. We strip the
    trailing _<number>_<LEAGUE> and title-case what's left.
    """
    parts = sgo_player_id.split("_")
    if len(parts) >= 3 and parts[-2].isdigit():
        parts = parts[:-2]
    elif len(parts) >= 2 and parts[-1].isupper() and len(parts[-1]) <= 4:
        parts = parts[:-1]
    return " ".join(p.capitalize() for p in parts)


def normalize_for_matching(s: str) -> str:
    """
    Strips accents, punctuation, and spacing so names that differ
    only cosmetically all normalize to the same comparable form:
      "Andrés Giménez"  / "Andres Gimenez"   -> ANDRESGIMENEZ
      "Bobby Witt Jr."  / "Bobby Witt Jr"    -> BOBBYWITTJR
      "Logan O'Hoppe"   / "Logan Ohoppe"     -> LOGANOHOPPE
      "Pete Crow-Armstrong" / "Pete Crowarmstrong" -> PETECROWARMSTRONG

    This does NOT fix genuine nickname-vs-legal-name mismatches —
    e.g. SGO's "JASRADO_CHISHOLM" vs. MLB's official "Jazz Chisholm
    Jr." are different words, not just different formatting, so no
    amount of normalization bridges that. Those get reported as
    unmatched rather than silently guessed at.
    """
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in ascii_only if c.isalnum()).upper()


def build_player_lookup(conn: sqlite3.Connection) -> dict:
    """
    One-time cache: normalized name -> real full_name, built from
    every player in mlb_players. O(1) lookups after this instead of
    a fresh table scan per player.

    KNOWN LIMITATION: if two different players normalize to the same
    key (e.g. two real "Luis Garcia"s currently in MLB), this keeps
    whichever one was read last — a genuine ambiguity this simple
    approach doesn't resolve. Rare enough to accept for now.
    """
    lookup = {}
    for (full_name,) in conn.execute("SELECT full_name FROM mlb_players"):
        lookup[normalize_for_matching(full_name)] = full_name
    return lookup


def find_matching_player(lookup: dict, guess: str) -> str:
    """Looks up a guessed name against the pre-built normalized cache."""
    return lookup.get(normalize_for_matching(guess), "")


def get_player_team(conn: sqlite3.Connection, full_name: str) -> str:
    row = conn.execute(
        "SELECT team FROM mlb_players WHERE full_name = ? LIMIT 1",
        (full_name,)
    ).fetchone()
    return row[0] if row else ""


def infer_side_from_history(conn: sqlite3.Connection, full_name: str,
                            home_full: str, away_full: str):
    """
    Fallback for when mlb_players.team is empty (a real gap seen in
    live data — MLB's bulk player-directory endpoint doesn't reliably
    populate every player's current team the way an individual lookup
    would). Uses data we already trust instead: if this player has
    ANY game in their history where they faced one of tonight's two
    teams, they must belong to the OTHER one.

    Returns (is_home, opponent) or None if no history helps.
    """
    for table in ("mlb_batting", "mlb_pitching"):
        row = conn.execute(f"""
            SELECT opponent, COUNT(*) as n FROM {table}
            WHERE full_name = ? AND opponent IN (?, ?)
            GROUP BY opponent ORDER BY n DESC LIMIT 1
        """, (full_name, home_full, away_full)).fetchone()
        if row:
            most_common_opponent = row[0]
            if most_common_opponent == home_full:
                return False, home_full   # they faced the home team -> they're away
            else:
                return True, away_full    # they faced the away team -> they're home
    return None


def fetch_latest_mlb_odds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Gets the most recent scrape's worth of MLB odds rows."""
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        "SELECT MAX(fetched_at) FROM live_odds WHERE league = 'MLB'"
    ).fetchone()[0]
    if not latest:
        return []
    return conn.execute("""
        SELECT * FROM live_odds
        WHERE league = 'MLB' AND fetched_at = ?
    """, (latest,)).fetchall()


def pair_over_under(rows: list[sqlite3.Row]) -> dict:
    """
    Groups odds rows by (event_id, player_id, stat_id, book, line) so
    we have both the over AND under odds for the same market — needed
    to remove the vig properly in analyze_mlb_prop.

    CRITICAL: event_id is part of the key. A real diagnostic pull
    showed the exact same player/stat/book/line combination appearing
    under TWO DIFFERENT event_ids — the data source apparently lists
    the same real-world game as more than one "event" in some cases.
    Without event_id in the grouping key, over/under prices from two
    unrelated event listings could get silently paired together,
    producing wildly wrong "true probability vs. odds" mismatches
    that looked like huge +EV finds but weren't real. Scoping by
    event_id means each event's own odds board stays internally
    consistent, even if we don't know why duplicates exist upstream.
    """
    grouped = defaultdict(dict)
    for r in rows:
        key = (r["event_id"], r["player_id"], r["stat_id"], r["book"], r["line"])
        deeplink = r["deeplink"] if "deeplink" in r.keys() else ""
        grouped[key][r["side"]] = {"odds": r["odds"], "deeplink": deeplink}
    return grouped


def format_game_time(starts_at: str) -> str:
    """
    Converts a UTC ISO timestamp (as stored by the scraper) into a
    readable date/time in Central Time — this matters because a scan
    can easily mix props from a game already in progress tonight with
    props for a game tomorrow or even weeks out, and without a date
    shown there's no way to tell them apart at a glance.
    """
    if not starts_at:
        return "time unknown"
    try:
        dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        return dt_local.strftime("%b %d, %I:%M %p CT")
    except (ValueError, TypeError):
        return starts_at[:10] if len(starts_at) >= 10 else "time unknown"


def local_date(starts_at: str) -> str:
    """
    Returns just the YYYY-MM-DD portion of a game's start time,
    converted to Central Time — NOT the raw UTC date. This matters:
    a late-night US game can cross midnight UTC while still being
    "tonight" in Central Time. Using this consistently for both the
    date listing and the --date filter means what you type matches
    what you'd naturally expect, instead of needing to think in UTC.
    """
    if not starts_at:
        return ""
    try:
        dt_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("America/Chicago"))
        return dt_local.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return starts_at[:10] if len(starts_at) >= 10 else ""


def get_matchup(conn: sqlite3.Connection, event_id: str) -> str:
    """Human-readable 'Away @ Home' string for a given event, resolved
    into the same team-name spelling your database uses."""
    row = conn.execute("""
        SELECT home_team, away_team FROM live_odds
        WHERE event_id = ? LIMIT 1
    """, (event_id,)).fetchone()
    if not row:
        return "Unknown matchup"
    home_full = resolve_team(row["home_team"])
    away_full = resolve_team(row["away_team"])
    return f"{away_full} @ {home_full}"


def scan_mlb_props(date_filter: str = None) -> tuple[list, dict]:
    """
    Core scanning pipeline, extracted so both the CLI (below) and the
    dashboard's Top Picks tab can share the exact same battle-tested
    logic instead of two copies drifting apart over time.

    Returns (results, diagnostics) where results is a list of
    MLBPropAnalysis objects (each with .matchup and .deeplink attached)
    and diagnostics is a dict with unmatched_players, unmatched_teams,
    single_sided_skipped, and available_dates for the caller to report
    however fits its context (printed CLI table vs. Streamlit UI).
    """
    diagnostics = {
        "unmatched_players": set(),
        "unmatched_teams": set(),
        "single_sided_skipped": 0,
        "available_dates": [],
        "error": None,
    }

    if not DB_PATH.exists():
        diagnostics["error"] = f"Database not found at {DB_PATH}"
        return [], diagnostics

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "live_odds" not in tables:
        diagnostics["error"] = "No live_odds table yet. Run the scraper first."
        conn.close()
        return [], diagnostics

    rows = fetch_latest_mlb_odds(conn)
    if not rows:
        diagnostics["error"] = "No MLB odds found. Run the scraper first."
        conn.close()
        return [], diagnostics

    diagnostics["available_dates"] = sorted(
        {local_date(r["starts_at"]) for r in rows if r["starts_at"]}
    )

    if date_filter:
        rows = [r for r in rows if local_date(r["starts_at"]) == date_filter]
        if not rows:
            diagnostics["error"] = f"No games found on {date_filter} (Central Time)."
            conn.close()
            return [], diagnostics

    grouped = pair_over_under(rows)
    player_lookup = build_player_lookup(conn)

    results = []
    for (event_id, sgo_player_id, stat, book, line), sides in grouped.items():
        if "over" not in sides or "under" not in sides:
            diagnostics["single_sided_skipped"] += 1
            continue

        guess = guess_player_name(sgo_player_id)
        real_name = find_matching_player(player_lookup, guess)
        if not real_name:
            diagnostics["unmatched_players"].add(f"{sgo_player_id} (guessed: {guess})")
            continue

        sample_row = next((r for r in rows
                           if r["player_id"] == sgo_player_id
                           and r["stat_id"] == stat
                           and r["event_id"] == event_id), None)
        if sample_row is None:
            continue
        home_full = resolve_team(sample_row["home_team"])
        away_full = resolve_team(sample_row["away_team"])
        if not home_full or not away_full:
            diagnostics["unmatched_teams"].add(
                f"{sample_row['home_team']} / {sample_row['away_team']}")
            continue

        player_team = get_player_team(conn, real_name)
        if player_team == home_full:
            is_home, opponent = True, away_full
        elif player_team == away_full:
            is_home, opponent = False, home_full
        else:
            inferred = infer_side_from_history(conn, real_name, home_full, away_full)
            if inferred is None:
                diagnostics["unmatched_teams"].add(
                    f"{real_name}'s team '{player_team}' not in "
                    f"{{{home_full}, {away_full}}} (no game history to infer from either)")
                continue
            is_home, opponent = inferred

        game_when = format_game_time(sample_row["starts_at"])
        matchup = f"{away_full} @ {home_full} ({game_when})"

        # Evaluate BOTH sides and keep whichever the market is
        # actually mispricing, instead of always defaulting to over.
        #
        # FIXED (2026-07-09): this used to call analyze_mlb_prop with
        # side="over" hardcoded -- meaning "under" was never even
        # considered, not "correctly ruled out". A real user noticed
        # every single recommended pick across days of use was an
        # over, and asked whether the model was structurally
        # incapable of finding unders, or just genuinely concluding
        # overs were always better. It was the former: this hardcoded
        # side made "only overs" true by construction, regardless of
        # what the underlying data actually supported. Now both sides
        # get a real, independent probability estimate (the side
        # parameter flows through every signal -- hit rate, the
        # Poisson/Normal model, opponent factor, etc. -- not just a
        # final inversion), and whichever side has the higher EV% is
        # the one surfaced.
        candidates = []
        for side in ("over", "under"):
            try:
                r = analyze_mlb_prop(
                    player_name=real_name, stat=stat, line=line,
                    over_odds=sides["over"]["odds"],
                    under_odds=sides["under"]["odds"],
                    opponent=opponent, side=side, book=book,
                    is_home=is_home,
                )
                candidates.append(r)
            except ValueError:
                continue
        if not candidates:
            continue
        r = max(candidates, key=lambda c: c.ev_percent)
        r.matchup = matchup
        r.starts_at = sample_row["starts_at"]  # raw ISO timestamp -- lets
                                                # the Tracker store a real
                                                # game_date instead of none
        r.deeplink = sides["over"]["deeplink"] or sides["under"]["deeplink"]
        results.append(r)

    conn.close()
    return results, diagnostics


def main():
    import argparse
    parser = argparse.ArgumentParser(description="WinWeave live MLB prop scanner")
    parser.add_argument("--date", type=str, default=None,
                        help="Only show games on this date (YYYY-MM-DD, "
                             "Central Time). Default: show all dates found.")
    args = parser.parse_args()

    results, diag = scan_mlb_props(date_filter=args.date)

    if diag["error"]:
        print(f"\n{diag['error']}\n")
        return

    if diag["available_dates"]:
        print(f"\nGame dates in this pull (Central Time): "
              f"{', '.join(diag['available_dates'])}")

    if not results:
        print("No props could be fully analyzed.")
    else:
        results.sort(key=lambda r: r.ev_percent, reverse=True)
        print(f"{'='*110}")
        print(f"  LIVE MLB PROPS — {len(results)} ANALYZED, SORTED BY EV%")
        print(f"{'='*110}")
        print(f"  {'Player':<20} {'Stat':<18} {'Line':<6} {'Odds':<7} "
              f"{'Book':<12} {'True%':<7} {'EV%':<9} {'Grade':<28} {'Matchup'}")
        print(f"  {'-'*106}")
        for r in results:
            sign = "+" if r.ev_percent >= 0 else ""
            print(f"  {r.player_name:<20} {r.stat:<18} {r.line:<6} "
                  f"{r.american_odds:<+7} {r.book:<12} {r.true_probability:<7.1%} "
                  f"{sign}{r.ev_percent:<8.2f} {r.grade():<28} {r.matchup}")
        print(f"{'='*110}\n")

    if diag["unmatched_players"]:
        print(f"Could not match {len(diag['unmatched_players'])} player name(s) "
              f"to your database:")
        for p in sorted(diag["unmatched_players"]):
            print(f"  {p}")
        print()

    if diag["unmatched_teams"]:
        print(f"Could not resolve {len(diag['unmatched_teams'])} team code/roster "
              f"issue(s) — shown below in FULL, since a truncated list "
              f"here can hide entire games' worth of missing props:")
        for t in sorted(diag["unmatched_teams"]):
            print(f"  {t}")
        print()

    if diag["single_sided_skipped"]:
        print(f"Skipped {diag['single_sided_skipped']} market(s) that only had "
              f"one side (over or under, not both) — can't remove vig "
              f"without both.\n")


if __name__ == "__main__":
    main()
