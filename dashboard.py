"""
dashboard.py — WinWeave Streamlit Dashboard

Top-level structure:

  🏈 NFL     — Player Explorer + EV Scanner for football
  ⚾ MLB     — Player Explorer + EV Scanner for baseball
  📋 Tracker — pending predictions, log results, accuracy report
               (shared across both sports)
  💰 Live Odds — latest lines pulled by the SGO scraper, with a
                 clickable "Bet" link straight to the sportsbook
                 when SGO provides one (shared across both sports)

HOW TO RUN:
    cd ~/WinWeave
    source .venv/bin/activate
    pip install streamlit          # first time only
    streamlit run dashboard.py

Then open http://localhost:8501 in your browser (it usually
opens automatically). Press Ctrl+C in the terminal to stop.
"""

import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.prop_analyzer import (
    analyze_prop,
    get_player_recent_stats,
    get_opponent_stat_allowed,
    get_league_stat_allowed,
)
from src.mlb_analyzer import (
    analyze_mlb_prop,
    MLB_STATS,
)
from src.factors.prop_tracker import (
    ensure_prop_results_table,
    save_prediction,
    log_result,
    get_player_track_record,
    get_all_player_track_records,
    get_bankroll_summary,
    get_bankroll_by_book,
    add_bank_transaction,
    set_starting_balance,
    get_prediction,
    edit_prediction,
    auto_resolve_pending_bets,
)
from scan_live_mlb_props import scan_mlb_props, local_date
from scan_live_game_markets import scan_game_markets
from scan_live_nfl_props import scan_nfl_props
from scrapers.sgo_scraper import run_scrape, TARGET_BOOKS

DB_PATH = PROJECT_ROOT / "data" / "winweave.db"

# Columns in the props table that are metadata, not statistics —
# excluded from the auto-detected stat list.
_NON_STAT_COLS = {
    "player_id", "player_name", "player_display_name", "position",
    "position_group", "headshot_url", "season", "week", "season_type",
    "recent_team", "team", "opponent_team", "game_id",
}

# Curated stat lists per position group, in priority order. Built
# from whatever columns actually exist in props — never hardcoded
# blind, so this stays correct even if the schema changes later.
_POSITION_STAT_PRIORITY = {
    "QB": ["passing_yards", "passing_tds", "interceptions", "completions",
           "attempts", "sacks", "rushing_yards", "rushing_tds"],
    "RB": ["rushing_yards", "rushing_tds", "carries", "receiving_yards",
           "receptions", "targets", "receiving_tds"],
    "WR": ["receiving_yards", "receptions", "targets", "receiving_tds",
           "rushing_yards"],
    "TE": ["receiving_yards", "receptions", "targets", "receiving_tds"],
}

# Positions this database does not carry individual stats for.
# props/player_stats here is an offense/skill-position weekly feed
# (the same source nflreadr's load_player_stats() provides) — it
# does not include defender-recorded tackles, sacks, or INTs.
_DEFENSIVE_POSITIONS = {
    "CB", "S", "SS", "FS", "LB", "ILB", "OLB", "DE", "DT", "NT", "DL", "DB",
}

NFL_TEAMS = [
    "ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN",
    "DET","GB","HOU","IND","JAX","KC","LA","LAC","LV","MIA","MIN",
    "NE","NO","NYG","NYJ","PHI","PIT","SEA","SF","TB","TEN","WAS",
]

MLB_TEAMS = [
    "Arizona Diamondbacks","Atlanta Braves","Baltimore Orioles",
    "Boston Red Sox","Chicago Cubs","Chicago White Sox",
    "Cincinnati Reds","Cleveland Guardians","Colorado Rockies",
    "Detroit Tigers","Houston Astros","Kansas City Royals",
    "Los Angeles Angels","Los Angeles Dodgers","Miami Marlins",
    "Milwaukee Brewers","Minnesota Twins","New York Mets",
    "New York Yankees","Athletics","Philadelphia Phillies",
    "Pittsburgh Pirates","San Diego Padres","San Francisco Giants",
    "Seattle Mariners","St. Louis Cardinals","Tampa Bay Rays",
    "Texas Rangers","Toronto Blue Jays","Washington Nationals",
]

# Bettable MLB stats for the EV Scanner dropdown (excludes nothing —
# all MLB_STATS keys are real props with a live market on SGO)
MLB_BETTABLE_STATS = list(MLB_STATS.keys())


@st.cache_data(ttl=3600)
def all_mlb_player_names() -> list[str]:
    df = query_df("""
        SELECT DISTINCT full_name FROM mlb_players
        WHERE full_name IS NOT NULL ORDER BY full_name
    """)
    return df["full_name"].tolist()


@st.cache_data(ttl=3600)
def mlb_player_position(player: str) -> str:
    df = query_df("""
        SELECT position FROM mlb_players
        WHERE full_name = ? LIMIT 1
    """, (player,))
    return df["position"].iloc[0] if not df.empty else "?"


@st.cache_data(ttl=3600)
def mlb_player_seasons(player: str, table: str) -> list[int]:
    df = query_df(f"""
        SELECT DISTINCT season FROM {table}
        WHERE full_name = ? ORDER BY season DESC
    """, (player,))
    return df["season"].tolist()

# ── Helpers ────────────────────────────────────────────────────

def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


@st.cache_data(ttl=3600)
def all_player_names() -> list[str]:
    df = query_df("""
        SELECT DISTINCT player_display_name
        FROM props
        WHERE player_display_name IS NOT NULL
        ORDER BY player_display_name
    """)
    return df["player_display_name"].tolist()


@st.cache_data(ttl=3600)
def player_seasons(player: str) -> list[int]:
    df = query_df("""
        SELECT DISTINCT season FROM props
        WHERE player_display_name = ?
        ORDER BY season DESC
    """, (player,))
    return df["season"].tolist()


@st.cache_data(ttl=3600)
def available_stat_columns() -> list[str]:
    """
    Auto-detects every real statistic column in the props table,
    rather than relying on a hardcoded list. If the database gains
    new columns after a refresh (interceptions, carries, targets —
    all of which already exist in this schema), the dashboard picks
    them up automatically with no code change needed.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(props)").fetchall()]
    finally:
        conn.close()
    return [c for c in cols if c not in _NON_STAT_COLS]


def stats_for_position(position: str) -> list[str]:
    """
    Returns the relevant stat columns for a position, ordered by
    relevance, filtered to only columns that actually exist.
    """
    available = available_stat_columns()
    priority = _POSITION_STAT_PRIORITY.get(position, [])
    ordered = [s for s in priority if s in available]
    leftovers = [s for s in available if s not in ordered]
    return ordered + leftovers


def is_defensive_position(position: str) -> bool:
    return position in _DEFENSIVE_POSITIONS


def scan_mlb_combined():
    """Player props + game-level markets (moneyline / run line /
    total) merged into one feed for the MLB Top Picks board. Game
    markets come from the same API pull, so this costs nothing
    extra. Diagnostics are merged; a props-side error still shows
    game markets and vice versa."""
    prop_results, prop_diag = scan_mlb_props()
    game_results, game_diag = scan_game_markets()
    results = list(prop_results) + list(game_results)
    diag = {
        "error": (prop_diag.get("error")
                  if prop_diag.get("error") and game_diag.get("error")
                  else None),
        "available_dates": sorted(
            set(prop_diag.get("available_dates") or []) |
            set(game_diag.get("available_dates") or [])),
        "single_sided_skipped":
            (prop_diag.get("single_sided_skipped") or 0) +
            (game_diag.get("single_sided_skipped") or 0),
        "unmatched_players": prop_diag.get("unmatched_players") or [],
        "unmatched_teams": sorted(
            set(prop_diag.get("unmatched_teams") or []) |
            set(game_diag.get("unmatched_teams") or [])),
    }
    return results, diag


# Human labels for the market-focus filter. Player-prop stats are
# labeled by their stat name; game-level markets get explicit names.
_MARKET_LABELS = {
    "moneyline":  "Moneyline (game)",
    "run_line":   "Run line (game)",
    "total_runs": "Game total (game)",
}


def _market_label(stat: str) -> str:
    return _MARKET_LABELS.get(stat, stat.replace("_", " ").title())


def render_top_picks(sport_label: str, scan_fn, key_prefix: str):
    """
    Shared "what's the best bet right now" leaderboard for a sport.
    Filters to props that are BOTH positive EV AND above a minimum
    hit-rate threshold — a technically +EV 8%-probability longshot
    and a 70%-probability near-lock with a small edge are very
    different kinds of bets, and this view is meant to surface the
    latter first, not just whatever has the single highest EV number.
    """
    f1, f2 = st.columns([1, 1])
    with f1:
        min_hit_rate_pct = st.slider(
            "Minimum hit rate (%)", min_value=40, max_value=80,
            value=55, step=5, key=f"{key_prefix}_min_hitrate",
        )
        min_hit_rate = min_hit_rate_pct / 100
        _HIT_RATE_GUIDE = {
            40: "**40-50%:** coin-flip territory. Only the payout size "
                "makes these +EV — a real edge, but high variance. "
                "Expect to lose more often than you win on any single bet.",
            50: "**50-55%:** slightly better than a coin flip. Still "
                "leans on decent odds to be worthwhile; variance is high.",
            55: "**55-65%:** a reasonable middle ground — noticeably "
                "more likely to hit than not, without needing to hit "
                "every time to be worth it.",
            60: "**55-65%:** a reasonable middle ground — noticeably "
                "more likely to hit than not, without needing to hit "
                "every time to be worth it.",
            65: "**65-75%:** fairly reliable picks. These should hit "
                "more often than not by a solid margin, at the cost of "
                "generally smaller EV% per bet.",
            70: "**65-75%:** fairly reliable picks. These should hit "
                "more often than not by a solid margin, at the cost of "
                "generally smaller EV% per bet.",
            75: "**75%+:** near-locks. Very likely to hit, but the "
                "market usually prices these tightly too — expect "
                "modest EV% even when the edge is real.",
            80: "**75%+:** near-locks. Very likely to hit, but the "
                "market usually prices these tightly too — expect "
                "modest EV% even when the edge is real.",
        }
        st.caption(_HIT_RATE_GUIDE.get(min_hit_rate_pct,
                                       _HIT_RATE_GUIDE[55]))
    with f2:
        top_n = st.number_input(
            "How many to show", min_value=1, max_value=500,
            value=10, step=5, key=f"{key_prefix}_top_n",
            help="Set this high (e.g. 500) to effectively show all "
                 "qualifying props.",
        )
        st.caption(f"Filters to props that are both +EV and at least "
                   f"{min_hit_rate_pct}% likely to hit, ranked by EV%.")

    results, diag = scan_fn()

    if diag["error"]:
        st.info(diag["error"])
        return

    # Market focus: scan only what you care about. Options are built
    # from what's actually in this pull, so player-prop stats and
    # game-level markets (moneyline / run line / total) all appear
    # once they exist in the odds data. Empty selection = everything.
    stats_present = sorted({r.stat for r in results},
                           key=lambda s: (s in _MARKET_LABELS, s))
    focus = st.multiselect(
        "Market focus (empty = all markets)",
        options=stats_present,
        format_func=_market_label,
        key=f"{key_prefix}_market_focus",
        help="Pick one or more markets to scan solely for those — "
             "e.g. just Strikeouts, or just game Moneylines.",
    )
    if focus:
        results = [r for r in results if r.stat in focus]

    # X-grade (audit) picks are excluded from Top Picks by design:
    # positive EV they may show, but the grade itself says "trace
    # this before trusting it" — a leaderboard is the wrong place
    # for un-audited numbers. Find them in the EV Scanner tab.
    qualifying = [r for r in results
                 if r.ev_percent > 0
                 and r.true_probability >= min_hit_rate
                 and not r.grade().startswith("X")]

    # Dedup: the same real prop (player, stat, line, side) often shows
    # up once per book, each with slightly different odds. A real user
    # caught this — three of the "top 10" were literally the same bet
    # at three different sportsbooks, which isn't 10 different
    # opportunities, it's one opportunity shown 3 times. Collapse each
    # unique prop down to its single best-priced book, but keep count
    # of how many books offer it so that information isn't lost.
    best_per_prop: dict = {}
    book_counts: dict = {}
    for r in qualifying:
        key = (r.player_name, r.stat, r.line, r.side)
        book_counts[key] = book_counts.get(key, 0) + 1
        current_best = best_per_prop.get(key)
        if current_best is None or r.ev_percent > current_best.ev_percent:
            best_per_prop[key] = r

    deduped = list(best_per_prop.values())
    deduped.sort(key=lambda r: r.ev_percent, reverse=True)
    top_picks = deduped[:top_n]

    if not top_picks:
        st.warning(
            f"No {sport_label} props currently meet both filters "
            f"(+EV and \u2265{int(min_hit_rate*100)}% hit rate) out of "
            f"{len(results)} analyzed. Try lowering the minimum hit rate."
        )
    else:
        st.caption(f"{len(qualifying)} qualifying lines across all books "
                   f"({len(deduped)} distinct props) out of {len(results)} "
                   f"analyzed \u2014 showing the top {len(top_picks)}")

        st.markdown("""
        <div style="display:flex;align-items:center;gap:18px;
                    padding:0 20px 4px 20px;">
            <div style="width:22px;flex-shrink:0;"></div>
            <div style="flex:1;min-width:0;font-size:12px;color:#8B92A0;
                        text-transform:uppercase;letter-spacing:0.04em;">
                Player / Prop / Matchup
            </div>
            <div style="min-width:28px;flex-shrink:0;font-size:12px;
                        color:#8B92A0;text-transform:uppercase;
                        letter-spacing:0.04em;">Grade</div>
            <div style="min-width:90px;flex-shrink:0;text-align:right;
                        font-size:12px;color:#8B92A0;text-transform:uppercase;
                        letter-spacing:0.04em;">EV%</div>
        </div>
        """, unsafe_allow_html=True)
        tracked_keys = get_tracked_pick_keys()
        n_untracked = sum(1 for r in top_picks
                          if _pick_key(r) not in tracked_keys)
        bc1, bc2 = st.columns([1.4, 2.6])
        with bc1:
            if n_untracked and st.button(
                    f"📋 Paper-track all {n_untracked} untracked",
                    key=f"{key_prefix}_bulk_track"):
                added = 0
                for r in top_picks:
                    if _pick_key(r) in tracked_keys:
                        continue
                    if hasattr(r, "tracker_encoding"):
                        enc = r.tracker_encoding()
                    else:
                        enc = {"player_name": r.player_name,
                               "stat": r.stat, "line": r.line,
                               "side": r.side,
                               "opponent": getattr(r, "opponent", "") or "",
                               "game_date": local_date(
                                   getattr(r, "starts_at", None) or ""
                               ) or None}
                    save_prediction(
                        **enc, book=r.book,
                        american_odds=r.american_odds,
                        season=datetime.now().year,
                        week=getattr(r, "week", 0) or 0,
                        predicted_prob=r.true_probability,
                        ev_percent=r.ev_percent,
                        kelly_fraction=getattr(r, "kelly_fraction", 0.0),
                        grade=r.grade(), bet_placed=False, stake=None)
                    added += 1
                get_tracked_pick_keys.clear()
                st.success(f"Paper-tracked {added} pick(s) — they'll "
                          "auto-resolve after the games.")
                st.rerun()
        with bc2:
            if n_untracked == 0:
                st.caption("✓ Everything shown is already tracked.")
            else:
                st.caption("One click logs every shown pick as a $0 "
                          "paper prediction (dupes skipped) — your "
                          "top-10-per-load routine, automated.")

        for i, r in enumerate(top_picks, 1):
            n_books = book_counts[(r.player_name, r.stat, r.line, r.side)]
            book_note = f"{r.book}" + (f" (best of {n_books} books)"
                                        if n_books > 1 else "")
            is_game_market = hasattr(r, "market")  # GameAnalysis objects
            if is_game_market:
                detail = (f"{r.describe()}  |  {r.american_odds:+d}  |  "
                         f"{book_note}  |  "
                         f"win prob {r.true_probability:.0%}  |  "
                         f"{r.sample_size} team-game sample")
            else:
                detail = (f"{r.stat.replace('_',' ').title()} {r.side.upper()} "
                         f"{r.line}  |  {r.american_odds:+d}  |  {book_note}  |  "
                         f"hit rate {r.true_probability:.0%}  |  "
                         f"{r.sample_size} game sample")
            render_pick_card(
                rank=i, player=r.player_name, detail=detail,
                matchup=getattr(r, "matchup", ""),
                ev_percent=r.ev_percent, grade_str=r.grade(),
                deeplink=getattr(r, "deeplink", None),
                mean_stat=getattr(r, "mean_stat", None),
                std_stat=getattr(r, "std_stat", None),
                sample_size=r.sample_size,
                stat_label=r.stat.replace("_", " "),
                result_obj=r,
                card_key=f"{key_prefix}_{i}_{r.player_name}_{r.stat}",
                already_tracked=_pick_key(r) in tracked_keys,
                headshot_url=get_headshot_url(r.player_name),
            )

    with st.expander("Diagnostics (unmatched players/teams, skipped markets)"):
        if diag["available_dates"]:
            st.caption(f"Game dates in this pull: "
                      f"{', '.join(diag['available_dates'])}")
        st.caption(f"{len(results)} total props fully analyzed \u2022 "
                  f"{diag['single_sided_skipped']} skipped (one-sided odds) \u2022 "
                  f"{len(diag['unmatched_players'])} unmatched player names \u2022 "
                  f"{len(diag['unmatched_teams'])} unresolved team/roster issues")
        if diag["unmatched_players"]:
            st.write("**Unmatched players:**")
            st.write(sorted(diag["unmatched_players"]))
        if diag["unmatched_teams"]:
            st.write("**Unresolved teams/rosters:**")
            st.write(sorted(diag["unmatched_teams"]))


# ── Page config ────────────────────────────────────────────────

st.set_page_config(page_title="WinWeave", page_icon="assets/logo_icon.png", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
    --wv-bg:        #0B0E11;
    --wv-surface:   #14181D;
    --wv-border:    #262B33;
    --wv-text:      #E8E6E1;
    --wv-text-dim:  #8B92A0;
    --wv-amber:     #FFA53D;
    --wv-green:     #3ECF8E;
    --wv-red:       #E5484D;
    --wv-gray:      #565E6B;
}

/* Numbers read like a board — everywhere a stat, odd, or % appears */
.wv-mono { font-family: 'IBM Plex Mono', monospace; }
body, .stApp, p, div, span, label { font-family: 'IBM Plex Sans', sans-serif; }

h1, h2, h3 { font-family: 'IBM Plex Sans', sans-serif; font-weight: 600; letter-spacing: -0.01em; }

/* ── Pick card: the signature element ───────────────────────── */
.pick-card {
    display: flex;
    align-items: center;
    gap: 18px;
    background: var(--wv-surface);
    border: 1px solid var(--wv-border);
    border-left: 5px solid var(--wv-gray);
    border-radius: 8px;
    padding: 14px 20px;
    margin-bottom: 10px;
}
.pick-card.grade-a { border-left-color: var(--wv-green); }
.pick-card.grade-b { border-left-color: var(--wv-amber); }
.pick-card.grade-c { border-left-color: var(--wv-gray); }
.pick-card.grade-f { border-left-color: var(--wv-red); }

.pick-rank {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    color: var(--wv-text-dim);
    width: 22px;
    flex-shrink: 0;
}
.pick-main { flex: 1; min-width: 0; }
.pick-player { font-size: 16px; font-weight: 600; color: var(--wv-text); }
.pick-detail { font-size: 13px; color: var(--wv-text-dim); margin-top: 2px; }
.pick-matchup { font-size: 12px; color: var(--wv-text-dim); margin-top: 2px; }

.pick-ev {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 26px;
    font-weight: 700;
    text-align: right;
    flex-shrink: 0;
    min-width: 90px;
}
.pick-ev.pos { color: var(--wv-green); text-shadow: 0 0 14px rgba(62,207,142,0.35); }
.pick-ev.neg { color: var(--wv-red); }

.pick-grade-chip {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 4px;
    flex-shrink: 0;
    text-align: center;
    min-width: 28px;
}
.pick-grade-chip.grade-a { background: rgba(62,207,142,0.15); color: var(--wv-green); }
.pick-grade-chip.grade-b { background: rgba(255,165,61,0.15); color: var(--wv-amber); }
.pick-grade-chip.grade-c { background: rgba(86,94,107,0.25); color: var(--wv-text-dim); }
.pick-grade-chip.grade-f { background: rgba(229,72,77,0.15); color: var(--wv-red); }

.pick-bet-link {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--wv-amber);
    text-decoration: none;
    border: 1px solid var(--wv-amber);
    border-radius: 4px;
    padding: 4px 10px;
    white-space: nowrap;
    flex-shrink: 0;
}
.pick-bet-link:hover { background: rgba(255,165,61,0.1); }
</style>
""", unsafe_allow_html=True)


def _grade_class(grade_str: str) -> str:
    letter = grade_str.strip()[0].lower() if grade_str else "c"
    return f"grade-{letter}" if letter in "abcf" else "grade-c"


def _pick_key(r) -> tuple:
    """Identity of a pick as the TRACKER stores it (game markets use
    their margin-based encoding), so checkmarks and dedupe agree with
    what save_prediction actually writes."""
    if hasattr(r, "tracker_encoding"):
        e = r.tracker_encoding()
        return (e["player_name"], e["stat"], round(float(e["line"]), 2),
                e["side"])
    return (r.player_name, r.stat, round(float(r.line), 2), r.side)


@st.cache_data(ttl=120)
def get_tracked_pick_keys() -> set:
    """Keys of every still-pending tracked prediction — used for the
    green checkmark on cards and to skip dupes in bulk tracking."""
    df = query_df("""
        SELECT player_name, stat, line, side FROM prop_results
        WHERE result IS NULL
    """)
    return {(r.player_name, r.stat, round(float(r.line), 2), r.side)
            for r in df.itertuples()}


@st.cache_data(ttl=3600)
def get_headshot_url(player_name: str):
    """Player headshot, zero-config: NFL from the headshot_url column
    nflverse already ships inside props; MLB from the public MLB
    static CDN keyed by mlb_players.player_id. Teams/parlays: None."""
    df = query_df("""
        SELECT headshot_url FROM props
        WHERE player_display_name = ? AND headshot_url IS NOT NULL
        ORDER BY season DESC LIMIT 1
    """, (player_name,))
    if not df.empty and df.iloc[0, 0]:
        return str(df.iloc[0, 0])
    df = query_df(
        "SELECT player_id FROM mlb_players WHERE full_name = ? LIMIT 1",
        (player_name,))
    if not df.empty and df.iloc[0, 0]:
        pid = int(df.iloc[0, 0])
        return ("https://img.mlbstatic.com/mlb-photos/image/upload/"
                "w_120,q_100/v1/people/%d/headshot/67/current" % pid)
    return None


def render_pick_card(rank: int, player: str, detail: str, matchup: str,
                     ev_percent: float, grade_str: str,
                     deeplink: str = None, mean_stat: float = None,
                     std_stat: float = None, sample_size: int = None,
                     stat_label: str = "", result_obj=None,
                     card_key: str = "",
                     already_tracked: bool = False,
                     headshot_url: str = None):
    """Renders one Top Pick as an odds-board-style card: colored edge
    bar by grade, big glowing mono EV%, optional direct bet link.
    A popover above the card lets you sanity-check the player without
    leaving the page, see their tracked track record if one exists,
    and log this pick into the Tracker (as a real wager or just to
    check model accuracy) without needing the EV Scanner tab."""
    gclass = _grade_class(grade_str)
    ev_class = "pos" if ev_percent >= 0 else "neg"
    sign = "+" if ev_percent >= 0 else ""
    grade_letter = grade_str.strip()[0] if grade_str else "?"
    bet_html = (f'<a class="pick-bet-link" href="{deeplink}" target="_blank">'
                f'Place bet ↗</a>') if deeplink else ""

    with st.popover(f"ℹ️ {player}", use_container_width=False):
        st.markdown(f"**{player}**")
        if mean_stat is not None and sample_size:
            st.write(f"Averaging **{mean_stat:.2f}** {stat_label} per game "
                    f"over the last **{sample_size}** games "
                    f"(± {std_stat:.2f} std dev).")
        st.caption("This is the same sample the model used to compute "
                  "the probability below — if the sample size looks "
                  "thin, treat the grade with extra caution.")

        track = get_player_track_record(player, min_sample=3)
        if track:
            st.markdown(
                f"**Tracked history:** {track['hits']}/{track['total']} hit "
                f"({track['hit_rate']:.0%})"
                + (f"  |  real bets: {track['bets_placed']}, "
                   f"net ${track['net_profit']:+.2f}"
                   if track['bets_placed'] else "")
            )
        else:
            st.caption("No tracked history yet for this player "
                      "(need 3+ logged results).")

        if result_obj is not None:
            st.divider()
            if already_tracked:
                st.success("✓ Already in the tracker (pending result) — "
                          "saving again is dedupe-protected, but you "
                          "don't need to.")
            st.caption("Track this pick")
            is_real = st.checkbox("This is a real bet", key=f"real_{card_key}")
            stake_val = None
            if is_real:
                stake_val = st.number_input(
                    "Stake ($)", min_value=0.0, value=5.0, step=1.0,
                    key=f"stake_{card_key}",
                )
            if st.button("Save to Tracker", key=f"track_{card_key}"):
                # Game-level picks (GameAnalysis) carry a
                # tracker_encoding(): a margin-based over/under scheme
                # that lets the existing tracker + auto-resolve grade
                # moneylines/run lines/totals without schema changes.
                # Saving the raw home/away side instead would break
                # the hit logic — always prefer the encoding.
                if hasattr(result_obj, "tracker_encoding"):
                    enc = result_obj.tracker_encoding()
                else:
                    enc = {
                        "player_name": result_obj.player_name,
                        "stat": result_obj.stat,
                        "line": result_obj.line,
                        "side": result_obj.side,
                        "opponent": result_obj.opponent,
                        "game_date": local_date(
                            getattr(result_obj, "starts_at", None) or ""
                        ) or None,
                    }
                row_id = save_prediction(
                    **enc,
                    book=result_obj.book,
                    american_odds=result_obj.american_odds,
                    season=datetime.now().year,
                    week=getattr(result_obj, "week", 0) or 0,
                    predicted_prob=result_obj.true_probability,
                    ev_percent=result_obj.ev_percent,
                    kelly_fraction=result_obj.kelly_fraction,
                    grade=result_obj.grade(),
                    bet_placed=is_real, stake=stake_val if is_real else None,
                )
                st.success(f"Saved as prediction #{row_id}. Log the "
                          f"result in the Tracker tab once the game "
                          f"is final.")

    bet_note = "" if deeplink else (
        '<div style="font-size:11px;color:#565E6B;margin-top:2px;">'
        'No direct bet link for this book/market (not all sportsbooks '
        'share one via the odds provider)</div>'
    )
    # Built as a single joined string rather than a triple-quoted f-string
    # with placeholders on their own lines: when bet_note or bet_html is
    # empty, a placeholder left alone on its own line becomes a genuinely
    # blank line, and a blank line in the middle of a raw HTML block
    # breaks Streamlit's HTML parsing -- the rest of the card then
    # renders as literal escaped text instead of markup. Found this by
    # noticing the bug only ever hit cards that HAD a deeplink (meaning
    # bet_note="" was the trigger), then confirming it by running the
    # exact template through markdown's HTML-block logic directly.
    head_html = ""
    if headshot_url:
        head_html = ('<img src="' + headshot_url + '" style="width:34px;'
                     'height:34px;border-radius:50%;object-fit:cover;'
                     'vertical-align:middle;margin-right:8px;'
                     'border:1px solid #2A313C;"/>')
    tracked_html = ""
    if already_tracked:
        tracked_html = ('<span style="font-size:11px;color:#3ECF8E;'
                        'border:1px solid rgba(62,207,142,0.4);'
                        'border-radius:10px;padding:1px 8px;'
                        'margin-left:8px;vertical-align:middle;">'
                        '&#10003; tracked</span>')
    card_html = (
        f'<div class="pick-card {gclass}">'
        f'<div class="pick-rank wv-mono">#{rank}</div>'
        f'<div class="pick-main">'
        f'<div class="pick-player">{head_html}{player}{tracked_html}</div>'
        f'<div class="pick-detail wv-mono">{detail}</div>'
        f'<div class="pick-matchup">{matchup}</div>'
        f'{bet_note}'
        f'</div>'
        f'<div class="pick-grade-chip {gclass}">{grade_letter}</div>'
        f'<div class="pick-ev {ev_class}">{sign}{ev_percent:.1f}%</div>'
        f'{bet_html}'
        f'</div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)


st.image("assets/logo_full.png", width=320)
st.caption("Personal betting intelligence — historical data, live odds, +EV detection")

if not DB_PATH.exists():
    st.error(f"Database not found at {DB_PATH}. "
             f"Copy winweave.db into the data/ folder and refresh.")
    st.stop()

with st.container(border=True):
    rc1, rc2, rc3 = st.columns([1, 2, 1])
    with rc1:
        refresh_league = st.selectbox("League", ["mlb", "nfl"],
                                      key="refresh_league")
    with rc2:
        refresh_books = st.multiselect(
            "Books to scan", TARGET_BOOKS, default=TARGET_BOOKS,
            key="refresh_books",
            help="Uncheck any book you don't have an account at.",
        )
    with rc3:
        st.write("")
        st.write("")
        refresh_clicked = st.button("🔄 Pull Fresh Odds", type="primary",
                                    width='stretch')

    if refresh_clicked:
        status_box = st.empty()
        status_box.info("Starting...")
        result = run_scrape(
            refresh_league, books=refresh_books or None,
            progress_callback=lambda msg: status_box.info(msg),
        )
        status_box.empty()
        if not result["success"]:
            st.error(result["error"])
        elif result["n_rows"] == 0:
            st.warning(result["error"] or "No odds found for this pull.")
        else:
            usage_note = ""
            if result["usage_max"] == "unlimited":
                usage_note = " | SGO usage: unlimited"
            elif result["usage_remaining"] is not None:
                usage_note = (f" | SGO usage: {result['usage_remaining']:,} "
                              f"of {result['usage_max']:,} remaining")
            game_note = ""
            if result.get("n_game_rows"):
                game_note = (f" + {result['n_game_rows']:,} game-market "
                             f"lines (ML/RL/totals)")
            st.success(
                f"Pulled {result['n_rows']:,} prop lines{game_note} "
                f"across {result['n_events']} games "
                f"({len(result['books'])} book(s)).{usage_note}"
            )
            with st.expander("Games in this pull"):
                for g in result["games"]:
                    st.write(f"- {g}")
            st.cache_data.clear()
            st.rerun()

def ensure_live_odds_columns():
    """
    Makes sure live_odds has the 'league' and 'deeplink' columns,
    regardless of whether the scraper has been re-run since they
    were added. The dashboard can be opened without ever running
    the scraper first — a real user hit exactly this: an existing
    live_odds table from before these columns existed, opened
    straight in the dashboard, with no scraper run in between to
    trigger the migration that normally lives in sgo_scraper.py.
    This mirrors that same migration here so the dashboard never
    depends on the scraper having run first.
    """
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "live_odds" not in tables:
            return  # nothing to migrate yet — Live Odds tab handles this
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
        conn.commit()
    finally:
        conn.close()


ensure_prop_results_table()
ensure_live_odds_columns()

tab_nfl, tab_mlb, tab_tracker, tab_odds = st.tabs(
    ["🏈 NFL", "⚾ MLB", "📋 Tracker", "💰 Live Odds"]
)

# ══════════════════════════════════════════════════════════════
#  NFL — Player Explorer + EV Scanner
# ══════════════════════════════════════════════════════════════

with tab_nfl:
    tab_top, tab_explorer, tab_scanner = st.tabs(
        ["🏆 Top Picks", "🔎 Player Explorer", "📈 EV Scanner"]
    )

    with tab_top:
        st.subheader("Best NFL props right now")
        st.caption("Built from your live_odds table. NFL props typically "
                   "don't appear until ~1-2 weeks before games (August "
                   "preseason onward) — this is unvalidated against real "
                   "NFL data since the season hasn't started yet.")
        render_top_picks("NFL", scan_nfl_props, "nfl_top")

    with tab_explorer:
        # ── A-Z filter ──────────────────────────────────────────────
        st.caption("Filter by first letter, then search")
        letters = ["All"] + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        letter_sel = st.radio("Letter", letters, horizontal=True,
                              label_visibility="collapsed", key="az_filter")

        all_names = all_player_names()
        if letter_sel != "All":
            filtered_names = [n for n in all_names if n.upper().startswith(letter_sel)]
            search_placeholder = f"{len(filtered_names)} players starting with {letter_sel}"
        else:
            filtered_names = all_names
            search_placeholder = "Start typing a name — e.g. Jalen Hurts"

        col_a, col_b = st.columns([2, 1])
        with col_a:
            player = st.selectbox(
                "Search a player",
                options=filtered_names,
                index=None,
                placeholder=search_placeholder,
            )

        if player:
            seasons = player_seasons(player)
            with col_b:
                season_sel = st.selectbox("Season", seasons, index=0)

            # ── Header metrics ─────────────────────────────────────
            meta = query_df("""
                SELECT position, MAX(season) AS latest
                FROM props WHERE player_display_name = ?
            """, (player,))
            position = meta["position"].iloc[0] if not meta.empty else "?"

            if is_defensive_position(position):
                st.warning(
                    f"**{player}** is listed as {position}. This database's "
                    f"props table is an offense/skill-position weekly feed "
                    f"(the same source used for player prop betting), so it "
                    f"does not include defender-recorded tackles, sacks, or "
                    f"interceptions. Only QB/RB/WR/TE stats are populated "
                    f"here — defensive stats would need to be built "
                    f"separately from the play-by-play table."
                )

            season_stats = query_df("""
                SELECT COUNT(*) AS games,
                       ROUND(AVG(passing_yards),1)   AS pass_avg,
                       ROUND(AVG(rushing_yards),1)   AS rush_avg,
                       ROUND(AVG(receiving_yards),1) AS recv_avg,
                       ROUND(AVG(receptions),1)      AS rec_avg
                FROM props
                WHERE player_display_name = ? AND season = ?
            """, (player, season_sel))

            m = season_stats.iloc[0]
            games_in_db = int(m["games"] or 0)
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("Position", position)
            c2.metric("Games", games_in_db)
            c3.metric("Pass yds/g", m["pass_avg"] or 0)
            c4.metric("Rush yds/g", m["rush_avg"] or 0)
            c5.metric("Recv yds/g", m["recv_avg"] or 0)
            c6.metric("Rec/g", m["rec_avg"] or 0)

            if 0 < games_in_db < 8 and season_sel < 2026:
                st.info(
                    f"Only {games_in_db} games in the database for "
                    f"{season_sel}. If {player} actually played more than "
                    f"this, the database is behind and needs a refresh — "
                    f"see `python refresh_season_data.py --season {season_sel}`."
                )

            # ── Game log + chart ───────────────────────────────────
            st.subheader(f"Game log — {season_sel}")

            stat_cols_for_position = stats_for_position(position)
            select_cols = ", ".join(stat_cols_for_position) if stat_cols_for_position else "week"
            log = query_df(f"""
                SELECT week, opponent_team AS opp, {select_cols}
                FROM props
                WHERE player_display_name = ? AND season = ?
                ORDER BY week
            """, (player, season_sel))

            chart_stat = st.selectbox(
                "Chart stat", stat_cols_for_position,
                index=0 if stat_cols_for_position else None,
            )
            if not log.empty and chart_stat and chart_stat in log.columns:
                chart_df = log.set_index("week")[[chart_stat]].dropna()
                if not chart_df.empty:
                    st.bar_chart(chart_df, height=240)

            st.dataframe(log, width='stretch', hide_index=True)

            # ── Season averages row ─────────────────────────────────
            # Wrapped defensively: newer pandas can infer a string/Arrow
            # dtype for columns that are entirely NULL for this player
            # (common for e.g. 'interceptions' on a rookie's early games),
            # and .mean() on a string dtype raises instead of coercing.
            # pd.to_numeric(..., errors="coerce") forces real numbers and
            # turns anything unparseable into NaN instead of crashing.
            # This whole block is also try/except-guarded so a display
            # nicety can never take down the rest of the app again.
            if not log.empty and stat_cols_for_position:
                try:
                    avg_row = {"week": "AVERAGE", "opp": "\u2014"}
                    for c in stat_cols_for_position:
                        if c in log.columns:
                            numeric_col = pd.to_numeric(log[c], errors="coerce")
                            val = numeric_col.mean()
                            avg_row[c] = round(val, 1) if pd.notna(val) else None
                    st.caption("Season averages")
                    st.dataframe(pd.DataFrame([avg_row]), width='stretch',
                                 hide_index=True)
                except Exception as e:
                    st.caption(f"Season averages unavailable ({e})")

            # ── Matchup history ────────────────────────────────────
            st.subheader("Matchup history")
            opp_sel = st.selectbox("vs opponent", NFL_TEAMS, index=None,
                                   placeholder="Pick a defense")
            if opp_sel:
                vs = query_df("""
                    SELECT season, week,
                           passing_yards, rushing_yards,
                           receiving_yards, receptions
                    FROM props
                    WHERE player_display_name = ? AND opponent_team = ?
                    ORDER BY season DESC, week DESC
                """, (player, opp_sel))
                if vs.empty:
                    st.info(f"{player} has no recorded games vs {opp_sel}.")
                else:
                    st.dataframe(vs, width='stretch', hide_index=True)

            # ── Context: snaps + injuries ──────────────────────────
            col_s, col_i = st.columns(2)
            with col_s:
                st.subheader("Snap share (recent)")
                snaps = query_df("""
                    SELECT season, week,
                           ROUND(offense_pct * 100, 1) AS snap_pct
                    FROM snap_counts
                    WHERE player = ?
                    ORDER BY season DESC, week DESC LIMIT 10
                """, (player,))
                if snaps.empty:
                    st.caption("No snap data for this player.")
                else:
                    st.dataframe(snaps, width='stretch',
                                 hide_index=True)
            with col_i:
                st.subheader("Injury reports (recent)")
                inj = query_df("""
                    SELECT season, week, practice_status
                    FROM injuries
                    WHERE full_name = ?
                    ORDER BY season DESC, week DESC LIMIT 10
                """, (player,))
                if inj.empty:
                    st.caption("No injury reports — clean history.")
                else:
                    st.dataframe(inj, width='stretch',
                                 hide_index=True)

    # ══════════════════════════════════════════════════════════════
    #  TAB 2 — EV SCANNER
    # ══════════════════════════════════════════════════════════════

    with tab_scanner:
        st.subheader("8-Signal Prop Analysis")

        with st.form("ev_form"):
            f1, f2, f3 = st.columns(3)
            with f1:
                sc_player = st.selectbox("Player", all_player_names(),
                                         index=None,
                                         placeholder="Player name")
                _bettable = {"passing_yards","rushing_yards","receiving_yards",
                            "receptions","passing_tds","rushing_tds",
                            "receiving_tds","interceptions","carries",
                            "targets","completions","attempts"}
                _stat_opts = [s for s in available_stat_columns() if s in _bettable]
                if not _stat_opts:
                    _stat_opts = ["passing_yards","rushing_yards","receiving_yards"]
                sc_stat = st.selectbox("Stat", _stat_opts)
                sc_side   = st.radio("Side", ["over", "under"],
                                     horizontal=True)
            with f2:
                sc_line  = st.number_input("Line", value=0.5, step=0.5)
                sc_over  = st.number_input("Over odds",  value=-110, step=5)
                sc_under = st.number_input("Under odds", value=-110, step=5)
            with f3:
                sc_opp   = st.selectbox("Opponent", NFL_TEAMS, index=None)
                sc_book  = st.text_input("Book", value="fanduel")
                sc_season = st.number_input("Season (optional, for weather/refs)",
                                            value=0, step=1)
                sc_week   = st.number_input("Week (optional)", value=0, step=1)

            submitted = st.form_submit_button("Analyze", type="primary",
                                              width='stretch')

        if submitted:
            if not sc_player or not sc_opp:
                st.warning("Pick a player and an opponent first.")
            else:
                try:
                    r = analyze_prop(
                        player_name=sc_player, stat=sc_stat,
                        line=float(sc_line),
                        over_odds=int(sc_over), under_odds=int(sc_under),
                        opponent=sc_opp, side=sc_side, book=sc_book,
                        season=int(sc_season) or None,
                        week=int(sc_week) or None,
                    )
                    st.session_state["last_analysis"] = r
                except ValueError as e:
                    st.error(str(e))
                    st.session_state.pop("last_analysis", None)

        if "last_analysis" in st.session_state:
            r = st.session_state["last_analysis"]

            verdict = "🟢 POSITIVE EV" if r.ev_percent > 0 else "🔴 NEGATIVE EV"
            st.markdown(f"### {verdict} — grade **{r.grade()}**")

            v1, v2, v3, v4 = st.columns(4)
            v1.metric("True probability", f"{r.true_probability:.1%}")
            v2.metric("Book fair (no vig)", f"{r.no_vig_probability:.1%}",
                      delta=f"{r.edge:+.1%} edge")
            v3.metric("EV", f"{r.ev_percent:+.2f}%")
            v4.metric("Kelly bet size", f"{r.kelly_fraction:.1%}")

            st.markdown("#### Signal breakdown")
            sig = pd.DataFrame({
                "Signal": ["Hit rate", "Statistical model", "Defense-adjusted",
                           "Prop tracker", "Roster health", "Coaching/pace",
                           "Weather", "Officials"],
                "Value": [f"{r.hit_rate_signal:.1%}", f"{r.model_signal:.1%}",
                          f"{r.defense_signal:.1%}", f"{r.prop_tracker_signal:.1%}",
                          f"{r.roster_mult:.2f}x", f"{r.coaching_mult:.2f}x",
                          f"{r.weather_mult:.2f}x", f"{r.official_mult:.2f}x"],
                "Weight": ["18%","18%","15%","14%","12%","10%","8%","5%"],
                "Notes": ["blend of simple + recency-weighted",
                          f"mean {r.mean_stat:.1f} ± {r.std_stat:.1f} "
                          f"({r.sample_size} games)",
                          f"{r.opponent} allows {r.opponent_avg:.1f}/game",
                          "player line-range history",
                          r.roster_details.get("injury_status",""),
                          r.coaching_desc,
                          r.weather_desc,
                          r.official_desc],
            })
            st.dataframe(sig, width='stretch', hide_index=True)

            if st.button("💾 Track this prediction (feedback loop)"):
                row_id = save_prediction(
                    player_name=r.player_name, stat=r.stat, line=r.line,
                    side=r.side, book=r.book, american_odds=r.american_odds,
                    opponent=r.opponent,
                    season=int(sc_season) or 0, week=int(sc_week) or 0,
                    predicted_prob=r.true_probability,
                    ev_percent=r.ev_percent,
                    kelly_fraction=r.kelly_fraction, grade=r.grade(),
                    sub_signals={
                        "hit_rate": r.hit_rate_signal,
                        "model_prob": r.model_signal,
                        "weather_mult": r.weather_mult,
                        "roster_mult": r.roster_mult,
                        "coaching_mult": r.coaching_mult,
                        "official_mult": r.official_mult,
                    },
                )
                st.success(f"Saved as prediction #{row_id}. "
                           f"Log the result in the Tracker tab after the game.")

# ══════════════════════════════════════════════════════════════
#  MLB — Player Explorer + EV Scanner
# ══════════════════════════════════════════════════════════════

with tab_mlb:
    mlb_tab_top, mlb_tab_explorer, mlb_tab_scanner = st.tabs(
        ["🏆 Top Picks", "🔎 Player Explorer", "📈 EV Scanner"]
    )

    with mlb_tab_top:
        st.subheader("Best MLB props right now")
        st.caption("Built from your live_odds table — run "
                   "`python scrapers/sgo_scraper.py --league mlb` for a "
                   "fresh pull before checking here.")
        render_top_picks("MLB", scan_mlb_combined, "mlb_top")

    with mlb_tab_explorer:
        st.caption("Filter by first letter, then search")
        mlb_letters = ["All"] + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        mlb_letter_sel = st.radio("Letter", mlb_letters, horizontal=True,
                                  label_visibility="collapsed",
                                  key="mlb_az_filter")

        mlb_all_names = all_mlb_player_names()
        if mlb_letter_sel != "All":
            mlb_filtered_names = [n for n in mlb_all_names
                                  if n.upper().startswith(mlb_letter_sel)]
            mlb_placeholder = f"{len(mlb_filtered_names)} players starting with {mlb_letter_sel}"
        else:
            mlb_filtered_names = mlb_all_names
            mlb_placeholder = "Start typing a name — e.g. Aaron Judge"

        mc1, mc2 = st.columns([2, 1])
        with mc1:
            mlb_player = st.selectbox(
                "Search a player", options=mlb_filtered_names,
                index=None, placeholder=mlb_placeholder,
                key="mlb_player_select",
            )

        if mlb_player:
            position = mlb_player_position(mlb_player)
            is_pitcher = position == "P"
            table = "mlb_pitching" if is_pitcher else "mlb_batting"

            seasons = mlb_player_seasons(mlb_player, table)
            if not seasons:
                st.info(f"No {table.replace('mlb_','')} game logs found "
                        f"for {mlb_player} yet.")
            else:
                with mc2:
                    mlb_season_sel = st.selectbox("Season", seasons, index=0,
                                                  key="mlb_season_select")

                if is_pitcher:
                    stat_cols = ["outs_recorded", "strikeouts", "hits_allowed",
                                "earned_runs", "walks_allowed"]
                    summary = query_df("""
                        SELECT COUNT(*) AS games,
                               ROUND(AVG(strikeouts),1) AS k_avg,
                               ROUND(AVG(earned_runs),2) AS er_avg,
                               ROUND(AVG(outs_recorded)/3.0,1) AS ip_avg
                        FROM mlb_pitching
                        WHERE full_name = ? AND season = ?
                    """, (mlb_player, mlb_season_sel))
                    m = summary.iloc[0]
                    pc1, pc2, pc3, pc4 = st.columns(4)
                    pc1.metric("Position", position)
                    pc2.metric("Starts", int(m["games"] or 0))
                    pc3.metric("K/game", m["k_avg"] or 0)
                    pc4.metric("Avg IP", m["ip_avg"] or 0)
                else:
                    stat_cols = ["hits", "home_runs", "total_bases", "rbi",
                                "runs", "walks", "stolen_bases"]
                    summary = query_df("""
                        SELECT COUNT(*) AS games,
                               ROUND(AVG(hits),2) AS h_avg,
                               ROUND(AVG(home_runs),2) AS hr_avg,
                               ROUND(AVG(rbi),2) AS rbi_avg
                        FROM mlb_batting
                        WHERE full_name = ? AND season = ?
                    """, (mlb_player, mlb_season_sel))
                    m = summary.iloc[0]
                    pc1, pc2, pc3, pc4 = st.columns(4)
                    pc1.metric("Position", position)
                    pc2.metric("Games", int(m["games"] or 0))
                    pc3.metric("Hits/game", m["h_avg"] or 0)
                    pc4.metric("RBI/game", m["rbi_avg"] or 0)

                st.subheader(f"Game log — {mlb_season_sel}")
                select_cols = ", ".join(stat_cols)
                extra_col = "vs_starter_name" if not is_pitcher else "NULL AS vs_starter_name"
                log = query_df(f"""
                    SELECT game_date, opponent, is_home, {select_cols},
                           {extra_col}
                    FROM {table}
                    WHERE full_name = ? AND season = ?
                    ORDER BY game_date
                """, (mlb_player, mlb_season_sel))

                mlb_chart_stat = st.selectbox("Chart stat", stat_cols,
                                              key="mlb_chart_stat")
                if not log.empty and mlb_chart_stat in log.columns:
                    chart_df = log.set_index("game_date")[[mlb_chart_stat]].dropna()
                    if not chart_df.empty:
                        st.bar_chart(chart_df, height=240)

                st.dataframe(log, width='stretch', hide_index=True)

                if not log.empty:
                    try:
                        avg_row = {"game_date": "AVERAGE", "opponent": "\u2014",
                                  "is_home": "", "vs_starter_name": ""}
                        for c in stat_cols:
                            val = pd.to_numeric(log[c], errors="coerce").mean()
                            avg_row[c] = round(val, 2) if pd.notna(val) else None
                        st.caption("Season averages")
                        st.dataframe(pd.DataFrame([avg_row]), width='stretch',
                                     hide_index=True)
                    except Exception as e:
                        st.caption(f"Season averages unavailable ({e})")

                st.subheader("Matchup history")
                mlb_opp_sel = st.selectbox("vs opponent", MLB_TEAMS, index=None,
                                           placeholder="Pick a team",
                                           key="mlb_opp_select")
                if mlb_opp_sel:
                    vs = query_df(f"""
                        SELECT season, game_date, {select_cols}
                        FROM {table}
                        WHERE full_name = ? AND opponent = ?
                        ORDER BY season DESC, game_date DESC
                    """, (mlb_player, mlb_opp_sel))
                    if vs.empty:
                        st.info(f"{mlb_player} has no recorded games vs {mlb_opp_sel}.")
                    else:
                        st.dataframe(vs, width='stretch', hide_index=True)

    with mlb_tab_scanner:
        st.subheader("6-Signal MLB Prop Analysis")

        with st.form("mlb_ev_form"):
            g1, g2, g3 = st.columns(3)
            with g1:
                msc_player = st.selectbox("Player", all_mlb_player_names(),
                                          index=None, placeholder="Player name",
                                          key="mlb_scanner_player")
                msc_stat = st.selectbox("Stat", MLB_BETTABLE_STATS,
                                        key="mlb_scanner_stat")
                msc_side = st.radio("Side", ["over", "under"], horizontal=True,
                                    key="mlb_scanner_side")
            with g2:
                msc_line = st.number_input("Line", value=0.5, step=0.5,
                                           key="mlb_scanner_line")
                msc_over = st.number_input("Over odds", value=-110, step=5,
                                           key="mlb_scanner_over")
                msc_under = st.number_input("Under odds", value=-110, step=5,
                                            key="mlb_scanner_under")
            with g3:
                msc_opp = st.selectbox("Opponent", MLB_TEAMS, index=None,
                                       key="mlb_scanner_opp")
                msc_book = st.text_input("Book", value="fanduel",
                                         key="mlb_scanner_book")
                msc_home = st.radio("Home or away?", ["Home", "Away", "Unknown"],
                                    horizontal=True, key="mlb_scanner_home")
                msc_starter = st.text_input(
                    "Opposing starting pitcher (optional, exact name)",
                    key="mlb_scanner_starter",
                )

            msc_submitted = st.form_submit_button(
                "Analyze", type="primary", width='stretch'
            )

        if msc_submitted:
            if not msc_player or not msc_opp:
                st.warning("Pick a player and an opponent first.")
            else:
                is_home_val = True if msc_home == "Home" else \
                    False if msc_home == "Away" else None
                try:
                    mr = analyze_mlb_prop(
                        player_name=msc_player, stat=msc_stat,
                        line=float(msc_line),
                        over_odds=int(msc_over), under_odds=int(msc_under),
                        opponent=msc_opp, side=msc_side, book=msc_book,
                        is_home=is_home_val,
                        opposing_starter=msc_starter or None,
                    )
                    st.session_state["mlb_last_analysis"] = mr
                except ValueError as e:
                    st.error(str(e))
                    st.session_state.pop("mlb_last_analysis", None)

        if "mlb_last_analysis" in st.session_state:
            mr = st.session_state["mlb_last_analysis"]

            mlb_verdict = "🟢 POSITIVE EV" if mr.ev_percent > 0 else "🔴 NEGATIVE EV"
            st.markdown(f"### {mlb_verdict} — grade **{mr.grade()}**")

            mv1, mv2, mv3, mv4 = st.columns(4)
            mv1.metric("True probability", f"{mr.true_probability:.1%}")
            mv2.metric("Book fair (no vig)", f"{mr.no_vig_probability:.1%}",
                      delta=f"{mr.edge:+.1%} edge")
            mv3.metric("EV", f"{mr.ev_percent:+.2f}%")
            mv4.metric("Kelly bet size", f"{mr.kelly_fraction:.1%}")

            st.markdown("#### Signal breakdown")
            msig_rows = [
                ["Hit rate", f"{mr.hit_rate_signal:.1%}", "22%",
                 "blend of simple + recency-weighted"],
                ["Statistical model", f"{mr.model_signal:.1%}", "25%",
                 f"mean {mr.mean_stat:.2f} ± {mr.std_stat:.2f} "
                 f"({mr.sample_size} games)"],
                ["Opponent", f"{mr.opponent_signal:.1%}", "13%",
                 f"{mr.opponent} allows {mr.opp_allows:.2f}/game"],
                ["Home/Away", f"{mr.home_away_signal:.1%}", "10%", ""],
            ]
            if mr.park_signal is not None:
                msig_rows.append(["Ballpark", f"{mr.park_signal:.1%}", "15%",
                                  f"factor {mr.park_factor:.2f}x"])
            else:
                msig_rows.append(["Ballpark", "skipped", "\u2014",
                                  "insufficient home/road sample"])
            if mr.bvp_signal is not None:
                msig_rows.append(["Vs Starter", f"{mr.bvp_signal:.1%}", "15%",
                                  f"{mr.bvp_sample} games vs "
                                  f"{mr.opposing_starter}"])
            else:
                msig_rows.append(["Vs Starter", "skipped", "\u2014",
                                  "no starter specified or insufficient history"])

            msig = pd.DataFrame(msig_rows,
                                columns=["Signal", "Value", "Weight", "Notes"])
            st.dataframe(msig, width='stretch', hide_index=True)

            if st.button("💾 Track this prediction (feedback loop)",
                        key="mlb_track_button"):
                mlb_row_id = save_prediction(
                    player_name=mr.player_name, stat=mr.stat, line=mr.line,
                    side=mr.side, book=mr.book, american_odds=mr.american_odds,
                    opponent=mr.opponent, season=datetime.now().year, week=0,
                    predicted_prob=mr.true_probability,
                    ev_percent=mr.ev_percent,
                    kelly_fraction=mr.kelly_fraction, grade=mr.grade(),
                    sub_signals={
                        "hit_rate": mr.hit_rate_signal,
                        "model_prob": mr.model_signal,
                    },
                )
                st.success(f"Saved as prediction #{mlb_row_id}. "
                          f"Log the result in the Tracker tab after the game.")

# ══════════════════════════════════════════════════════════════
#  TAB 3 — TRACKER (feedback loop)
# ══════════════════════════════════════════════════════════════

with tab_tracker:
    st.subheader("Bankroll")
    per_book = get_bankroll_by_book()

    with st.expander("Set starting balance for a book"):
        sc1, sc2, sc3 = st.columns([1.5, 1, 1])
        with sc1:
            sb_book = st.selectbox(
                "Book", ["fanduel", "draftkings", "betmgm", "caesars"],
                key="sb_book_select",
            )
        with sc2:
            sb_amount = st.number_input("Starting balance ($)", min_value=0.0,
                                        value=0.0, step=5.0, key="sb_amount")
        with sc3:
            st.write("")
            st.write("")
            if st.button("Save", key="sb_save"):
                set_starting_balance(sb_book, float(sb_amount))
                st.success(f"Starting balance for {sb_book} set to "
                          f"${sb_amount:.2f}.")
                st.rerun()
        st.caption("Set this once per book to whatever your balance was "
                  "right before you started tracking bets here — current "
                  "balance is then starting + deposits + net profit from "
                  "every logged real bet, automatically.")

    with st.expander("Add a deposit or withdrawal"):
        dc1, dc2, dc3, dc4 = st.columns([1.4, 1, 1, 1])
        with dc1:
            tx_book = st.selectbox(
                "Book ", ["fanduel", "draftkings", "betmgm", "caesars"],
                key="tx_book_select")
        with dc2:
            tx_kind = st.radio("Type", ["deposit", "withdrawal"],
                               key="tx_kind", horizontal=True)
        with dc3:
            tx_amount = st.number_input("Amount ($)", min_value=0.0,
                                        value=10.0, step=5.0,
                                        key="tx_amount")
        with dc4:
            st.write(""); st.write("")
            if st.button("Add", key="tx_save") and tx_amount > 0:
                add_bank_transaction(tx_book, float(tx_amount), tx_kind)
                st.success(f"{tx_kind.capitalize()} of ${tx_amount:.2f} "
                          f"recorded for {tx_book}.")
                st.rerun()
        st.caption("Re-ups and cash-outs both live here, so the budget "
                  "math stays honest: current balance = starting + net "
                  "deposits + net profit − pending real stakes.")

    if not per_book:
        st.info("No bankroll data yet. Set a starting balance above, or "
                "track a real bet (check \"This is a real bet\" when "
                "logging a pick) to see it here.")
    else:
        for b in per_book:
            with st.container(border=True):
                st.markdown(f"**{b['book'].capitalize()}**")
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Starting", f"${b['starting_balance']:.2f}")
                c2.metric("Deposits", f"${b.get('deposits', 0.0):+.2f}")
                c3.metric("Record", f"{b['wins']}-{b['losses']}"
                          if b['n_bets'] else "—")
                c4.metric("Staked", f"${b['total_staked']:.2f}")
                c5.metric("Net profit", f"${b['net_profit']:+.2f}")
                c6.metric("Current balance", f"${b['current_balance']:.2f}",
                          delta=f"{b['net_profit']:+.2f}")

    st.divider()
    st.subheader("Pending predictions")
    ar1, ar2 = st.columns([1, 3])
    with ar1:
        if st.button("🔄 Fetch results & auto-resolve", key="auto_resolve_btn"):
            # v3.7: the old button only READ from mlb_batting/pitching,
            # so any game played since the last full rebuild was
            # invisible and its bets silently stayed pending with no
            # explanation. Now it first fetches fresh game logs for
            # exactly the players with pending bets (~1 API call each)
            # and grades game-market bets straight from schedule final
            # scores — then resolves. No 20-minute rebuild required.
            from update_mlb_results import update_and_resolve
            with st.spinner("Fetching fresh results for pending players "
                            "and grading..."):
                full = update_and_resolve()
            ar_result = full["auto_resolve"]
            game_res = full["game_bets"]["resolved"]
            n_resolved = len(ar_result["resolved"]) + len(game_res)
            if n_resolved:
                names = ([f"{r['player']} ({r['actual_value']})"
                          for r in ar_result["resolved"]] +
                         [f"{p} ({v})" for _, p, v in game_res])
                st.success(f"Resolved {n_resolved} bet(s): "
                           + ", ".join(names))
            else:
                st.info("Nothing resolved — see reasons below.")
            still = (ar_result["skipped_no_data"]
                     + [{"id": i, "player": "(game bet)", "reason": rsn}
                        for i, rsn in full["game_bets"]["skipped"]])
            if still:
                st.warning("Still pending, and why:\n" + "\n".join(
                    f"- #{s['id']} {s['player']}: {s['reason']}"
                    for s in still[:12]))
            if ar_result["skipped_ambiguous"]:
                st.warning(f"{len(ar_result['skipped_ambiguous'])} bet(s) "
                          f"ambiguous (multiple candidate games) — "
                          f"resolve manually below: " + ", ".join(
                              f"#{s['id']} {s['player']}"
                              for s in ar_result["skipped_ambiguous"][:8]))
            st.session_state["_last_auto_resolve"] = ar_result
            st.rerun()
    with ar2:
        st.caption("Fetches last-night's results for every player with a "
                  "pending bet (fast, targeted API calls — no full "
                  "rebuild), grades game-market bets from official "
                  "final scores, then resolves everything it can. "
                  "Today's not-yet-played games correctly stay pending.")

    pending = query_df("""
        SELECT id, player_name, stat, side, line, book,
               american_odds, opponent, game_date, season, week,
               ROUND(predicted_prob*100,1) AS pred_pct,
               ROUND(ev_percent,2) AS ev_pct, grade,
               bet_placed, stake
        FROM prop_results
        WHERE result IS NULL
        ORDER BY analyzed_at DESC
    """)
    if pending.empty:
        st.info("No pending predictions. Track a pick from Top Picks or "
                "the EV Scanner tab, then log its result here once the "
                "game is final.")
    else:
        st.dataframe(pending, width='stretch', hide_index=True)

        st.markdown("#### Log a result")
        lc1, lc2, lc3 = st.columns([1, 1, 1])
        with lc1:
            log_id = st.number_input("Prediction ID", min_value=1, step=1)
        with lc2:
            actual = st.number_input("Actual stat value", value=0.0,
                                     step=0.5)
        with lc3:
            st.write("")
            st.write("")
            if st.button("Log result", type="primary"):
                log_result(int(log_id), float(actual))
                st.rerun()
        st.caption("Note: if this prediction was tracked as a real bet "
                  "(stake entered when you tracked it), profit/loss is "
                  "computed automatically from the odds.")

    st.divider()
    st.markdown("#### Fix a mistake")
    st.caption("Wrong player, wrong stake, wrong line/odds — pull up any "
              "logged prediction by its ID and correct it. If it already "
              "has a result, the result and payout are recomputed "
              "automatically from your correction, not left stale.")
    edit_id = st.number_input("Prediction ID to edit", min_value=1, step=1,
                              key="edit_id_input")
    if st.button("Load", key="edit_load_button"):
        st.session_state["_editing_row"] = get_prediction(int(edit_id))

    editing = st.session_state.get("_editing_row")
    if editing and editing["id"] == int(edit_id):
        st.write(f"Editing prediction #{editing['id']} — currently "
                f"logged as: **{editing['player_name']}**, "
                f"{editing['stat']} {editing['side']} {editing['line']}, "
                f"{editing['american_odds']:+d} @ {editing['book']}, "
                f"stake ${editing['stake']}"
                + (f" — result: {editing['result']}"
                   if editing['result'] else " — still pending"))
        ec1, ec2, ec3 = st.columns(3)
        with ec1:
            new_name = st.text_input("Player name", value=editing["player_name"],
                                     key="edit_name")
            new_stat = st.text_input("Stat", value=editing["stat"],
                                     key="edit_stat")
        with ec2:
            new_line = st.number_input("Line", value=float(editing["line"]),
                                       step=0.5, key="edit_line")
            new_side = st.radio("Side", ["over", "under"],
                                index=0 if editing["side"] == "over" else 1,
                                key="edit_side", horizontal=True)
        with ec3:
            new_odds = st.number_input("Odds", value=int(editing["american_odds"]),
                                       step=5, key="edit_odds")
            new_stake = st.number_input(
                "Stake ($)", value=float(editing["stake"] or 0), step=1.0,
                key="edit_stake")
        ec4, ec5 = st.columns(2)
        with ec4:
            new_book = st.text_input("Book", value=editing["book"], key="edit_book")
            new_opponent = st.text_input("Opponent", value=editing["opponent"] or "",
                                         key="edit_opponent")
        with ec5:
            new_game_date = st.text_input(
                "Game date (YYYY-MM-DD)", value=editing["game_date"] or "",
                key="edit_game_date")

        if st.button("Save correction", type="primary", key="edit_save"):
            edit_prediction(
                int(edit_id), player_name=new_name, stat=new_stat,
                line=new_line, side=new_side, book=new_book,
                american_odds=int(new_odds), stake=new_stake,
                opponent=new_opponent or None,
                game_date=new_game_date or None,
            )
            st.session_state.pop("_editing_row", None)
            st.success(f"Prediction #{edit_id} updated.")
            st.rerun()

    st.divider()
    st.subheader("Model accuracy")
    completed = query_df("""
        SELECT result, predicted_prob, stat, player_name, bet_placed
        FROM prop_results
        WHERE result IN ('hit', 'miss')
    """)
    # Calibration countdown: paper + real both count toward the
    # 150-graded threshold that unlocks trusting the report.
    CAL_GOAL = 150
    n_graded = len(completed)
    n_real = int((completed["bet_placed"] == 1).sum()) if n_graded else 0
    n_paper = n_graded - n_real
    st.progress(min(n_graded / CAL_GOAL, 1.0),
                text=f"Calibration sample: {n_graded}/{CAL_GOAL} graded "
                     f"({n_real} real · {n_paper} paper)"
                     + ("  —  ready! Run calibration_report.py"
                        if n_graded >= CAL_GOAL else ""))
    acc_scope = st.radio(
        "Score", ["All", "Real money only", "Paper only"],
        horizontal=True, key="acc_scope",
        help="Real and paper are scored together for calibration "
             "(a prediction is a prediction), but you can split them "
             "to compare the model's record against your betting "
             "record.")
    if acc_scope == "Real money only":
        completed = completed[completed["bet_placed"] == 1]
    elif acc_scope == "Paper only":
        completed = completed[completed["bet_placed"] != 1]
    if completed.empty:
        st.caption("No completed predictions yet. Accuracy stats appear "
                   "here once you log results — this is the feedback "
                   "loop that pushes the model toward 60%+.")
    else:
        hits  = int((completed["result"] == "hit").sum())
        total = len(completed)
        a1, a2 = st.columns(2)
        a1.metric("Overall hit rate", f"{hits}/{total}",
                  f"{100*hits/total:.1f}%")

        # Calibration by predicted-probability bucket
        completed["bucket"] = pd.cut(
            completed["predicted_prob"],
            bins=[0, 0.5, 0.6, 0.7, 0.8, 1.0],
            labels=["<50%","50-60%","60-70%","70-80%","80%+"],
        )
        calib = completed.groupby("bucket", observed=True).agg(
            games=("result", "size"),
            hit_rate=("result", lambda s: f"{100*(s=='hit').mean():.0f}%"),
        ).reset_index()
        with a2:
            st.dataframe(calib, width='stretch',
                         hide_index=True)

        by_stat = completed.groupby("stat").agg(
            games=("result", "size"),
            hit_rate=("result", lambda s: f"{100*(s=='hit').mean():.0f}%"),
        ).reset_index()
        st.dataframe(by_stat, width='stretch', hide_index=True)

        st.markdown("#### By player")
        st.caption("Players need at least 3 logged results before "
                  "showing up here — a track record from 1-2 games "
                  "isn't meaningful yet.")
        player_records = get_all_player_track_records(min_sample=3)
        if not player_records:
            st.caption("No player has 3+ logged results yet.")
        else:
            pr_df = pd.DataFrame(player_records)
            pr_df["hit_rate"] = (pr_df["hit_rate"] * 100).round(1).astype(str) + "%"
            pr_df["net_profit"] = pr_df["net_profit"].apply(lambda x: f"${x:+.2f}")
            pr_df = pr_df.rename(columns={
                "player_name": "Player", "total": "Tracked",
                "hits": "Hits", "hit_rate": "Hit Rate",
                "bets_placed": "Real Bets", "net_profit": "Net $",
            })
            st.dataframe(pr_df, width='stretch', hide_index=True)

# ══════════════════════════════════════════════════════════════
#  TAB 4 — LIVE ODDS
# ══════════════════════════════════════════════════════════════

with tab_odds:
    st.subheader("Latest scraped lines")
    st.caption("Populated by `python scrapers/sgo_scraper.py` "
               "(add `--league mlb` for baseball). NFL props appear "
               "~1-2 weeks before games (August+); MLB is in season now.")

    try:
        date_options = query_df("""
            SELECT DISTINCT substr(fetched_at,1,10) AS pull_date
            FROM live_odds ORDER BY pull_date DESC
        """)
    except Exception:
        date_options = pd.DataFrame()

    if date_options.empty:
        st.info("No lines in the live_odds table yet. Run the scraper "
                "when props are posted:  python scrapers/sgo_scraper.py "
                "(or add --league mlb)")
    else:
        all_dates = date_options["pull_date"].tolist()
        f0, f1 = st.columns([1, 2])
        with f0:
            date_filter = st.selectbox(
                "Pull date (browse past snapshots)", all_dates, index=0,
            )

        # Only fetch full rows for the selected date — a single day's
        # pull can be thousands of rows (a real MLB pull was 5,750),
        # so this is queried fresh per-date rather than capped at a
        # fixed row limit that would silently hide most of it.
        odds = query_df("""
            SELECT fetched_at, league, book, player_id, stat_id AS stat,
                   side, line, odds, home_team, away_team, starts_at, deeplink
            FROM live_odds
            WHERE substr(fetched_at,1,10) = ?
            ORDER BY fetched_at DESC
        """, (date_filter,))

        with f1:
            league_filter = st.multiselect(
                "League", sorted(odds["league"].dropna().unique()),
                default=sorted(odds["league"].dropna().unique()),
            )
        book_filter = st.multiselect(
            "Books", sorted(odds["book"].unique()),
            default=sorted(odds["book"].unique()),
        )
        filtered = odds[odds["book"].isin(book_filter) &
                        odds["league"].isin(league_filter)]
        st.caption(f"{len(filtered):,} lines from {date_filter}")
        st.dataframe(
            filtered, width='stretch', hide_index=True,
            column_config={
                "deeplink": st.column_config.LinkColumn(
                    "Bet", display_text="Place bet \u2197",
                    help="Opens this exact line directly on the "
                         "sportsbook, when SGO provides a direct link.",
                )
            },
        )
