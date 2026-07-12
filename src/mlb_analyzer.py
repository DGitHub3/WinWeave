"""
mlb_analyzer.py — MLB player-prop EV analysis (v2).

Reuses the exact same math core as NFL (src/ev_engine.py):
Poisson/Normal models, hit rates, vig removal, EV, Kelly.
Only the data layer differs — that's the whole point of the
shared-engine architecture: one brain, many sports.

v2 signals (6):
  1. Weighted hit rate           22%
  2. Statistical model           25%
  3. Opponent team factor        13%
  4. Home/away split             10%
  5. Ballpark factor             15%   (NEW)
  6. Batter-vs-starter           15%   (NEW, only when data supports it)

Ballpark factor is computed entirely from data already in your
database — no new API dependency. It compares a team's own hitters'
production at home vs. on the road. This is a simplified, single-team
version of the textbook park factor (which also incorporates the
opponent's split at that same park); it trades some precision for
being fully self-contained.

Batter-vs-starter uses the announced probable starting pitcher
attached to each batting row by build_mlb_db.py. When a batter
doesn't have enough history against a specific starter (<3 games),
that signal's weight is redistributed proportionally across the
other signals rather than faked with a copy of the general hit rate.
"""

import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.ev_engine import (
    american_to_implied_prob, remove_vig, decimal_payout,
    normal_probability, poisson_probability,
    hit_rate, weighted_hit_rate,
    calculate_ev, kelly_criterion,
)
from src.calibration import calibrate, shrink_hit_rate

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "winweave.db"

# Which table and model each MLB stat uses
MLB_STATS = {
    #  stat               table           model      league avg/game (fallback, team total)
    "hits":            ("mlb_batting",  "poisson",  8.5),
    "total_bases":     ("mlb_batting",  "normal",   13.5),
    "home_runs":       ("mlb_batting",  "poisson",  1.2),
    "rbi":             ("mlb_batting",  "poisson",  4.4),
    "runs":            ("mlb_batting",  "poisson",  4.4),
    "walks":           ("mlb_batting",  "poisson",  3.2),
    "stolen_bases":    ("mlb_batting",  "poisson",  0.7),
    "batter_strikeouts": ("mlb_batting", "poisson", 8.5),   # batter Ks (different market from pitcher Ks)
    "strikeouts":      ("mlb_pitching", "poisson",  8.5),   # pitcher Ks
    "outs_recorded":   ("mlb_pitching", "normal",   16.0),  # pitcher outs
    "walks_allowed":   ("mlb_pitching", "poisson",  3.2),   # pitcher BB allowed
    "earned_runs":     ("mlb_pitching", "poisson",  4.3),   # pitcher ER allowed
    "hits_allowed":    ("mlb_pitching", "poisson",  8.5),   # pitcher hits allowed
}

_OPP_CACHE: dict = {}
_LEAGUE_CACHE: dict = {}
_PARK_CACHE: dict = {}

BASE_WEIGHTS = {
    "hit_rate":   0.22,
    "model":      0.25,
    "opponent":   0.13,
    "home_away":  0.10,
    "park":       0.15,
    "bvp":        0.15,
}


@dataclass
class MLBPropAnalysis:
    player_name: str
    stat: str
    line: float
    side: str
    book: str
    american_odds: int
    hit_rate_signal: float
    model_signal: float
    opponent_signal: float
    home_away_signal: float
    park_signal: Optional[float]
    park_factor: float
    bvp_signal: Optional[float]
    bvp_sample: int
    opposing_starter: str
    true_probability: float
    implied_probability: float
    no_vig_probability: float
    ev_percent: float
    kelly_fraction: float
    edge: float
    sample_size: int
    mean_stat: float
    std_stat: float
    opponent: str
    opp_allows: float
    is_home: Optional[bool]
    weights_used: dict = field(default_factory=dict)

    # v3: calibration context
    raw_model_prob: float = 0.0
    model_weight:   float = 0.0
    cal_capped:     bool  = False
    cal_flag:       Optional[str] = None

    def grade(self) -> str:
        """
        v3 HONEST GRADING — same rationale as ev_engine.PropAnalysis:
        the v2 thresholds graded 22/22 tracked picks "A", including
        +80% and +110% EV claims. Raw model-vs-market gaps >= 15 pts
        now route to AUDIT (run explain_mlb_prop.py) instead of a
        letter grade, because gaps that size are broken input data
        far more often than real edges.
        """
        if self.cal_flag == "AUDIT":
            gap = abs(self.raw_model_prob - self.no_vig_probability)
            return (f"X — Audit first: model vs market gap {gap:.0%}. "
                    "Run explain_mlb_prop.py before trusting.")
        if self.ev_percent > 15:
            return ("X — Audit first: {:+.1f}% EV is implausible for a "
                    "player prop; longshot pricing or bad input data is "
                    "far more likely than a real edge this size."
                    .format(self.ev_percent))
        if self.ev_percent <= 0:
            return "F — Negative EV, skip"
        if self.sample_size < 10:
            return "C — Positive EV but thin sample (<10 games)"
        if self.ev_percent >= 6 and self.sample_size >= 20 \
                and not self.cal_capped \
                and self.no_vig_probability >= 0.25:  # v3.1: no A on longshots
            return "A — Strong edge, good sample"
        if self.ev_percent >= 3:
            return "B — Real but modest edge"
        return "C — Marginal edge, likely inside the noise"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _mlb_tables_exist(conn) -> bool:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return {"mlb_batting", "mlb_pitching", "mlb_players"}.issubset(tables)


_PITCH_START_FILTER: list = []

def _pitching_starts_filter(conn: sqlite3.Connection) -> str:
    """
    v3 — THE QUANTRILL FIX. Pitcher prop markets (strikeouts, outs
    recorded, hits allowed, earned runs) are STARTER markets, but
    get_player_games previously sampled every mlb_pitching row —
    relief and short outings included. A few 1-2 inning relief rows
    in a 25-game sample crater the mean, which is precisely how the
    model claimed 94.4% for Quantrill under 14.5 outs (market: ~45%)
    and 97.4% under 4.5 hits allowed on 2026-07-10.

    Schema-adaptive: prefers an explicit starter flag if the table
    has one; otherwise falls back to outs_recorded >= 9 (3+ innings),
    which excludes relief cameos while keeping short/blowup starts —
    those are real starter outcomes and must stay in the sample.
    """
    if _PITCH_START_FILTER:
        return _PITCH_START_FILTER[0]
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(mlb_pitching)").fetchall()}
    if "games_started" in cols:
        # games_started=1 alone is not enough: "opener" outings (a
        # planned 1-2 inning start in front of a bulk reliever) are
        # recorded as starts but are relief workloads. Quantrill's
        # 2026-06-23 outing vs Miami was exactly this — 2 IP as an
        # opener. Requiring 7+ outs keeps genuine short/blowup starts
        # (3+ IP) while excluding opener cameos and relief.
        f = "AND games_started >= 1 AND outs_recorded >= 7"
    elif "is_starter" in cols:
        f = "AND is_starter = 1"
    elif "outs_recorded" in cols:
        f = "AND outs_recorded >= 9"
    else:
        f = ""
    _PITCH_START_FILTER.append(f)
    return f


def get_player_games(player: str, stat: str, table: str,
                     n_games: int = 25) -> list[sqlite3.Row]:
    conn = _conn()
    try:
        if not _mlb_tables_exist(conn):
            raise ValueError(
                "MLB tables not found in the database yet. Run "
                "'python scrapers/build_mlb_db.py' first."
            )
        starts = _pitching_starts_filter(conn) \
            if table == "mlb_pitching" else ""
        return conn.execute(f"""
            SELECT {stat} AS v, is_home, game_date, opponent
            FROM {table}
            WHERE full_name = ? AND {stat} IS NOT NULL
              {starts}
            ORDER BY game_date DESC
            LIMIT ?
        """, (player, n_games)).fetchall()
    finally:
        conn.close()


def get_opponent_allows(opponent: str, stat: str, table: str) -> float:
    """Per-game total of this stat that the opposing TEAM gives up."""
    key = (opponent, stat)
    if key in _OPP_CACHE:
        return _OPP_CACHE[key]
    conn = _conn()
    try:
        rows = conn.execute(f"""
            SELECT game_date, SUM({stat}) AS total
            FROM {table}
            WHERE opponent = ? AND {stat} IS NOT NULL
            GROUP BY game_date
        """, (opponent,)).fetchall()
        totals = [r["total"] for r in rows if r["total"] is not None]
        result = sum(totals) / len(totals) if totals else \
                 get_league_allows(stat, table)
        _OPP_CACHE[key] = result
        return result
    finally:
        conn.close()


def get_league_allows(stat: str, table: str) -> float:
    """League average per-game team total, computed the same way."""
    if stat in _LEAGUE_CACHE:
        return _LEAGUE_CACHE[stat]
    conn = _conn()
    try:
        rows = conn.execute(f"""
            SELECT opponent, game_date, SUM({stat}) AS total
            FROM {table}
            WHERE {stat} IS NOT NULL
            GROUP BY opponent, game_date
        """).fetchall()
        totals = [r["total"] for r in rows if r["total"] is not None]
        result = sum(totals) / len(totals) if totals else \
                 MLB_STATS[stat][2]
        _LEAGUE_CACHE[stat] = result
        return result
    finally:
        conn.close()


def get_park_factor(team: str, stat: str, table: str) -> float:
    """
    Empirical park factor: ratio of a TEAM's own batters' average
    per-game production at home vs. on the road.

    Only computed for batting stats — pitching park effects are much
    noisier over a partial season with far fewer starts to sample,
    so this returns a neutral 1.0 for the pitching table in v1.

    Capped at 0.85-1.20 to avoid small-sample overreaction (a team
    3 games into a homestand shouldn't produce a 1.8x park factor).
    """
    if table != "mlb_batting" or not team:
        return 1.0

    key = (team, stat)
    if key in _PARK_CACHE:
        return _PARK_CACHE[key]

    conn = _conn()
    try:
        home_row = conn.execute(f"""
            SELECT AVG(b.{stat}) AS avg_val, COUNT(*) AS n
            FROM mlb_batting b
            JOIN mlb_players p ON p.player_id = b.player_id
            WHERE p.team = ? AND b.is_home = 1 AND b.{stat} IS NOT NULL
        """, (team,)).fetchone()
        road_row = conn.execute(f"""
            SELECT AVG(b.{stat}) AS avg_val, COUNT(*) AS n
            FROM mlb_batting b
            JOIN mlb_players p ON p.player_id = b.player_id
            WHERE p.team = ? AND b.is_home = 0 AND b.{stat} IS NOT NULL
        """, (team,)).fetchone()

        home_avg = home_row["avg_val"] if home_row else None
        road_avg = road_row["avg_val"] if road_row else None
        min_sample = 20  # require a reasonable number of rows on both sides

        if not home_avg or not road_avg or road_avg <= 0 or \
           (home_row["n"] or 0) < min_sample or (road_row["n"] or 0) < min_sample:
            factor = 1.0
        else:
            factor = max(0.85, min(1.20, home_avg / road_avg))

        _PARK_CACHE[key] = factor
        return factor
    finally:
        conn.close()


def get_player_team(player: str) -> str:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT team FROM mlb_players WHERE full_name = ? LIMIT 1",
            (player,)
        ).fetchone()
        return row["team"] if row else ""
    finally:
        conn.close()


def get_vs_starter_history(player: str, table: str, stat: str,
                           starter_name: str, n_games: int = 50) -> list[float]:
    """
    Batter's historical stat values specifically in games their team
    faced this particular starting pitcher (via the announced probable
    starter — see caveats in build_mlb_db.py's fetch_schedule_probables).
    """
    if table != "mlb_batting" or not starter_name:
        return []
    conn = _conn()
    try:
        rows = conn.execute(f"""
            SELECT {stat} AS v FROM mlb_batting
            WHERE full_name = ? AND vs_starter_name = ?
              AND {stat} IS NOT NULL
            ORDER BY game_date DESC LIMIT ?
        """, (player, starter_name, n_games)).fetchall()
        return [r["v"] for r in rows]
    finally:
        conn.close()


def _redistribute_weights(weights: dict, drop_key: str) -> dict:
    """Removes a signal's weight and redistributes it proportionally
    across the remaining signals, keeping total weight at 1.0."""
    w = weights.copy()
    dropped = w.pop(drop_key)
    total = sum(w.values())
    if total <= 0:
        return w
    for k in w:
        w[k] += dropped * (w[k] / total)
    return w


def analyze_mlb_prop(
    player_name: str,
    stat:        str,
    line:        float,
    over_odds:   int,
    under_odds:  int,
    opponent:    str,
    side:        str  = "over",
    book:        str  = "manual",
    is_home:     Optional[bool] = None,
    opposing_starter: Optional[str] = None,
    n_games:     int  = 25,
) -> MLBPropAnalysis:
    if stat not in MLB_STATS:
        raise ValueError(f"Unknown MLB stat '{stat}'. "
                         f"Options: {', '.join(MLB_STATS)}")
    table, model, _ = MLB_STATS[stat]

    games = get_player_games(player_name, stat, table, n_games)
    if not games:
        raise ValueError(
            f"No MLB data for '{player_name}' / '{stat}'. Run "
            f"scrapers/build_mlb_db.py first, and check name spelling."
        )

    values = [g["v"] for g in games]
    mean_v = sum(values) / len(values)
    std_v  = math.sqrt(sum((v - mean_v)**2 for v in values) / len(values))
    sample = len(values)

    def model_prob(mean, std=std_v):
        if model == "poisson":
            return poisson_probability(mean, line, side)
        return normal_probability(mean, std, line, side)

    # ── v3: market numbers FIRST — needed as shrinkage prior and
    # calibration anchor for everything below.
    american = over_odds if side == "over" else under_odds
    implied  = american_to_implied_prob(american)
    nv_o, nv_u = remove_vig(over_odds, under_odds)
    no_vig   = nv_o if side == "over" else nv_u

    # Signal 1: weighted hit rate — v3: empirical-Bayes shrunk toward
    # the market base rate. 25 hot games still speak; 6 don't scream.
    hr_raw = 0.4 * hit_rate(values, line, side) + \
             0.6 * weighted_hit_rate(values, line, side)
    hr = shrink_hit_rate(hr_raw * sample, sample, no_vig)

    # Signal 2: statistical model
    model_sig = model_prob(mean_v)

    # Signal 3: opponent factor
    opp_allows    = get_opponent_allows(opponent, stat, table)
    league_allows = get_league_allows(stat, table)
    opp_mult = max(0.75, min(1.25, opp_allows / league_allows)) \
        if league_allows > 0 else 1.0
    opp_sig = model_prob(mean_v * opp_mult)

    # Signal 4: home/away split — v3: shrunk; a 5-game split is far
    # too small to take at face value.
    if is_home is not None:
        split = [g["v"] for g in games if bool(g["is_home"]) == is_home]
        if len(split) >= 5:
            raw_ha = hit_rate(split, line, side)
            ha_sig = shrink_hit_rate(raw_ha * len(split), len(split),
                                     no_vig)
        else:
            ha_sig = hr
    else:
        ha_sig = hr

    # Signal 5: ballpark factor (new)
    weights = BASE_WEIGHTS.copy()
    park_factor = 1.0
    park_sig = None
    if is_home is not None:
        park_team = get_player_team(player_name) if is_home else opponent
        park_factor = get_park_factor(park_team, stat, table)
        if park_factor != 1.0:
            park_sig = model_prob(mean_v * park_factor)
    if park_sig is None:
        weights = _redistribute_weights(weights, "park")

    # Signal 6: batter-vs-starter (new)
    bvp_sig = None
    bvp_values = get_vs_starter_history(player_name, table, stat,
                                        opposing_starter or "")
    bvp_sample = len(bvp_values)
    if opposing_starter and bvp_sample >= 3:
        # v3: shrunk hard — BvP samples of 3-10 games are the most
        # famous noise trap in baseball analytics. With K=20 pseudo-
        # games of market prior, a 3-game sample moves the needle
        # only slightly, which is all it has earned.
        bvp_sig = shrink_hit_rate(
            hit_rate(bvp_values, line, side) * bvp_sample,
            bvp_sample, no_vig)
    else:
        weights = _redistribute_weights(weights, "bvp")

    # Combine whatever signals are actually active
    signal_map = {
        "hit_rate":  hr,
        "model":     model_sig,
        "opponent":  opp_sig,
        "home_away": ha_sig,
    }
    if park_sig is not None:
        signal_map["park"] = park_sig
    if bvp_sig is not None:
        signal_map["bvp"] = bvp_sig

    total_prob = sum(weights[k] * signal_map[k] for k in signal_map)
    total_w    = sum(weights[k] for k in signal_map)
    raw_prob   = max(0.01, min(0.99, total_prob / total_w))

    # ── v3 CALIBRATION: market anchoring + edge cap ────────────
    # (odds/no_vig were computed at the top of this function)
    cal = calibrate(raw_prob, no_vig, sample)
    true_prob = cal.probability

    ev    = calculate_ev(true_prob, american) * 100
    kelly = kelly_criterion(true_prob, american)

    return MLBPropAnalysis(
        player_name=player_name, stat=stat, line=line, side=side,
        book=book, american_odds=american,
        hit_rate_signal=hr, model_signal=model_sig,
        opponent_signal=opp_sig, home_away_signal=ha_sig,
        park_signal=park_sig, park_factor=park_factor,
        bvp_signal=bvp_sig, bvp_sample=bvp_sample,
        opposing_starter=opposing_starter or "",
        true_probability=true_prob, implied_probability=implied,
        no_vig_probability=no_vig, ev_percent=ev,
        kelly_fraction=kelly, edge=true_prob - no_vig,
        sample_size=sample, mean_stat=mean_v, std_stat=std_v,
        opponent=opponent, opp_allows=opp_allows, is_home=is_home,
        weights_used=weights,
        raw_model_prob=cal.raw_model_prob,
        model_weight=cal.model_weight,
        cal_capped=cal.capped,
        cal_flag=cal.flag,
    )


def print_mlb_analysis(r: MLBPropAnalysis):
    w = 64
    label = "POSITIVE EV" if r.ev_percent > 0 else "NEGATIVE EV"
    sign = "+" if r.ev_percent >= 0 else ""
    print("\n" + "="*w)
    print(f"  {r.player_name}  —  {r.stat.replace('_',' ').title()}  [MLB]")
    print(f"  {r.side.upper()} {r.line}  |  {r.american_odds:+d}  |  {r.book.upper()}")
    print("="*w)
    print(f"\n  HISTORICAL  (last {r.sample_size} games)")
    print(f"  |- Average:    {r.mean_stat:.2f}")
    print(f"  |- Std dev:    {r.std_stat:.2f}")
    print(f"  '- Hit rate:   {r.hit_rate_signal:.1%}")
    print(f"\n  SIGNALS  (active weights shown; inactive signals redistributed)")
    print(f"  |- Hit rate:   {r.hit_rate_signal:.1%}  "
          f"({r.weights_used.get('hit_rate',0):.0%})")
    print(f"  |- Model:      {r.model_signal:.1%}  "
          f"({r.weights_used.get('model',0):.0%})")
    print(f"  |- Opponent:   {r.opponent_signal:.1%}  "
          f"({r.weights_used.get('opponent',0):.0%})  "
          f"[{r.opponent} allows {r.opp_allows:.1f}/gm]")
    print(f"  |- Home/Away:  {r.home_away_signal:.1%}  "
          f"({r.weights_used.get('home_away',0):.0%})")
    if r.park_signal is not None:
        print(f"  |- Park:       {r.park_signal:.1%}  "
              f"({r.weights_used.get('park',0):.0%})  "
              f"[factor: {r.park_factor:.2f}x]")
    else:
        print(f"  |- Park:       skipped (insufficient home/road sample yet)")
    if r.bvp_signal is not None:
        print(f"  '- Vs Starter: {r.bvp_signal:.1%}  "
              f"({r.weights_used.get('bvp',0):.0%})  "
              f"[{r.opposing_starter}, {r.bvp_sample} career games]")
    else:
        reason = "no starter specified" if not r.opposing_starter else \
                 f"only {r.bvp_sample} career games vs {r.opposing_starter} (<3)"
        print(f"  '- Vs Starter: skipped ({reason})")
    print(f"\n  VERDICT  —  {label}")
    print(f"  |- Raw model:  {r.raw_model_prob:.1%}  (signal blend, pre-anchor)")
    print(f"  |- Anchor:     {r.no_vig_probability:.1%} market, "
          f"model weight {r.model_weight:.0%}")
    print(f"  |- TRUE PROB:  {r.true_probability:.1%}"
          f"{'  [edge-capped]' if r.cal_capped else ''}")
    print(f"  |- Book fair:  {r.no_vig_probability:.1%}")
    print(f"  |- Edge:       {sign}{r.edge:.1%}")
    print(f"  |- EV%:        {sign}{r.ev_percent:.2f}%")
    print(f"  |- Kelly:      {r.kelly_fraction:.1%} of bankroll")
    print(f"  '- Grade:      {r.grade()}")
    print("="*w + "\n")
