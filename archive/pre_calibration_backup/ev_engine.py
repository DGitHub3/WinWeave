"""
ev_engine.py — WinWeave EV Math Engine (v2)

Pure math functions. No database access — this module only
does calculations. prop_analyzer.py feeds numbers in.

8-signal probability model:
  1. Weighted hit rate      18%
  2. Statistical model      18%
  3. Opponent defense       15%
  4. Prop tracker           14%
  5. Roster health          12%
  6. Coaching tendency      10%
  7. Weather                 8%
  8. Officials/crew          5%
"""

import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PropAnalysis:
    """Full analysis result for a single player prop."""
    player_name:        str
    stat:               str
    line:               float
    side:               str
    book:               str
    american_odds:      int

    # 8 signal values
    hit_rate_signal:    float
    model_signal:       float
    defense_signal:     float
    prop_tracker_signal:float
    roster_mult:        float
    coaching_mult:      float
    weather_mult:       float
    official_mult:      float

    # Combined result
    true_probability:   float
    implied_probability: float
    no_vig_probability: float

    # EV outputs
    ev_percent:         float
    kelly_fraction:     float
    edge:               float

    # Context
    sample_size:        int
    mean_stat:          float
    std_stat:           float
    opponent_avg:       float
    opponent:           str

    # Sub-factor details (for display)
    roster_details:     dict = field(default_factory=dict)
    weather_desc:       str  = ""
    official_desc:      str  = ""
    coaching_desc:      str  = ""

    def is_positive_ev(self) -> bool:
        return self.ev_percent > 0

    def grade(self) -> str:
        if self.ev_percent >= 8 and self.sample_size >= 10:
            return "A — Strong edge, good sample"
        elif self.ev_percent >= 5 and self.sample_size >= 8:
            return "B — Solid edge"
        elif self.ev_percent >= 3:
            return "C — Marginal edge"
        elif self.ev_percent > 0:
            return "D — Thin edge"
        else:
            return "F — Negative EV, skip"


# ── Odds conversions ───────────────────────────────────────────

def american_to_implied_prob(american_odds: int) -> float:
    if american_odds < 0:
        return abs(american_odds) / (abs(american_odds) + 100)
    return 100 / (american_odds + 100)


def remove_vig(over_odds: int, under_odds: int) -> tuple[float, float]:
    raw_over  = american_to_implied_prob(over_odds)
    raw_under = american_to_implied_prob(under_odds)
    total = raw_over + raw_under
    return raw_over / total, raw_under / total


def decimal_payout(american_odds: int) -> float:
    if american_odds < 0:
        return 100 / abs(american_odds)
    return american_odds / 100


def implied_to_american(prob: float) -> int:
    if prob >= 0.5:
        return round(-prob / (1 - prob) * 100)
    return round((1 - prob) / prob * 100)


# ── Probability models ─────────────────────────────────────────

def normal_probability(mean: float, std: float,
                        line: float, side: str = "over") -> float:
    if std <= 0:
        if side == "over":  return 1.0 if mean > line else 0.0
        return 1.0 if mean < line else 0.0
    z   = (line - mean) / (std * math.sqrt(2))
    cdf = 0.5 * (1 + math.erf(z))
    return (1 - cdf) if side == "over" else cdf


def poisson_probability(mean: float, line: float,
                         side: str = "over") -> float:
    if mean <= 0:
        return 0.0 if side == "over" else 1.0
    k_max = int(line)
    cdf = 0.0
    log_fact = 0.0
    for k in range(k_max + 1):
        if k > 0: log_fact += math.log(k)
        cdf += math.exp(k * math.log(mean) - mean - log_fact)
    cdf = min(cdf, 1.0)
    return (1 - cdf) if side == "over" else cdf


def choose_model(stat: str) -> str:
    poisson_stats = {"passing_tds","rushing_tds","receiving_tds",
                     "receptions","targets","sacks","interceptions"}
    return "poisson" if stat in poisson_stats else "normal"


def calculate_probability(mean: float, std: float, line: float,
                           stat: str, side: str = "over") -> float:
    if choose_model(stat) == "poisson":
        return poisson_probability(mean, line, side)
    return normal_probability(mean, std, line, side)


# ── Hit rate ───────────────────────────────────────────────────

def hit_rate(values: list[float], line: float,
             side: str = "over") -> float:
    if not values: return 0.5
    if side == "over":
        return sum(1 for v in values if v > line) / len(values)
    return sum(1 for v in values if v < line) / len(values)


def weighted_hit_rate(values: list[float], line: float,
                       side: str = "over", decay: float = 0.9) -> float:
    """
    CORRECTED (found and fixed 2026-07): both callers (NFL's
    get_player_recent_stats and MLB's get_player_games) query with
    "ORDER BY ... DESC", meaning values[0] is always the MOST RECENT
    game. The previous version iterated `reversed(values)`, which
    processes the OLDEST game first and gives IT weight 1.0, with
    weight decaying toward the most recent game — the exact opposite
    of this function's own stated intent ("more recent games count
    more... Last game: weight 1.0"). This silently inverted recency
    weighting for every NFL and MLB analysis. Removing the reversal
    fixes it: values[0] (most recent) now correctly gets weight 1.0.
    """
    if not values: return 0.5
    weighted_hits = total_weight = 0.0
    weight = 1.0
    for v in values:  # values[0] is already most-recent per caller ordering
        hit = 1.0 if (side == "over" and v > line) or \
                     (side == "under" and v < line) else 0.0
        weighted_hits += hit * weight
        total_weight  += weight
        weight        *= decay
    return weighted_hits / total_weight if total_weight > 0 else 0.5


# ── Opponent adjustment ────────────────────────────────────────

def opponent_multiplier(opp_avg: float, league_avg: float) -> float:
    if league_avg <= 0: return 1.0
    return max(0.70, min(1.30, opp_avg / league_avg))


# ── EV & Kelly ─────────────────────────────────────────────────

def calculate_ev(true_prob: float, american_odds: int) -> float:
    payout = decimal_payout(american_odds)
    return (true_prob * payout) - (1 - true_prob)


def kelly_criterion(true_prob: float, american_odds: int,
                    fraction: float = 0.25) -> float:
    b = decimal_payout(american_odds)
    p = true_prob
    q = 1 - true_prob
    full_kelly = (b * p - q) / b
    if full_kelly <= 0: return 0.0
    return min(full_kelly * fraction, 0.15)


# ── 8-Signal combiner ──────────────────────────────────────────

# Base weights — these will be tuned by the feedback loop over time
BASE_WEIGHTS = {
    "hit_rate":     0.18,
    "model":        0.18,
    "defense":      0.15,
    "prop_tracker": 0.14,
    "roster":       0.12,
    "coaching":     0.10,
    "weather":      0.08,
    "officials":    0.05,
}


def combine_all_signals(
    hit_rate_prob:    float,
    model_prob:       float,
    defense_prob:     float,
    prop_tracker_prob: float,
    roster_mult:      float,
    coaching_mult:    float,
    weather_mult:     float,
    official_mult:    float,
    sample_size:      int,
) -> float:
    """
    Combines all 8 signals into a single true probability.

    Multiplicative factors (roster, coaching, weather, officials)
    are applied as adjustments to the base probability signals
    rather than added as independent probability estimates.

    Small sample correction: shifts weight toward model when
    player history is limited.
    """
    weights = BASE_WEIGHTS.copy()

    # Small sample correction
    if sample_size < 5:
        weights["hit_rate"]     = max(0.05, weights["hit_rate"] - 0.10)
        weights["prop_tracker"] = max(0.05, weights["prop_tracker"] - 0.07)
        weights["model"]        = min(0.35, weights["model"] + 0.17)

    total_w = weights["hit_rate"] + weights["model"] + \
              weights["defense"] + weights["prop_tracker"]

    # Base probability from the 4 probability-type signals
    base_prob = (
        weights["hit_rate"]     * hit_rate_prob    +
        weights["model"]        * model_prob        +
        weights["defense"]      * defense_prob      +
        weights["prop_tracker"] * prop_tracker_prob
    ) / total_w

    # Apply multiplicative adjustments
    # Each mult is relative to 1.0 so we blend them in
    mult_adj = (
        weights["roster"]   * roster_mult   +
        weights["coaching"] * coaching_mult +
        weights["weather"]  * weather_mult  +
        weights["officials"] * official_mult
    ) / (weights["roster"] + weights["coaching"] +
         weights["weather"] + weights["officials"])

    # Blend: 75% base probability, 25% multiplier effect
    adjusted = base_prob * (0.75 + 0.25 * mult_adj)

    return max(0.01, min(0.99, adjusted))


# ── League averages ────────────────────────────────────────────

LEAGUE_AVERAGES = {
    "passing_yards":   240.0,
    "rushing_yards":    58.0,
    "receiving_yards":  55.0,
    "receptions":        4.5,
    "passing_tds":       1.8,
    "rushing_tds":       0.4,
    "receiving_tds":     0.3,
    "targets":           6.5,
    "interceptions":     0.8,
    "sacks":             2.2,
    "carries":          14.0,
    "completions":      21.0,
    "attempts":         32.0,
}
