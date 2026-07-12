"""
game_analyzer.py — WinWeave game-level MLB markets (v1).

Analyzes MONEYLINE, RUN LINE, and GAME TOTAL markets using team-level
run scoring/allowing rates reconstructed from the mlb_batting table
you already build — no new data source, no new API calls.

HOW TEAM STATS ARE RECONSTRUCTED (and why it works):
  mlb_batting stores each batter's game with the OPPONENT they faced.
  Every real game therefore appears as two groups of rows:
  (date, opponent=T) — those are T's opponents' batters, whose runs
  sum to "runs T allowed" — and the mirror group for runs T scored.
  Each group's own team identity is recovered by majority-voting the
  batters' current teams from mlb_players (one traded player can't
  outvote eight teammates). Doubleheader dates are detected (a batter
  appearing twice on one date) and skipped rather than merged.

MODEL (deliberately simple v1):
  expected_runs(team) = (team scored/gm + opp allowed/gm) / 2,
  small home bump. Margin ~ Normal(exp_home − exp_away, 4.2);
  Total ~ Normal(exp_home + exp_away, 4.6). Then EVERYTHING passes
  through src.calibration.calibrate() against the no-vig market
  number, exactly like player props — which matters even more here,
  because game markets are the sharpest, most efficient lines the
  books offer. Expect mostly F grades. That is the market being good,
  not the code being broken.

TRACKING ENCODING (fits the existing prop_results table unchanged):
  moneyline : player_name=<team bet on>, opponent=<other team>,
              stat='moneyline',  side='over', line=0.0,
              actual_value = chosen team's margin  (win = margin > 0)
  run_line  : same, stat='run_line', line = -(team's spread)
              (a -1.5 favorite -> line=1.5: must win by 2+;
               a +1.5 dog     -> line=-1.5: margin > -1.5 covers)
  total     : player_name=<home team>, opponent=<away team>,
              stat='total_runs', side='over'/'under', line=<total>,
              actual_value = combined runs
  This reuses the tracker's over/under hit logic, payout math, the
  dashboard, and calibration_report.py with zero schema changes.
"""

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.calibration import calibrate, grade_pick, fractional_kelly, \
    CalibratedProb
from src.ev_engine import american_to_implied_prob, remove_vig, \
    calculate_ev, kelly_criterion

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "winweave.db"

MARGIN_SD = 4.2   # empirical MLB final-margin spread
TOTAL_SD  = 4.6   # empirical MLB game-total spread
HOME_RUNS_BUMP = 0.10  # home teams score ~a tenth of a run more

def local_game_date(starts_at):
    """SGO startsAt (UTC) -> US-local calendar date. Starts before
    08:00 UTC are evening games from the previous local day (8:10pm
    CT = 01:10 UTC next date; nothing starts 00:00-08:00 UTC
    otherwise)."""
    if not starts_at:
        return None
    from datetime import datetime as _dt, timedelta as _td
    try:
        ts = _dt.fromisoformat(starts_at.replace("Z", "+00:00"))
        d = ts.date() - _td(days=1) if ts.hour < 8 else ts.date()
        return d.isoformat()
    except ValueError:
        return starts_at[:10]


_TEAM_GAMES_CACHE: dict = {}
_KNOWN_TEAMS_CACHE: list = []


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── SGO team-ID -> database team-name matching ─────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def known_team_names() -> list[str]:
    """Distinct opponent names in mlb_batting — the DB's vocabulary."""
    if _KNOWN_TEAMS_CACHE:
        return _KNOWN_TEAMS_CACHE
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT opponent FROM mlb_batting "
            "WHERE opponent != ''").fetchall()
        _KNOWN_TEAMS_CACHE.extend(r[0] for r in rows)
        return _KNOWN_TEAMS_CACHE
    finally:
        conn.close()


def match_team_name(sgo_team_id: str) -> Optional[str]:
    """
    'TORONTO_BLUE_JAYS_MLB' -> 'Toronto Blue Jays', matched against
    the names actually present in mlb_batting (normalization handles
    punctuation cases like 'St. Louis Cardinals').
    """
    raw = sgo_team_id.rsplit("_MLB", 1)[0].replace("_", " ")
    target = _norm(raw)
    for name in known_team_names():
        if _norm(name) == target:
            return name
    # fallback: unique containment match
    hits = [n for n in known_team_names()
            if target in _norm(n) or _norm(n) in target]
    return hits[0] if len(hits) == 1 else None


# ── Team game reconstruction ───────────────────────────────────

def build_team_games(team: str, n_games: int = 30) -> list[dict]:
    """
    Last N completed games for `team`:
    [{game_date, opponent, scored, allowed, is_home}, ...] newest first.
    Doubleheader dates are excluded (can't split merged box sums).
    """
    key = (team, n_games)
    if key in _TEAM_GAMES_CACHE:
        return _TEAM_GAMES_CACHE[key]

    conn = _conn()
    try:
        team_of = {r["full_name"]: r["team"] for r in conn.execute(
            "SELECT full_name, team FROM mlb_players")}

        # Rows where batters FACED this team -> team's runs allowed,
        # and the batters' majority team identifies the opponent.
        opp_rows = conn.execute("""
            SELECT game_date, full_name, runs, is_home
            FROM mlb_batting WHERE opponent = ?
            ORDER BY game_date DESC
        """, (team,)).fetchall()

        games = []
        by_date: dict = {}
        for r in opp_rows:
            by_date.setdefault(r["game_date"], []).append(r)

        for gdate, rows in by_date.items():
            names = [r["full_name"] for r in rows]
            if len(names) != len(set(names)):
                continue  # doubleheader — skip rather than merge
            allowed = sum(r["runs"] or 0 for r in rows)
            teams = [team_of.get(n) for n in names if team_of.get(n)]
            if not teams:
                continue
            opponent = Counter(teams).most_common(1)[0][0]
            # mirror group: team's own batters that date faced `opponent`
            own = conn.execute("""
                SELECT full_name, runs, is_home FROM mlb_batting
                WHERE opponent = ? AND game_date = ?
            """, (opponent, gdate)).fetchall()
            own_names = [r["full_name"] for r in own]
            if not own or len(own_names) != len(set(own_names)):
                continue
            scored = sum(r["runs"] or 0 for r in own)
            games.append({
                "game_date": gdate, "opponent": opponent,
                "scored": scored, "allowed": allowed,
                "is_home": bool(own[0]["is_home"]),
            })

        games.sort(key=lambda g: g["game_date"], reverse=True)
        games = games[:n_games]
        _TEAM_GAMES_CACHE[key] = games
        return games
    finally:
        conn.close()


def team_run_rates(team: str, n_games: int = 30) -> Optional[dict]:
    g = build_team_games(team, n_games)
    if len(g) < 5:
        return None
    n = len(g)
    return {
        "scored_pg":  sum(x["scored"] for x in g) / n,
        "allowed_pg": sum(x["allowed"] for x in g) / n,
        "n": n,
    }


# ── Probability model ──────────────────────────────────────────

def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def expected_runs(home: str, away: str) -> Optional[tuple]:
    h, a = team_run_rates(home), team_run_rates(away)
    if not h or not a:
        return None
    exp_h = (h["scored_pg"] + a["allowed_pg"]) / 2 + HOME_RUNS_BUMP / 2
    exp_a = (a["scored_pg"] + h["allowed_pg"]) / 2 - HOME_RUNS_BUMP / 2
    return exp_h, exp_a, min(h["n"], a["n"])


@dataclass
class GameAnalysis:
    market:       str          # moneyline | run_line | total
    home_team:    str
    away_team:    str
    side:         str          # home | away | over | under
    line:         float
    book:         str
    american_odds: int
    true_probability:  float
    no_vig_probability: float
    ev_percent:   float
    kelly_fraction: float
    sample_size:  int
    exp_home:     float
    exp_away:     float
    cal:          CalibratedProb = None
    starts_at:    str = ""
    deeplink:     str = ""

    def grade(self) -> str:
        return grade_pick(self.ev_percent, self.sample_size, self.cal)

    # -- Dashboard/tracker compatibility --------------------------
    @property
    def stat(self) -> str:
        return {"moneyline": "moneyline", "run_line": "run_line",
                "total": "total_runs"}[self.market]

    @property
    def player_name(self) -> str:
        """Display + tracking identity: the team bet on (home team
        for totals, matching the resolution encoding)."""
        if self.market == "total" or self.side == "home":
            return self.home_team
        return self.away_team

    @property
    def opponent(self) -> str:
        if self.player_name == self.home_team:
            return self.away_team
        return self.home_team

    @property
    def matchup(self) -> str:
        when = local_game_date(self.starts_at)
        return f"{self.away_team} @ {self.home_team}" + \
            (f" ({when})" if when else "")

    def tracker_encoding(self) -> dict:
        """Exact field values a prop_results row must carry so the
        tracker's over/under hit logic and auto-resolve grade this
        bet correctly (see module docstring for the scheme)."""
        if self.market == "total":
            side, line = self.side, self.line
        elif self.market == "moneyline":
            side, line = "over", 0.0
        else:  # run_line: cover means margin > -(handicap)
            side, line = "over", -self.line
        return {"player_name": self.player_name,
                "opponent": self.opponent,
                "stat": self.stat, "side": side, "line": line,
                "game_date": local_game_date(self.starts_at)}

    def describe(self) -> str:
        if self.market == "moneyline":
            team = self.home_team if self.side == "home" else self.away_team
            return f"{team} ML"
        if self.market == "run_line":
            team = self.home_team if self.side == "home" else self.away_team
            return f"{team} {self.line:+.1f}"
        return f"{self.side.title()} {self.line:g}"


def analyze_game_market(market: str, home_team: str, away_team: str,
                        side: str, line: float,
                        side_odds: int, other_odds: int,
                        book: str = "manual",
                        starts_at: str = "",
                        deeplink: str = "") -> Optional[GameAnalysis]:
    """
    Analyze one side of a game market. `other_odds` is the opposite
    side's price at the same book/line — required for vig removal.
    For run_line, `line` is THIS side's handicap (e.g. -1.5 home
    favorite, +1.5 away dog).
    """
    exp = expected_runs(home_team, away_team)
    if exp is None:
        return None
    exp_h, exp_a, n = exp
    # v1 MODEL HAIRCUT: this game model is pitcher-blind (a Skenes
    # start and a bullpen game project identically), and the first
    # live pull (2026-07-11) showed the resulting systematic tilt:
    # it liked nearly every underdog ML. Capping the effective sample
    # at 12 keeps the model's blend weight ~29% vs the market until
    # the feedback loop earns it more. Raise only if the calibration
    # report shows game markets beating the market baseline.
    n_eff = min(n, 12)

    if market == "moneyline":
        margin = exp_h - exp_a
        p_home = 1 - _phi((0 - margin) / MARGIN_SD)
        raw = p_home if side == "home" else 1 - p_home
    elif market == "run_line":
        team_margin = (exp_h - exp_a) if side == "home" else (exp_a - exp_h)
        # cover if margin + handicap > 0  ->  margin > -line
        raw = 1 - _phi((-line - team_margin) / MARGIN_SD)
    elif market == "total":
        total = exp_h + exp_a
        p_over = 1 - _phi((line - total) / TOTAL_SD)
        raw = p_over if side == "over" else 1 - p_over
    else:
        return None

    nv_this, _ = remove_vig(side_odds, other_odds)
    cal = calibrate(raw, nv_this, n_eff)
    p = cal.probability
    ev = calculate_ev(p, side_odds) * 100

    return GameAnalysis(
        market=market, home_team=home_team, away_team=away_team,
        side=side, line=line, book=book, american_odds=side_odds,
        true_probability=p, no_vig_probability=nv_this,
        ev_percent=ev, kelly_fraction=kelly_criterion(p, side_odds),
        sample_size=n, exp_home=exp_h, exp_away=exp_a, cal=cal,
        starts_at=starts_at, deeplink=deeplink,
    )


# ── Tracking & resolution ──────────────────────────────────────

def save_game_prediction(r: GameAnalysis, bet_placed: bool = False,
                         stake: float = 0.0) -> int:
    """Insert into the existing prop_results table (see module
    docstring for the encoding). Returns the new row id."""
    if r.market == "total":
        player_name, opponent = r.home_team, r.away_team
        stat, side, line = "total_runs", r.side, r.line
    else:
        team = r.home_team if r.side == "home" else r.away_team
        opp  = r.away_team if r.side == "home" else r.home_team
        player_name, opponent = team, opp
        stat = r.market
        side = "over"
        line = 0.0 if r.market == "moneyline" else -r.line

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        gdate = local_game_date(r.starts_at)
        cur = conn.execute("""
            INSERT INTO prop_results
                (player_name, stat, line, side, book, american_odds,
                 opponent, game_date, predicted_prob, ev_percent,
                 kelly_fraction, grade, analyzed_at, bet_placed, stake)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (player_name, stat, line, side, r.book, r.american_odds,
              opponent, gdate, r.true_probability, r.ev_percent,
              r.kelly_fraction, r.grade(),
              datetime.now(timezone.utc).isoformat(),
              1, stake))  # bet_placed=1 always: auto-resolve only
                          # walks bet_placed rows; stake 0 = tracking-only
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_final_scores(team: str, opponent: str, on_or_after: str,
                     exact_date: str = None) -> Optional[tuple]:
    """
    (team_runs, opponent_runs, game_date) for a completed game
    between the two teams. Series-safe:

    - exact_date given (US-local date stored at tracking time):
      only that date qualifies. Series-safe by construction.
    - no exact_date: search on/after `on_or_after` minus a day, and
      resolve ONLY if exactly one candidate game exists. Teams play
      3-4 game series; "first game after the bet date" silently
      grades the wrong game, so one-candidate-or-pending is the rule
      (same discipline as the player-prop resolver).

    Doubleheader dates (a batter appearing twice) also return None.
    """
    conn = _conn()
    try:
        team_of = {r["full_name"]: r["team"] for r in conn.execute(
            "SELECT full_name, team FROM mlb_players")}

        if exact_date:
            # exact_date is already the US-local game date (computed
            # from startsAt at save time) — one date, no window, no
            # false ambiguity during a series.
            date_clause = "AND game_date = ?"
            date_params = (exact_date,)
        else:
            date_clause = "AND game_date >= date(?, '-1 day')"
            date_params = (on_or_after,)

        dates = [r[0] for r in conn.execute(f"""
            SELECT DISTINCT game_date FROM mlb_batting
            WHERE opponent = ? {date_clause}
            ORDER BY game_date
        """, (team, *date_params)).fetchall()]

        candidates = []
        for gdate in dates:
            opp_batters = conn.execute("""
                SELECT full_name, runs FROM mlb_batting
                WHERE opponent = ? AND game_date = ?
            """, (team, gdate)).fetchall()
            own_batters = conn.execute("""
                SELECT full_name, runs FROM mlb_batting
                WHERE opponent = ? AND game_date = ?
            """, (opponent, gdate)).fetchall()
            if not own_batters:
                continue  # that date was vs someone else
            # Verify mutual matchup: batters facing `team` must
            # majority-belong to `opponent`.
            facing = [team_of.get(r["full_name"])
                      for r in opp_batters if team_of.get(r["full_name"])]
            if not facing or \
                    Counter(facing).most_common(1)[0][0] != opponent:
                continue
            names_o = [r["full_name"] for r in opp_batters]
            names_s = [r["full_name"] for r in own_batters]
            if len(names_o) != len(set(names_o)) or \
               len(names_s) != len(set(names_s)):
                return None  # doubleheader — resolve manually
            team_runs = sum(r["runs"] or 0 for r in own_batters)
            opp_runs  = sum(r["runs"] or 0 for r in opp_batters)
            candidates.append((team_runs, opp_runs, gdate))

        # Exactly one qualifying game, or nothing. Two candidates =
        # genuine ambiguity (series games) = left pending.
        return candidates[0] if len(candidates) == 1 else None
    finally:
        conn.close()
