"""
src/factors/prop_tracker.py — The feedback loop.

Tracks predictions made, logs actual results, and reports back on
model accuracy over time — plus real-money bankroll tracking, since
"was the model right" and "did I actually make money" are related
but genuinely different questions worth answering separately.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "winweave.db"


def prop_tracker_signal(player_name: str, stat: str, line: float, side: str,
                        position: str = None) -> float:
    """
    NFL Signal 4 (14% weight): how has THIS player performed in our
    own previously tracked/logged predictions, for props on this
    stat with a similar line -- distinct from the "Hit rate" signal,
    which reads raw historical game logs instead of our own tracked
    prediction history.

    RECONSTRUCTED 2026-07-09: this function was accidentally dropped
    when prop_tracker.py was rebuilt from memory during a bug-fix
    session, causing an ImportError. This is a careful reconstruction
    based on the confirmed call site (prop_analyzer.py) and
    combine_all_signals() in ev_engine.py, which uses this value
    directly in a weighted average with no None-check -- meaning,
    unlike MLB's skippable park/bvp signals, this one must ALWAYS
    return a real float, never None. 0.5 (neutral, uninformative) is
    returned as the fallback when there's no tracked history yet,
    rather than skipping/redistributing weight. If NFL analysis
    behavior seems different from before this fix, this function is
    the first place to check -- it is a reconstruction, not a
    guaranteed match to whatever the original implementation did.

    "position" is accepted to match the real call signature but
    isn't used in this reconstruction's logic.
    """
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # "Line range" rather than an exact match -- a prop line for
        # the same player/stat rarely moves far week to week, so a
        # tolerance keeps this a meaningful comparison without being
        # so strict that it almost never has any data to use.
        tolerance = max(abs(line) * 0.2, 1.0)
        rows = conn.execute("""
            SELECT result FROM prop_results
            WHERE player_name = ? AND stat = ? AND side = ?
              AND result IS NOT NULL
              AND line BETWEEN ? AND ?
        """, (player_name, stat, side, line - tolerance, line + tolerance)
        ).fetchall()

        if len(rows) < 3:
            return 0.5  # not enough tracked history -- neutral, uninformative

        hits = sum(1 for r in rows if r["result"] == "hit")
        return hits / len(rows)
    finally:
        conn.close()


def ensure_prop_results_table():
    """
    Creates the prop_results table if it doesn't exist.
    This is the feedback loop table — the model's memory.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prop_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name      TEXT NOT NULL,
                player_id        TEXT,
                stat             TEXT NOT NULL,
                line             REAL NOT NULL,
                side             TEXT NOT NULL,
                book             TEXT,
                american_odds    INTEGER,
                opponent         TEXT,
                season           INTEGER,
                week             INTEGER,
                game_date        TEXT,
                predicted_prob   REAL,
                ev_percent       REAL,
                kelly_fraction   REAL,
                grade            TEXT,
                actual_value     REAL,
                result           TEXT,
                hit_rate_signal  REAL,
                model_signal     REAL,
                weather_mult     REAL,
                roster_mult      REAL,
                coaching_mult    REAL,
                official_mult    REAL,
                analyzed_at      TEXT NOT NULL,
                result_logged_at TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prop_results_player
            ON prop_results (player_name, stat)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prop_results_season
            ON prop_results (season, week)
        """)
        existing_cols = {r[1] for r in
            conn.execute("PRAGMA table_info(prop_results)").fetchall()}
        if "bet_placed" not in existing_cols:
            conn.execute(
                "ALTER TABLE prop_results ADD COLUMN bet_placed INTEGER DEFAULT 0"
            )
        if "stake" not in existing_cols:
            conn.execute("ALTER TABLE prop_results ADD COLUMN stake REAL")
        if "payout" not in existing_cols:
            conn.execute("ALTER TABLE prop_results ADD COLUMN payout REAL")
        if "is_bonus_bet" not in existing_cols:
            conn.execute(
                "ALTER TABLE prop_results ADD COLUMN is_bonus_bet INTEGER DEFAULT 0"
            )
        conn.commit()
    finally:
        conn.close()


def find_existing_bet(player_name: str, stat: str, line: float, side: str,
                       book: str, stake: float = None) -> Optional[int]:
    """
    Checks whether a REAL bet matching these exact details is already
    logged and still PENDING (no result yet). Returns the existing
    row's id if found, else None.

    FIXED (this replaces a same-day-only check): a real bet is
    commonly placed one day and not resolved/logged until a later
    day -- the original version only matched same-day entries
    (date(analyzed_at) = date('now')), so a bet placed on the 9th
    and resolved via a fresh save_prediction() call on a later day
    wasn't recognized as the same bet, creating a duplicate row
    instead of reusing the original. Scoping to "still pending"
    instead of "same day" fixes that while still allowing a genuinely
    new bet on the same player/stat/line/side/book to be logged once
    the earlier one has actually resolved -- a resolved bet no longer
    matches, so it can't block a real new wager placed later.
    """
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("""
            SELECT id FROM prop_results
            WHERE player_name = ? AND stat = ? AND line = ? AND side = ?
              AND book = ? AND bet_placed = 1 AND result IS NULL
            ORDER BY id DESC LIMIT 1
        """, (player_name, stat, line, side, book)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_prediction(row_id: int) -> Optional[dict]:
    """Fetches one full row from prop_results as a dict."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM prop_results WHERE id = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_prediction(
    player_name:     str,
    stat:            str,
    line:            float,
    side:            str,
    book:            str,
    american_odds:   int,
    opponent:        str,
    season:          int,
    week:            int,
    predicted_prob:  float,
    ev_percent:      float,
    kelly_fraction:  float,
    grade:           str,
    game_date:       str   = None,
    player_id:       str   = None,
    sub_signals:     dict  = None,
    bet_placed:      bool  = False,
    stake:           float = None,
    force:           bool  = False,
    is_bonus_bet:    bool  = False,
) -> int:
    """
    Saves a model prediction to prop_results BEFORE the game.

    Safety: for REAL wagers (bet_placed=True), this checks whether
    the exact same bet was already logged today, and returns the
    EXISTING row's id instead of creating a duplicate. Pass
    force=True on the rare occasion you genuinely placed the same
    bet twice.
    """
    ensure_prop_results_table()
    sub = sub_signals or {}

    if bet_placed and not force:
        existing_id = find_existing_bet(player_name, stat, line, side, book)
        if existing_id is not None:
            print(f"  Already logged as prediction #{existing_id} (still "
                  f"pending) -- reusing it instead of creating a duplicate. "
                  f"Pass force=True if you really did place this bet twice.")
            return existing_id

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute("""
            INSERT INTO prop_results (
                player_name, player_id, stat, line, side, book,
                american_odds, opponent, season, week, game_date,
                predicted_prob, ev_percent, kelly_fraction, grade,
                hit_rate_signal, model_signal, weather_mult,
                roster_mult, coaching_mult, official_mult,
                analyzed_at, bet_placed, stake, is_bonus_bet
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            player_name, player_id, stat, line, side, book,
            american_odds, opponent, season, week, game_date,
            predicted_prob, ev_percent, kelly_fraction, grade,
            sub.get("hit_rate"), sub.get("model_prob"),
            sub.get("weather_mult"), sub.get("roster_mult"),
            sub.get("coaching_mult"), sub.get("official_mult"),
            datetime.now(timezone.utc).isoformat(),
            1 if bet_placed else 0, stake, 1 if is_bonus_bet else 0,
        ))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def edit_prediction(
    row_id:        int,
    player_name:   str   = None,
    stat:          str   = None,
    line:          float = None,
    side:          str   = None,
    book:          str   = None,
    american_odds: int   = None,
    stake:         float = None,
    bet_placed:    bool  = None,
    is_bonus_bet:  bool  = None,
    opponent:      str   = None,
    season:        int   = None,
    game_date:     str   = None,
) -> bool:
    """
    Corrects a mistake on an already-logged prediction. If this row
    already has a result logged, result and payout are automatically
    recomputed from the corrected values -- including correctly
    switching a resolved miss to $0 (not -stake) if you realize
    after the fact that a bet was a bonus/free bet.
    """
    existing = get_prediction(row_id)
    if existing is None:
        print(f"No prediction found with id={row_id}")
        return False

    updates = {
        "player_name": player_name, "stat": stat, "line": line,
        "side": side, "book": book, "american_odds": american_odds,
        "stake": stake,
        "bet_placed": (1 if bet_placed else 0) if bet_placed is not None else None,
        "is_bonus_bet": (1 if is_bonus_bet else 0) if is_bonus_bet is not None else None,
        "opponent": opponent, "season": season, "game_date": game_date,
    }
    merged = {**existing, **{k: v for k, v in updates.items() if v is not None}}

    if existing["result"] is not None:
        merged["result"] = _determine_result(
            existing["actual_value"], merged["line"], merged["side"])
        if merged["bet_placed"] and merged["stake"]:
            merged["payout"] = _compute_payout(
                merged["stake"], merged["american_odds"], merged["result"],
                bool(merged["is_bonus_bet"]))
        else:
            merged["payout"] = None

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            UPDATE prop_results SET
                player_name=?, stat=?, line=?, side=?, book=?,
                american_odds=?, stake=?, bet_placed=?, is_bonus_bet=?,
                opponent=?, season=?, game_date=?,
                result=?, payout=?
            WHERE id=?
        """, (
            merged["player_name"], merged["stat"], merged["line"],
            merged["side"], merged["book"], merged["american_odds"],
            merged["stake"], merged["bet_placed"], merged["is_bonus_bet"],
            merged["opponent"], merged["season"], merged["game_date"],
            merged["result"], merged["payout"], row_id,
        ))
        conn.commit()
        return True
    finally:
        conn.close()


def _compute_payout(stake: float, american_odds: int, result: str,
                    is_bonus_bet: bool = False) -> float:
    """
    Net profit/loss on a real wager. Push returns the stake (0 net).

    is_bonus_bet: a promotional free bet / bonus bet isn't real
    withdrawable cash -- losing one costs $0 real money, not -stake,
    since that money was never actually yours to lose. Winning one
    is unaffected: sportsbooks pay bonus bet wins as PROFIT ONLY,
    never returning the "stake" itself either way -- which happens
    to be exactly what this formula already computes for every win
    (stake is deliberately excluded from the win branch below), so
    no special-casing is needed there.
    """
    if result == "push":
        return 0.0
    if result == "miss":
        return 0.0 if is_bonus_bet else -stake
    if american_odds > 0:
        return stake * (american_odds / 100)
    return stake * (100 / abs(american_odds))


def _determine_result(actual_value: float, line: float, side: str) -> str:
    if actual_value == line:
        return "push"
    if side == "over":
        return "hit" if actual_value > line else "miss"
    return "hit" if actual_value < line else "miss"


def void_bet(row_id: int):
    """
    Marks a bet as VOIDED by the sportsbook -- distinct from a push.
    A push means the stat landed exactly on the line (a real outcome
    of the game). A void means the book cancelled the bet entirely
    for an unrelated reason (a scratched player, a rules dispute,
    an admin error) -- the underlying stat outcome doesn't matter
    and often isn't even known/relevant. Either way the stake is
    returned in full, net $0 -- true for both real cash and bonus
    bets, so no bonus-bet special-casing is needed here.

    Use log_result() for a normal resolved outcome; use this instead
    when the book itself cancelled the wager.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            UPDATE prop_results
            SET result = 'void', payout = 0.0, result_logged_at = ?
            WHERE id = ?
        """, (datetime.now(timezone.utc).isoformat(), row_id))
        conn.commit()
        print(f"  Prediction #{row_id} marked VOID -- stake returned, $0 net")
    finally:
        conn.close()


def log_result(row_id: int, actual_value: float):
    """After the game, records what actually happened and computes
    payout for real wagers automatically (bonus-bet-aware: a losing
    bonus bet costs $0 real money, not the stake)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT line, side, bet_placed, stake, american_odds, is_bonus_bet "
            "FROM prop_results WHERE id = ?",
            (row_id,)
        ).fetchone()

        if not row:
            print(f"No prediction found with id={row_id}")
            return

        line   = row["line"]
        side   = row["side"]
        result = _determine_result(actual_value, line, side)

        payout = None
        if row["bet_placed"] and row["stake"]:
            payout = _compute_payout(row["stake"], row["american_odds"],
                                     result, bool(row["is_bonus_bet"]))

        conn.execute("""
            UPDATE prop_results
            SET actual_value = ?, result = ?, result_logged_at = ?, payout = ?
            WHERE id = ?
        """, (actual_value, result,
               datetime.now(timezone.utc).isoformat(), payout, row_id))
        conn.commit()
        msg = f"  Result logged: {actual_value:.1f} vs {line} ({side}) → {result.upper()}"
        if payout is not None:
            msg += f"  |  payout: {payout:+.2f}"
        print(msg)
    finally:
        conn.close()


def get_player_track_record(player_name: str, min_sample: int = 3) -> Optional[dict]:
    """Structured hit-rate history for ONE player, from real logged results."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as hits,
                   SUM(CASE WHEN bet_placed=1 THEN 1 ELSE 0 END) as bets_placed,
                   SUM(CASE WHEN bet_placed=1 THEN payout ELSE 0 END) as net_profit
            FROM prop_results
            WHERE player_name = ? AND result IS NOT NULL
        """, (player_name,)).fetchone()

        if not row or row["total"] < min_sample:
            return None

        return {
            "player_name": player_name,
            "total": row["total"],
            "hits": row["hits"] or 0,
            "hit_rate": (row["hits"] or 0) / row["total"],
            "bets_placed": row["bets_placed"] or 0,
            "net_profit": row["net_profit"] or 0.0,
        }
    finally:
        conn.close()


def get_all_player_track_records(min_sample: int = 3) -> list[dict]:
    """Same as get_player_track_record but for every player with
    enough logged history, sorted by hit rate descending."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT player_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as hits,
                   SUM(CASE WHEN bet_placed=1 THEN 1 ELSE 0 END) as bets_placed,
                   SUM(CASE WHEN bet_placed=1 THEN payout ELSE 0 END) as net_profit
            FROM prop_results
            WHERE result IS NOT NULL
            GROUP BY player_name
            HAVING total >= ?
            ORDER BY CAST(hits AS REAL)/total DESC, total DESC
        """, (min_sample,)).fetchall()

        return [{
            "player_name": r["player_name"],
            "total": r["total"],
            "hits": r["hits"] or 0,
            "hit_rate": (r["hits"] or 0) / r["total"],
            "bets_placed": r["bets_placed"] or 0,
            "net_profit": r["net_profit"] or 0.0,
        } for r in rows]
    finally:
        conn.close()


def get_bankroll_summary() -> dict:
    """Real money summary across every RESOLVED logged wager, globally."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT COUNT(*) as n_bets,
                   SUM(stake) as total_staked,
                   SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END) as losses,
                   SUM(payout) as net_profit
            FROM prop_results
            WHERE bet_placed = 1 AND result IS NOT NULL
        """).fetchone()
        return {
            "n_bets": row["n_bets"] or 0,
            "total_staked": row["total_staked"] or 0.0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "net_profit": row["net_profit"] or 0.0,
        }
    finally:
        conn.close()


def ensure_bankroll_settings_table():
    """A tiny key-value table: one starting balance per sportsbook."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_settings (
                book             TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL,
                updated_at       TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def set_starting_balance(book: str, amount: float):
    """Sets (or resets) the starting balance for a sportsbook."""
    ensure_bankroll_settings_table()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO bankroll_settings (book, starting_balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(book) DO UPDATE SET
                starting_balance = excluded.starting_balance,
                updated_at = excluded.updated_at
        """, (book, amount, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def ensure_bank_transactions_table():
    """bankroll_transactions — deposits and withdrawals per book, so
    current balance = starting + net deposits + net profit − pending
    stakes. Re-ups and cash-outs finally have a home."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL,
                amount REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'deposit',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )""")
        conn.commit()
    finally:
        conn.close()


def add_bank_transaction(book: str, amount: float,
                         kind: str = "deposit", note: str = ""):
    """kind: 'deposit' | 'withdrawal'. Amount always positive."""
    from datetime import datetime, timezone
    ensure_bank_transactions_table()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO bankroll_transactions
                (book, amount, kind, note, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (book, abs(float(amount)), kind, note,
              datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_deposits_by_book() -> dict:
    """{book: net_deposits} — deposits minus withdrawals."""
    ensure_bank_transactions_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return {r["book"]: r["net"] for r in conn.execute("""
            SELECT book,
                   SUM(CASE WHEN kind='withdrawal' THEN -amount
                            ELSE amount END) AS net
            FROM bankroll_transactions GROUP BY book
        """).fetchall()}
    finally:
        conn.close()


def get_bankroll_by_book() -> list[dict]:
    """
    Per-book bankroll: starting balance, real bets (resolved AND
    pending), W-L, staked amounts, net profit, and current balance.

    FIXED (2026-07-09): the original version only counted bets with
    result IS NOT NULL, which silently excluded every PENDING real
    bet from "Staked" entirely -- a real user had $80 sitting in
    pending DraftKings bets and $17 in pending BetMGM bets that
    showed as $0.00 staked with no record at all, because none of
    them had resolved yet. Now split into resolved (for W-L and net
    profit, which only make sense once a result exists) and pending
    (money already committed the moment a bet is placed, in real
    life, whether or not the outcome is known yet).

    current_balance now correctly subtracts pending stakes too --
    a sportsbook deducts your stake the moment you place a bet, not
    when it resolves, so pending money is already "gone" from your
    visible balance even though the outcome is unknown. EXCEPT for
    bonus bets: a pending bonus bet's stake was never real cash to
    begin with, so it doesn't reduce your real/withdrawable balance
    the way a real pending stake does -- only tracked separately as
    "pending_bonus_staked" for visibility, not subtracted from
    current_balance.
    """
    ensure_prop_results_table()
    ensure_bankroll_settings_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        starting = {r["book"]: r["starting_balance"] for r in conn.execute(
            "SELECT book, starting_balance FROM bankroll_settings").fetchall()}
        deposits = get_deposits_by_book()

        bet_books = {r[0] for r in conn.execute(
            "SELECT DISTINCT book FROM prop_results WHERE bet_placed=1"
        ).fetchall()}

        all_books = sorted(set(starting) | bet_books | set(deposits))
        results = []
        for book in all_books:
            resolved = conn.execute("""
                SELECT COUNT(*) as n_bets, SUM(stake) as staked,
                       SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END) as losses,
                       SUM(payout) as net_profit
                FROM prop_results
                WHERE bet_placed=1 AND result IS NOT NULL AND book=?
            """, (book,)).fetchone()
            pending_real = conn.execute("""
                SELECT COUNT(*) as n_bets, SUM(stake) as staked
                FROM prop_results
                WHERE bet_placed=1 AND result IS NULL AND book=?
                  AND (is_bonus_bet=0 OR is_bonus_bet IS NULL)
            """, (book,)).fetchone()
            pending_bonus = conn.execute("""
                SELECT COUNT(*) as n_bets, SUM(stake) as staked
                FROM prop_results
                WHERE bet_placed=1 AND result IS NULL AND book=?
                  AND is_bonus_bet=1
            """, (book,)).fetchone()

            start_bal = starting.get(book, 0.0)
            net = resolved["net_profit"] or 0.0
            resolved_staked = resolved["staked"] or 0.0
            pending_real_staked = pending_real["staked"] or 0.0
            pending_bonus_staked = pending_bonus["staked"] or 0.0

            results.append({
                "book": book,
                "starting_balance": start_bal,
                "n_bets": resolved["n_bets"] or 0,
                "wins": resolved["wins"] or 0,
                "losses": resolved["losses"] or 0,
                "total_staked": resolved_staked + pending_real_staked + pending_bonus_staked,
                "pending_staked": pending_real_staked,
                "pending_count": pending_real["n_bets"] or 0,
                "pending_bonus_staked": pending_bonus_staked,
                "pending_bonus_count": pending_bonus["n_bets"] or 0,
                "net_profit": net,
                "deposits": deposits.get(book, 0.0),
                # v3.6: current balance now includes deposits and
                # withdrawals — starting + net deposits + net profit
                # − money currently locked in pending real stakes.
                "current_balance": start_bal + deposits.get(book, 0.0)
                                   + net - pending_real_staked,
            })
        return results
    finally:
        conn.close()


# ── Auto-resolve: fill in real results without manual typing ────

NFL_STATS = {
    "passing_yards", "rushing_yards", "receiving_yards", "receptions",
    "passing_tds", "rushing_tds", "receiving_tds", "targets",
    "interceptions", "carries", "completions", "attempts",
}


def auto_resolve_pending_bets(dry_run: bool = False) -> dict:
    """
    Walks every still-pending real MLB bet and tries to find the
    player's actual game result from mlb_batting/mlb_pitching,
    logging it automatically via log_result() when found
    unambiguously -- no manual typing of the actual stat value.

    Run this after refreshing your MLB tables (build_mlb_db.py) so
    the games in question actually exist in the database yet.

    Matching strategy, safest first:
    1. If the bet has a game_date stored, match that exact date.
    2. Otherwise, look for games on or after the date the bet was
       logged (analyzed_at) -- but only auto-resolve if that yields
       EXACTLY ONE candidate game. Two or more possible games means
       genuine ambiguity (which one was this bet actually for?),
       so it's left pending rather than guessed at.

    Only MLB bets are handled (the player must exist in
    mlb_players) -- NFL bets, parlays, and any stat not in
    MLB_STATS are left untouched, since there's no single-table
    lookup that resolves them safely.

    dry_run=True: reports what WOULD be resolved without writing
    anything -- run this first to sanity-check before trusting it.

    Returns {"resolved": [...], "skipped_no_data": [...],
             "skipped_ambiguous": [...], "skipped_not_mlb": [...]}
    each a list of dicts describing what happened to each pending bet.
    """
    from src.mlb_analyzer import MLB_STATS

    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    summary = {"resolved": [], "skipped_no_data": [],
               "skipped_ambiguous": [], "skipped_not_mlb": []}
    try:
        # v3.3: resolves ALL pending predictions, not just real-money
        # bets. Paper-tracked picks (bet_placed=0) are the calibration
        # sample — the entire point of logging them is that they get
        # graded, so the model's report card grows at zero dollar cost.
        pending = conn.execute("""
            SELECT id, player_name, stat, side, line, game_date,
                   analyzed_at, opponent, season, week
            FROM prop_results
            WHERE result IS NULL
        """).fetchall()

        for bet in pending:
            # ── v3.2: GAME-LEVEL bets (moneyline / run_line /
            # total_runs) resolve from reconstructed team scores,
            # not from a player's box line. Encoding (see
            # src/game_analyzer.py docstring): player_name holds the
            # team bet on (or the home team for totals), opponent
            # holds the other club; actual_value is the margin for
            # moneyline/run_line and combined runs for total_runs.
            if bet["stat"] in ("moneyline", "run_line", "total_runs"):
                from src.game_analyzer import get_final_scores
                scores = get_final_scores(
                    bet["player_name"], bet["opponent"] or "",
                    bet["analyzed_at"][:10],
                    exact_date=bet["game_date"])
                if scores is None:
                    summary["skipped_no_data"].append({
                        "id": bet["id"], "player": bet["player_name"],
                        "reason": "game not in DB yet, doubleheader "
                                  "(resolve manually), or refresh "
                                  "needed (build_mlb_db.py)"})
                    continue
                team_runs, opp_runs, gdate = scores
                actual = (team_runs + opp_runs) \
                    if bet["stat"] == "total_runs" \
                    else (team_runs - opp_runs)
                summary["resolved"].append({
                    "id": bet["id"], "player": bet["player_name"],
                    "stat": bet["stat"], "actual_value": actual,
                    "game_date": gdate})
                if not dry_run:
                    log_result(bet["id"], actual)
                continue

            # ── v3.4: NFL bets resolve from the props table by
            # player + season + week (the nflverse weekly refresh is
            # the result feed, so this works the Monday after
            # refresh_season_data.py runs). Requires week > 0 on the
            # bet — the dashboard now records it. One-row-or-pending:
            # duplicate props rows (the Josh Allen bug) make a bet
            # ambiguous rather than silently wrong.
            if bet["stat"] in NFL_STATS:
                wk = bet["week"] if "week" in bet.keys() else None
                if not wk:
                    summary["skipped_no_data"].append({
                        "id": bet["id"], "player": bet["player_name"],
                        "reason": "no week recorded on bet — resolve "
                                  "manually via track_result.py"})
                    continue
                rows = conn.execute(f"""
                    SELECT {bet['stat']} AS v FROM props
                    WHERE player_display_name = ? AND season = ?
                      AND week = ? AND {bet['stat']} IS NOT NULL
                """, (bet["player_name"], bet["season"], wk)).fetchall()
                if len(rows) != 1:
                    reason = ("game not in props yet — run "
                              "refresh_season_data.py") if not rows                         else "duplicate props rows — dedupe first"
                    summary["skipped_no_data" if not rows else
                            "skipped_ambiguous"].append({
                        "id": bet["id"], "player": bet["player_name"],
                        "reason": reason})
                    continue
                actual = rows[0]["v"]
                summary["resolved"].append({
                    "id": bet["id"], "player": bet["player_name"],
                    "stat": bet["stat"], "actual_value": actual,
                    "game_date": f"{bet['season']} wk{wk}"})
                if not dry_run:
                    log_result(bet["id"], actual)
                continue

            if bet["stat"] not in MLB_STATS:
                summary["skipped_not_mlb"].append(
                    {"id": bet["id"], "player": bet["player_name"],
                     "reason": f"stat '{bet['stat']}' has no MLB table mapping "
                              f"(NFL bet or a parlay/multi-leg bet)"})
                continue

            table, _, _ = MLB_STATS[bet["stat"]]
            player_row = conn.execute(
                "SELECT player_id FROM mlb_players WHERE full_name = ?",
                (bet["player_name"],)
            ).fetchone()
            if not player_row:
                summary["skipped_not_mlb"].append(
                    {"id": bet["id"], "player": bet["player_name"],
                     "reason": "not found in mlb_players (NFL bet, or a name "
                              "mismatch worth checking)"})
                continue

            if bet["game_date"]:
                games = conn.execute(f"""
                    SELECT game_date, {bet['stat']} as val FROM {table}
                    WHERE full_name = ? AND game_date = ?
                """, (bet["player_name"], bet["game_date"])).fetchall()
            else:
                # UTC BUFFER FIX: analyzed_at is stored in UTC, but
                # game_date is a US-local calendar date. A bet tracked
                # at 9pm CT on July 9 has analyzed_at = July 10 in UTC,
                # so "game_date >= analyzed_at date" skipped that
                # night's game forever (permanent skipped_no_data).
                # Searching from one day earlier fixes it; the
                # exactly-one-candidate rule below still protects
                # against grabbing the wrong game — extra candidates
                # just mean "left pending", never a wrong resolve.
                games = conn.execute(f"""
                    SELECT game_date, {bet['stat']} as val FROM {table}
                    WHERE full_name = ? AND game_date >= date(?, '-1 day')
                    ORDER BY game_date
                """, (bet["player_name"], bet["analyzed_at"][:10])).fetchall()

            if not games:
                summary["skipped_no_data"].append(
                    {"id": bet["id"], "player": bet["player_name"],
                     "reason": "no matching game found yet -- game may not "
                              "have happened, or mlb_batting/mlb_pitching "
                              "needs a refresh (build_mlb_db.py)"})
                continue
            if len(games) > 1:
                summary["skipped_ambiguous"].append(
                    {"id": bet["id"], "player": bet["player_name"],
                     "reason": f"{len(games)} candidate games found with no "
                              f"game_date stored on the bet to disambiguate -- "
                              f"left pending rather than guess"})
                continue

            actual_value = games[0]["val"]
            if not dry_run:
                log_result(bet["id"], actual_value)
            summary["resolved"].append(
                {"id": bet["id"], "player": bet["player_name"],
                 "stat": bet["stat"], "actual_value": actual_value,
                 "game_date": games[0]["game_date"]})

        return summary
    finally:
        conn.close()


def model_accuracy_report() -> str:
    """Human-readable summary of model accuracy: overall, by
    predicted-probability bucket, by stat, and by top players."""
    ensure_prop_results_table()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT result, predicted_prob, stat, player_name
            FROM prop_results
            WHERE result IS NOT NULL
              AND result NOT IN ('push', 'void')
        """).fetchall()
        # push/void excluded: a push is a tie and a void never
        # happened — counting either as a "miss" (which the old
        # WHERE clause silently did) deflates every accuracy number
        # in this report.

        if not rows:
            return "\nNo completed predictions yet. Log some results first.\n"

        total = len(rows)
        hits = sum(1 for r in rows if r["result"] == "hit")

        lines = ["\n" + "="*60, "  MODEL ACCURACY REPORT", "="*60]
        lines.append(f"\nOverall: {hits}/{total} ({100*hits/total:.1f}%)")

        buckets = {"<50%": [], "50-60%": [], "60-70%": [], "70-80%": [], "80%+": []}
        for r in rows:
            p = r["predicted_prob"] or 0
            if p < 0.5: buckets["<50%"].append(r)
            elif p < 0.6: buckets["50-60%"].append(r)
            elif p < 0.7: buckets["60-70%"].append(r)
            elif p < 0.8: buckets["70-80%"].append(r)
            else: buckets["80%+"].append(r)

        lines.append("\nCalibration by predicted probability:")
        for label, bucket_rows in buckets.items():
            if bucket_rows:
                b_hits = sum(1 for r in bucket_rows if r["result"] == "hit")
                lines.append(f"  {label:<8} {b_hits}/{len(bucket_rows)} "
                             f"({100*b_hits/len(bucket_rows):.0f}%)")

        by_stat = {}
        for r in rows:
            by_stat.setdefault(r["stat"], []).append(r)
        lines.append("\nBy stat:")
        for stat, stat_rows in sorted(by_stat.items()):
            s_hits = sum(1 for r in stat_rows if r["result"] == "hit")
            lines.append(f"  {stat:<20} {s_hits}/{len(stat_rows)} "
                         f"({100*s_hits/len(stat_rows):.0f}%)")

        by_player = {}
        for r in rows:
            by_player.setdefault(r["player_name"], []).append(r)
        qualifying = {p: rs for p, rs in by_player.items() if len(rs) >= 3}
        if qualifying:
            lines.append("\nTOP 5 PLAYERS (by model accuracy, min 3 logged):")
            ranked = sorted(
                qualifying.items(),
                key=lambda kv: sum(1 for r in kv[1] if r["result"]=="hit") / len(kv[1]),
                reverse=True,
            )[:5]
            for player, p_rows in ranked:
                p_hits = sum(1 for r in p_rows if r["result"] == "hit")
                lines.append(f"  {player:<25} {p_hits}/{len(p_rows)} "
                             f"({100*p_hits/len(p_rows):.0f}%)")

        lines.append("="*60 + "\n")
        return "\n".join(lines)
    finally:
        conn.close()
