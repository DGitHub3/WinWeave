"""
src/calibration.py — WinWeave's calibration layer.

WHY THIS FILE EXISTS
--------------------
The 2026-07-09/10 tracked-bet exports show the model claiming true
probabilities 27-49 percentage points away from the no-vig market
number (e.g. Quantrill under 14.5 outs: model 94.4%, market ~45%).
Real edges in liquid player-prop markets are 2-8 points. Gaps that
large are model error, and because the scanner sorts by EV%, the
LARGEST errors float to the top of Top Picks (winner's curse).

This module fixes that with three standard techniques, applied in
order, all sport-agnostic:

  1. EMPIRICAL-BAYES SHRINKAGE — small-sample hit rates get pulled
     toward a prior (the market's own base rate) in proportion to
     how little data supports them. 6 hot games stops looking like
     destiny.

  2. MARKET ANCHORING — the final probability is a confidence-
     weighted blend of the model and the no-vig line. The market is
     the single best publicly available predictor; a model that
     ignores it will always be out-predicted by it.

  3. EDGE CAP — after blending, the claimed edge is hard-capped.
     Any pre-cap edge beyond the audit threshold is flagged for a
     `explain_*_prop.py` trace instead of being trusted.

Plus honest GRADING (A is now rare) and FRACTIONAL KELLY sizing.

INTEGRATION (two lines in each analyzer)
----------------------------------------
In src/mlb_analyzer.py and src/prop_analyzer.py, right after
combine_all_signals() produces `raw_prob` and you have computed
`no_vig_prob` for the chosen side:

    from src.calibration import calibrate
    cal = calibrate(raw_prob, no_vig_prob, sample_size)
    true_probability = cal.probability      # use everywhere downstream
    # keep cal on the result object so the dashboard can show
    # cal.flag / cal.pre_cap_edge in the signal breakdown table

Then replace the old grade() with grade_pick(), and size bets with
fractional_kelly() instead of raw kelly_criterion().

No other engine changes are required. Tunables are module constants;
revisit them after ~100 graded results using calibration_report.py.
"""

from dataclasses import dataclass
from typing import Optional

# ── Tunables ────────────────────────────────────────────────────────
# Sample size at which the model earns EQUAL say with the market.
# n=30 games -> 50/50 blend; n=10 -> 25% model / 75% market.
MARKET_ANCHOR_K = 30

# Model's blend weight can never exceed this, no matter the sample.
# Even a perfectly-fed model shouldn't fully ignore the market.
MAX_MODEL_WEIGHT = 0.60

# Hard cap on |final probability - no-vig probability|.
# v3.1: the cap is now BOTH absolute and RELATIVE. The 2026-07-11
# live scan showed the flaw in a fixed 8-point cap: on a 13% longshot
# market (+610 stolen-base props), 8 points is a 60% relative claim
# against the books, and at longshot odds even a capped edge explodes
# into +40% EV "A" grades. Edge is now capped at whichever is
# smaller: 8 points, or 25% of the smaller market side.
EDGE_CAP = 0.08
EDGE_CAP_REL = 0.25

# v3.1: below this market probability, the model's blend weight is
# scaled down proportionally. Longshot prices carry favorite-longshot
# bias — proportional vig removal OVERSTATES a longshot's fair
# probability — so the model is anchoring to a number that is itself
# inflated. Less model say + tighter cap keeps longshots honest.
LONGSHOT_MARKET = 0.20

# Pre-cap edges beyond this get flag="AUDIT" — meaning: do not bet,
# run explain_*_prop.py, because either the data under this player is
# broken (relief outings polluting a starter sample, wrong player
# matched, stale line) or you found something genuinely rare. Both
# deserve a trace before money.
AUDIT_THRESHOLD = 0.15

# Empirical-Bayes prior strength: the market base rate counts as
# this many pseudo-games when shrinking a raw hit rate.
PRIOR_STRENGTH = 20

# Fractional Kelly (0.25 = quarter Kelly) and absolute stake ceiling
# as a fraction of bankroll. Quarter Kelly gives up little growth and
# cuts drawdown risk drastically under estimation error — and
# estimation error is exactly what the exports demonstrate.
KELLY_FRACTION = 0.25
KELLY_MAX_STAKE = 0.02


@dataclass
class CalibratedProb:
    probability: float        # final, use this for EV / Kelly / display
    raw_model_prob: float     # what the signal blend originally said
    market_prob: float        # no-vig anchor used
    model_weight: float       # how much say the model got (0..1)
    pre_cap_edge: float       # blended edge BEFORE the hard cap
    capped: bool              # True if EDGE_CAP was applied
    flag: Optional[str]       # None | "AUDIT"


def shrink_hit_rate(hits: float, n: int, prior_rate: float,
                    prior_strength: int = PRIOR_STRENGTH) -> float:
    """
    Empirical-Bayes (beta-binomial) shrinkage for the hit-rate signal.

    Use inside ev_engine.hit_rate()/weighted_hit_rate(): instead of
    returning hits/n, return this. `prior_rate` should be the no-vig
    market probability for the same side — the best available
    league/context base rate, updated daily for free by the books.

        shrunk = (hits + prior_rate * K) / (n + K)

    n=6, 5 hits (raw 83%), market prior 35%, K=20:
        (5 + 7.0) / 26 = 46%  — hot, but no longer delusional.
    n=60, 50 hits (raw 83%): (50 + 7) / 80 = 71% — big samples still
    speak loudly. That is the entire point.
    """
    if n <= 0:
        return prior_rate
    return (hits + prior_rate * prior_strength) / (n + prior_strength)


def calibrate(model_prob: float, no_vig_prob: float,
              sample_size: int) -> CalibratedProb:
    """
    Market-anchored blend + edge cap. The one call that fixes the
    94%-vs-45% class of error.
    """
    model_prob = min(max(model_prob, 0.001), 0.999)
    no_vig_prob = min(max(no_vig_prob, 0.001), 0.999)

    n = max(sample_size, 0)
    w = min(n / (n + MARKET_ANCHOR_K), MAX_MODEL_WEIGHT)

    # v3.1: longshot guard — scale the model's say down when betting
    # the low-probability side of a market.
    if no_vig_prob < LONGSHOT_MARKET:
        w *= no_vig_prob / LONGSHOT_MARKET

    blended = w * model_prob + (1 - w) * no_vig_prob
    pre_cap_edge = blended - no_vig_prob

    flag = None
    # Judge weirdness by the RAW disagreement, not the blended one —
    # anchoring can hide a broken input, and broken inputs need a trace.
    if abs(model_prob - no_vig_prob) >= AUDIT_THRESHOLD:
        flag = "AUDIT"

    # v3.1: relative cap — 25% of the smaller market side, never more
    # than 8 absolute points.
    cap = min(EDGE_CAP, EDGE_CAP_REL * min(no_vig_prob, 1 - no_vig_prob))

    capped = False
    if pre_cap_edge > cap:
        blended, capped = no_vig_prob + cap, True
    elif pre_cap_edge < -cap:
        blended, capped = no_vig_prob - cap, True

    return CalibratedProb(
        probability=blended,
        raw_model_prob=model_prob,
        market_prob=no_vig_prob,
        model_weight=w,
        pre_cap_edge=pre_cap_edge,
        capped=capped,
        flag=flag,
    )


def grade_pick(ev_percent: float, sample_size: int,
               cal: CalibratedProb) -> str:
    """
    Honest replacement for the old grade(). Under the old thresholds
    every tracked pick came out "A — Strong edge" — a grader with one
    output measures nothing. Under these, A should be <10% of scans.
    """
    if cal.flag == "AUDIT":
        return ("X — Audit first: model disagrees with market by "
                f"{abs(cal.raw_model_prob - cal.market_prob):.0%}. "
                "Run explain prop before trusting.")
    if ev_percent > 15:
        return ("X — Audit first: {:+.1f}% EV is implausible for a player "
                "prop; longshot pricing or bad input data is far more "
                "likely than a real edge this size.".format(ev_percent))
    if ev_percent <= 0:
        return "F — Negative EV"
    if sample_size < 10:
        return "C — Positive EV but thin sample (<10 games)"
    if ev_percent >= 6 and sample_size >= 20 and not cal.capped \
            and cal.market_prob >= 0.25:  # v3.1: no A on longshot markets
        return "A — Strong edge, good sample"
    if ev_percent >= 3:
        return "B — Real but modest edge"
    return "C — Marginal edge, likely inside the noise"


def fractional_kelly(full_kelly: float,
                     fraction: float = KELLY_FRACTION,
                     max_stake: float = KELLY_MAX_STAKE) -> float:
    """
    Quarter-Kelly with a 2%-of-bankroll ceiling. Full Kelly is only
    optimal when p is EXACTLY right; with estimated p it over-bets
    catastrophically. Wire this wherever kelly_criterion() output is
    shown or used for stake suggestions.
    """
    return max(0.0, min(full_kelly * fraction, max_stake))
