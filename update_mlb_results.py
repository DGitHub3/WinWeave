"""
update_mlb_results.py — targeted, fast MLB results refresh.

THE PROBLEM THIS SOLVES: auto-resolve can only grade games that
exist in mlb_batting / mlb_pitching, and the full rebuild
(build_mlb_db.py) takes 15-30 minutes — far too heavy to run just to
grade yesterday's picks. This script fetches game logs for ONLY the
players who currently have pending bets (one API call each, ~20-60
seconds total) and final scores for pending game-market bets straight
from the MLB schedule endpoint, then runs auto-resolve.

The dashboard's "Auto-resolve MLB bets" button now calls this
automatically, so the daily flow is just: click the button.

CLI: python update_mlb_results.py [--dry-run]
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH  = Path(__file__).resolve().parent / "data" / "winweave.db"
API_BASE = "https://statsapi.mlb.com/api/v1"
DELAY    = 0.25

GAME_STATS = {"moneyline", "run_line", "total_runs"}
PITCH_STATS = {"strikeouts_pitcher", "outs_recorded", "hits_allowed",
               "earned_runs", "walks_allowed"}
# stats that live in mlb_pitching regardless of naming nuances
PITCHING_TABLE_STATS = {"outs_recorded", "hits_allowed", "earned_runs",
                        "walks_allowed", "batters_faced", "strikeouts"}


def api_get(path: str) -> dict:
    url = f"{API_BASE}{path}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt == 2:
                return {}
            time.sleep(1.5 * (attempt + 1))
    return {}


def ip_to_outs(ip_str) -> int:
    try:
        whole, _, frac = str(ip_str).partition(".")
        return int(whole) * 3 + (int(frac) if frac else 0)
    except (ValueError, TypeError):
        return 0


def _pending(conn):
    return conn.execute("""
        SELECT id, player_name, stat, opponent, game_date, analyzed_at
        FROM prop_results WHERE result IS NULL
    """).fetchall()


def _is_pitching_stat(conn, stat: str) -> bool:
    if stat in ("strikeouts",):
        # strikeouts exists in both tables; pitcher props are the ones
        # WinWeave scans — but check the batting table too if needed.
        return True
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mlb_pitching)")}
    return stat in cols


def refresh_player_logs(conn, dry_run=False,
                        fetch=api_get) -> dict:
    """
    For each player with a pending (non-game) bet: fetch their current
    season game log and window-replace their rows from the earliest
    pending game date onward. Idempotent; doubleheader-safe (both
    games of a DH come back as separate splits and are re-inserted
    together).
    """
    out = {"players_updated": 0, "rows_inserted": 0, "not_found": []}
    pend = [b for b in _pending(conn) if b["stat"] not in GAME_STATS]
    if not pend:
        return out
    season = datetime.now().year

    # earliest date we need per player
    need: dict = {}
    for b in pend:
        d = (b["game_date"] or b["analyzed_at"] or "")[:10]
        if not d:
            d = (datetime.now() - timedelta(days=3)).date().isoformat()
        start = (datetime.fromisoformat(d) - timedelta(days=1)).date()
        cur = need.get(b["player_name"])
        need[b["player_name"]] = min(cur, start) if cur else start

    ids = {r["full_name"]: r["player_id"] for r in conn.execute(
        "SELECT full_name, player_id FROM mlb_players")}

    for player, start in need.items():
        pid = ids.get(player)
        if not pid:
            out["not_found"].append(player)
            continue
        pending_stats = {b["stat"] for b in pend
                         if b["player_name"] == player}
        groups = set()
        for s in pending_stats:
            groups.add("pitching" if _is_pitching_stat(conn, s)
                       and s in PITCHING_TABLE_STATS else "hitting")
            if s == "strikeouts":
                groups.add("pitching")

        inserted = 0
        for group in groups:
            data = fetch(f"/people/{pid}/stats?stats=gameLog"
                         f"&season={season}&group={group}")
            time.sleep(DELAY)
            splits = []
            for block in data.get("stats", []):
                splits = block.get("splits", [])
            rows = [s for s in splits
                    if (s.get("date") or "") >= start.isoformat()]
            if not rows:
                continue
            table = "mlb_pitching" if group == "pitching" else "mlb_batting"
            if not dry_run:
                conn.execute(f"""
                    DELETE FROM {table}
                    WHERE player_id = ? AND game_date >= ?
                """, (pid, start.isoformat()))
            for s in rows:
                stat = s.get("stat", {})
                opp  = (s.get("opponent") or {}).get("name", "")
                home = 1 if s.get("isHome") else 0
                gd   = s.get("date", "")
                if dry_run:
                    inserted += 1
                    continue
                if group == "hitting":
                    hits    = int(stat.get("hits", 0) or 0)
                    doubles = int(stat.get("doubles", 0) or 0)
                    triples = int(stat.get("triples", 0) or 0)
                    hrs     = int(stat.get("homeRuns", 0) or 0)
                    tb = (hits - doubles - triples - hrs) \
                        + 2*doubles + 3*triples + 4*hrs
                    conn.execute("""
                        INSERT INTO mlb_batting VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'')
                    """, (pid, player, season, gd, opp, home,
                          int(stat.get("atBats", 0) or 0), hits,
                          doubles, triples, hrs, tb,
                          int(stat.get("rbi", 0) or 0),
                          int(stat.get("runs", 0) or 0),
                          int(stat.get("baseOnBalls", 0) or 0),
                          int(stat.get("strikeOuts", 0) or 0),
                          int(stat.get("stolenBases", 0) or 0)))
                else:
                    pcols = {r[1] for r in conn.execute(
                        "PRAGMA table_info(mlb_pitching)")}
                    vals = [pid, player, season, gd, opp, home,
                            ip_to_outs(stat.get("inningsPitched", "0")),
                            int(stat.get("strikeOuts", 0) or 0),
                            int(stat.get("hits", 0) or 0),
                            int(stat.get("earnedRuns", 0) or 0),
                            int(stat.get("baseOnBalls", 0) or 0),
                            int(stat.get("battersFaced", 0) or 0)]
                    if "games_started" in pcols:
                        vals.append(int(stat.get("gamesStarted", 0) or 0))
                    ph = ",".join("?" * len(vals))
                    conn.execute(
                        f"INSERT INTO mlb_pitching VALUES ({ph})", vals)
                inserted += 1
        if inserted:
            out["players_updated"] += 1
            out["rows_inserted"] += inserted
    if not dry_run:
        conn.commit()
    return out


def resolve_game_bets_from_schedule(conn, dry_run=False,
                                    fetch=api_get) -> dict:
    """
    Pending game-market bets (moneyline / run_line / total_runs) are
    graded straight from the MLB schedule endpoint's final scores —
    no batting-table reconstruction needed. Doubleheaders resolve
    correctly here too, because each game carries its own score.
    ...unless BOTH games were against the same club and both final on
    the same date with no way to tell which was bet — then it stays
    pending (one-candidate rule, as everywhere else).
    """
    from src.factors.prop_tracker import log_result
    out = {"resolved": [], "skipped": []}
    pend = [b for b in _pending(conn) if b["stat"] in GAME_STATS]
    for b in pend:
        gdate = (b["game_date"] or b["analyzed_at"] or "")[:10]
        if not gdate:
            out["skipped"].append((b["id"], "no game date"))
            continue
        data = fetch(f"/schedule?sportId=1&date={gdate}")
        time.sleep(DELAY)
        finals = []
        for date_block in data.get("dates", []):
            for g in date_block.get("games", []):
                st_ = (g.get("status") or {}).get("abstractGameState", "")
                teams = g.get("teams", {})
                hname = ((teams.get("home") or {}).get("team") or {}).get("name", "")
                aname = ((teams.get("away") or {}).get("team") or {}).get("name", "")
                names = {hname, aname}
                if st_ == "Final" and b["player_name"] in names \
                        and (b["opponent"] or "") in names:
                    finals.append((hname,
                                   (teams["home"].get("score") or 0),
                                   aname,
                                   (teams["away"].get("score") or 0)))
        if len(finals) != 1:
            out["skipped"].append(
                (b["id"], "game not final yet" if not finals
                 else "doubleheader — resolve manually"))
            continue
        hn, hs, an, as_ = finals[0]
        team_runs = hs if b["player_name"] == hn else as_
        opp_runs  = as_ if b["player_name"] == hn else hs
        actual = (team_runs + opp_runs) if b["stat"] == "total_runs" \
            else (team_runs - opp_runs)
        out["resolved"].append((b["id"], b["player_name"], actual))
        if not dry_run:
            log_result(b["id"], actual)
    return out


def update_and_resolve(dry_run=False, fetch=api_get) -> dict:
    """One call: refresh logs for pending players, grade game bets
    from schedule scores, then run the normal auto-resolver."""
    from src.factors.prop_tracker import auto_resolve_pending_bets
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        logs = refresh_player_logs(conn, dry_run, fetch)
        games = resolve_game_bets_from_schedule(conn, dry_run, fetch)
    finally:
        conn.close()
    res = auto_resolve_pending_bets(dry_run=dry_run)
    return {"log_refresh": logs, "game_bets": games, "auto_resolve": res}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = update_and_resolve(dry_run=a.dry_run)
    lr, gb, ar = r["log_refresh"], r["game_bets"], r["auto_resolve"]
    print(f"Player logs: {lr['players_updated']} players refreshed "
          f"({lr['rows_inserted']} game rows)"
          + (f"; not found: {lr['not_found']}" if lr["not_found"] else ""))
    print(f"Game bets:   {len(gb['resolved'])} resolved from schedule "
          f"scores, {len(gb['skipped'])} skipped")
    print(f"Auto-resolve: {len(ar['resolved'])} graded, "
          f"{len(ar['skipped_no_data'])} awaiting data, "
          f"{len(ar['skipped_ambiguous'])} ambiguous")
    for item in ar["skipped_no_data"][:10]:
        print(f"   pending: #{item['id']} {item['player']} — {item['reason']}")
