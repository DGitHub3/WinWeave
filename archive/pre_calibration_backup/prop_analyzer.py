"""
prop_analyzer.py — Connects the database to all 8 EV signals. (v3)

FIXES in v3:
  1. RECENCY FILTER: player stats now come from the player's most
     recent seasons only (default: last 2 seasons of THEIR career),
     so we model who the player is NOW, not their whole career.
  2. TEAM-LEVEL DEFENSE: "opponent allows" is now the per-GAME total
     a defense gives up to a position group (e.g. "DAL allows 108
     rushing yds/game to RBs"), and the league baseline is computed
     the same way from the database itself — no more definitional
     mismatch, no more hardcoded league averages for defense.
"""

import math
import sqlite3
from pathlib import Path
from typing import Optional

from src.ev_engine import (
    PropAnalysis, LEAGUE_AVERAGES,
    american_to_implied_prob, remove_vig,
    calculate_probability, hit_rate, weighted_hit_rate,
    opponent_multiplier, calculate_ev, kelly_criterion,
    combine_all_signals,
)
from src.factors.weather      import get_weather_for_team_game, weather_multiplier, describe_weather_impact
from src.factors.officials    import get_referee_for_team_week, official_multiplier, describe_crew
from src.factors.coaching     import coaching_multiplier, pace_factor, describe_coaching
from src.factors.roster       import roster_multiplier
from src.factors.prop_tracker import prop_tracker_signal, ensure_prop_results_table

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "winweave.db"

# Session caches so repeated scans don't re-run heavy queries
_DEF_CACHE: dict = {}
_LEAGUE_DEF_CACHE: dict = {}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── FIX 1: Recency-filtered player stats ───────────────────────

def get_player_recent_stats(player_name: str, stat: str,
                             n_games: int = 15,
                             recent_seasons: int = 2) -> list[float]:
    """
    Last N games from the player's most recent `recent_seasons`
    seasons only. Anchored to the PLAYER's latest season (not the
    calendar), so it works mid-season and offseason alike.
    """
    conn = get_connection()
    try:
        latest = conn.execute(f"""
            SELECT MAX(season) FROM props
            WHERE player_display_name = ? AND {stat} IS NOT NULL
        """, (player_name,)).fetchone()[0]

        if latest is None:
            return []

        cutoff = latest - (recent_seasons - 1)

        rows = conn.execute(f"""
            SELECT {stat} FROM props
            WHERE player_display_name = ?
              AND {stat} IS NOT NULL
              AND season >= ?
            ORDER BY season DESC, week DESC
            LIMIT ?
        """, (player_name, cutoff, n_games)).fetchall()
        return [r[0] for r in rows if r[0] is not None]
    finally:
        conn.close()


# ── FIX 2: Team-level defensive totals ─────────────────────────

def get_opponent_stat_allowed(opponent: str, stat: str,
                               position: str) -> float:
    """
    Per-GAME total this defense allows to a position group.
    E.g. "DAL allows 108.3 rushing yards per game to RBs".

    Method: sum the stat across all players at that position in
    each individual game vs this defense, then average those
    per-game totals.
    """
    key = (opponent, stat, position)
    if key in _DEF_CACHE:
        return _DEF_CACHE[key]

    conn = get_connection()
    try:
        rows = conn.execute(f"""
            SELECT season, week, SUM({stat}) AS game_total
            FROM props
            WHERE opponent_team = ?
              AND position = ?
              AND {stat} IS NOT NULL
              AND season >= 2023
            GROUP BY season, week
        """, (opponent, position)).fetchall()

        totals = [r["game_total"] for r in rows if r["game_total"] is not None]
        result = (sum(totals) / len(totals)) if totals else \
                 get_league_stat_allowed(stat, position)
        _DEF_CACHE[key] = result
        return result
    finally:
        conn.close()


def get_league_stat_allowed(stat: str, position: str) -> float:
    """
    League-average per-game total for this stat/position combo,
    computed the SAME way as the opponent number — so the ratio
    between them is meaningful. Computed once per session from
    your own database instead of hardcoded guesses.
    """
    key = (stat, position)
    if key in _LEAGUE_DEF_CACHE:
        return _LEAGUE_DEF_CACHE[key]

    conn = get_connection()
    try:
        rows = conn.execute(f"""
            SELECT opponent_team, season, week, SUM({stat}) AS game_total
            FROM props
            WHERE position = ?
              AND {stat} IS NOT NULL
              AND season >= 2023
            GROUP BY opponent_team, season, week
        """, (position,)).fetchall()

        totals = [r["game_total"] for r in rows if r["game_total"] is not None]
        result = (sum(totals) / len(totals)) if totals else \
                 LEAGUE_AVERAGES.get(stat, 50.0)
        _LEAGUE_DEF_CACHE[key] = result
        return result
    finally:
        conn.close()


# ── Player metadata ────────────────────────────────────────────

def get_player_position(player_name: str) -> Optional[str]:
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT position FROM props
            WHERE player_display_name = ?
            ORDER BY season DESC LIMIT 1
        """, (player_name,)).fetchone()
        return row["position"] if row else None
    finally:
        conn.close()


def get_player_team(player_name: str, season: int = None) -> Optional[str]:
    conn = get_connection()
    try:
        season_clause = f"AND season = {int(season)}" if season else ""
        row = conn.execute(f"""
            SELECT team FROM rosters
            WHERE full_name LIKE ?
              {season_clause}
            ORDER BY season DESC LIMIT 1
        """, (f"%{player_name}%",)).fetchone()
        return row["team"] if row else None
    finally:
        conn.close()


# ── Main analysis ──────────────────────────────────────────────

def analyze_prop(
    player_name:  str,
    stat:         str,
    line:         float,
    over_odds:    int,
    under_odds:   int,
    opponent:     str,
    side:         str   = "over",
    book:         str   = "manual",
    n_games:      int   = 15,
    season:       int   = None,
    week:         int   = None,
    recent_seasons: int = 2,
) -> PropAnalysis:
    """Full 8-signal EV analysis for a player prop."""
    ensure_prop_results_table()

    # ── Historical stats (recency-filtered) ───────────────────
    values = get_player_recent_stats(player_name, stat,
                                     n_games, recent_seasons)
    if not values:
        raise ValueError(
            f"No recent data for '{player_name}' / '{stat}'. "
            f"Check spelling against the database."
        )

    mean_stat = sum(values) / len(values)
    variance  = sum((v - mean_stat)**2 for v in values) / len(values)
    std_stat  = math.sqrt(variance)
    sample    = len(values)

    # ── Signals 1 & 2: hit rate + statistical model ───────────
    simple_hr   = hit_rate(values, line, side)
    weighted_hr = weighted_hit_rate(values, line, side)
    hr_signal   = 0.4 * simple_hr + 0.6 * weighted_hr
    model_sig   = calculate_probability(mean_stat, std_stat, line, stat, side)

    # ── Signal 3: opponent defense (team-level, same-basis) ───
    position   = get_player_position(player_name) or "WR"
    opp_avg    = get_opponent_stat_allowed(opponent, stat, position)
    league_avg = get_league_stat_allowed(stat, position)
    def_mult   = opponent_multiplier(opp_avg, league_avg)
    adj_mean   = mean_stat * def_mult
    def_sig    = calculate_probability(adj_mean, std_stat, line, stat, side)

    # ── Signal 4: prop tracker ────────────────────────────────
    pt_sig = prop_tracker_signal(player_name, stat, line, side, position)

    # ── Signal 5: roster health ───────────────────────────────
    team = get_player_team(player_name, season) or opponent
    r_mult, r_details = roster_multiplier(player_name, team)

    # ── Signal 6: coaching tendency + pace ────────────────────
    c_season = season or 2024
    c_mult = coaching_multiplier(team, c_season, stat)
    c_mult = 0.5 * c_mult + 0.5 * pace_factor(team, c_season)
    c_desc = describe_coaching(team, c_season)

    # ── Signal 7: weather ─────────────────────────────────────
    weather = get_weather_for_team_game(team, season, week) \
        if (season and week) else {}
    w_mult = weather_multiplier(weather, stat)
    w_desc = describe_weather_impact(weather, stat)

    # ── Signal 8: officials ───────────────────────────────────
    referee = get_referee_for_team_week(team, season, week) \
        if (season and week) else None
    o_mult = official_multiplier(referee, stat)
    o_desc = describe_crew(referee)

    # ── Combine all 8 signals ─────────────────────────────────
    true_prob = combine_all_signals(
        hit_rate_prob     = hr_signal,
        model_prob        = model_sig,
        defense_prob      = def_sig,
        prop_tracker_prob = pt_sig,
        roster_mult       = r_mult,
        coaching_mult     = c_mult,
        weather_mult      = w_mult,
        official_mult     = o_mult,
        sample_size       = sample,
    )

    # ── Book probabilities & EV ───────────────────────────────
    american_odds = over_odds if side == "over" else under_odds
    implied_prob  = american_to_implied_prob(american_odds)
    nv_over, nv_under = remove_vig(over_odds, under_odds)
    no_vig_prob   = nv_over if side == "over" else nv_under

    ev_pct = calculate_ev(true_prob, american_odds) * 100
    kelly  = kelly_criterion(true_prob, american_odds)
    edge   = true_prob - no_vig_prob

    return PropAnalysis(
        player_name         = player_name,
        stat                = stat,
        line                = line,
        side                = side,
        book                = book,
        american_odds       = american_odds,
        hit_rate_signal     = hr_signal,
        model_signal        = model_sig,
        defense_signal      = def_sig,
        prop_tracker_signal = pt_sig,
        roster_mult         = r_mult,
        coaching_mult       = c_mult,
        weather_mult        = w_mult,
        official_mult       = o_mult,
        true_probability    = true_prob,
        implied_probability = implied_prob,
        no_vig_probability  = no_vig_prob,
        ev_percent          = ev_pct,
        kelly_fraction      = kelly,
        edge                = edge,
        sample_size         = sample,
        mean_stat           = mean_stat,
        std_stat            = std_stat,
        opponent_avg        = opp_avg,
        opponent            = opponent,
        roster_details      = r_details,
        weather_desc        = w_desc,
        official_desc       = o_desc,
        coaching_desc       = c_desc,
    )


def print_full_analysis(result: PropAnalysis):
    """Complete formatted output of a prop analysis."""
    w = 65
    ev_label = "POSITIVE EV" if result.ev_percent > 0 else "NEGATIVE EV"
    sign = "+" if result.ev_percent >= 0 else ""

    print("\n" + "="*w)
    print(f"  {result.player_name}  —  {result.stat.replace('_',' ').title()}")
    print(f"  {result.side.upper()} {result.line}  |  "
          f"{result.american_odds:+d}  |  {result.book.upper()}")
    print("="*w)

    print(f"\n  HISTORICAL  (last {result.sample_size} games, recent seasons only)")
    print(f"  |- Average:      {result.mean_stat:.1f}")
    print(f"  |- Std dev:      {result.std_stat:.1f}")
    print(f"  '- Hit rate:     {result.hit_rate_signal:.1%}")

    matchup_ratio = result.opponent_avg  # already team-level
    print(f"\n  MATCHUP vs {result.opponent}  (team-level per game)")
    print(f"  |- Opp allows:   {matchup_ratio:.1f} / game to this position")
    opp_tag = "favorable" if result.defense_signal > result.model_signal + 0.02 \
              else "tough" if result.defense_signal < result.model_signal - 0.02 \
              else "neutral"
    print(f"  '- Matchup:      {opp_tag}")

    print(f"\n  8-SIGNAL BREAKDOWN")
    print(f"  |- Hit rate:      {result.hit_rate_signal:.1%}   (w 18%)")
    print(f"  |- Model:         {result.model_signal:.1%}   (w 18%)")
    print(f"  |- Defense adj:   {result.defense_signal:.1%}   (w 15%)")
    print(f"  |- Prop tracker:  {result.prop_tracker_signal:.1%}   (w 14%)")
    print(f"  |- Roster:        {result.roster_mult:.2f}x  (w 12%)  "
          f"{result.roster_details.get('injury_status','')}")
    print(f"  |- Snap trend:    {result.roster_details.get('snap_trend','-')}")
    print(f"  |- Coaching/pace: {result.coaching_mult:.2f}x  (w 10%)")
    print(f"  |- Weather:       {result.weather_mult:.2f}x  (w  8%)  {result.weather_desc}")
    print(f"  '- Officials:     {result.official_mult:.2f}x  (w  5%)")

    print(f"\n  VERDICT  —  {ev_label}")
    print(f"  |- TRUE PROB:    {result.true_probability:.1%}")
    print(f"  |- Book implied: {result.implied_probability:.1%}  (w/ vig)")
    print(f"  |- Book fair:    {result.no_vig_probability:.1%}  (no vig)")
    print(f"  |- Edge:         {sign}{result.edge:.1%}")
    print(f"  |- EV%:          {sign}{result.ev_percent:.2f}%")
    print(f"  |- Kelly size:   {result.kelly_fraction:.1%} of bankroll")
    print(f"  '- Grade:        {result.grade()}")
    print("="*w + "\n")
