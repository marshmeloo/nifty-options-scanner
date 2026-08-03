"""
Config for the directional single-sided credit spread strategy: sell a
put spread when the market bias is bullish, sell a call spread when it's
bearish. Deliberately a SEPARATE file from config.py and config_condor.py
-- same reasoning as config_condor.py's own header: this strategy has
its own risk model, its own holding period, and runs as its own process
(main_directional_spread.py), so keeping config separate keeps that
separation real rather than aspirational.

HOW THIS DIFFERS FROM THE EXISTING IRON CONDOR (config_condor.py)
------------------------------------------------------------------
The condor is market-NEUTRAL: it sells both a call spread and a put
spread and profits if spot stays in a range, opens once a week (the day
after the previous expiry), and is held to expiry.

This strategy is directional: it sells ONE side, chosen by
scanner.compute_market_bias() -- the same top-down bullish/bearish/range
read that (as of 2026-07-29) gates the momentum scanner's counter-bias
candidates. Here bias isn't just a gate, it IS the entry signal. It can
open on ANY day the bias is strong enough (checked every cycle, not just
once a week), and a position is meant to be held OVERNIGHT -- across the
close, carrying real gap risk -- rather than force-closed intraday like
the momentum scanner, and unlike the condor it is actively managed with
a profit target / stop loss rather than always run to expiry.
"""

# --- Direction selection ---
# How strong scanner.compute_market_bias()'s score has to be before this
# strategy treats it as tradeable. Separate from config.BIAS_STRONG_THRESHOLD
# (used by the momentum scanner's counter-bias PENALTY) on purpose: that
# one only needs to be confident enough to penalise a candidate opposing
# it, this one needs to be confident enough to sell naked-side premium
# against the other direction, which deserves a higher bar.
BIAS_STRONG_THRESHOLD = 2.0

# --- Strike selection (same premium-band + highest-OI approach as the
# condor, applied to whichever single side the bias selects) ---
#
# CHANGED 2026-08-02: was SHORT_PREMIUM_MIN/MAX = 30.0/60.0,
# HEDGE_DISTANCE_POINTS = 150.0. sweep_spread_config.py re-ran the full
# 493-day, 2-year history once per (premium band, hedge distance)
# combination -- the risk gates enforced (MIN_NET_CREDIT,
# MAX_CAPITAL_AT_RISK, MAX_CONCURRENT_POSITIONS, MAX_NEW_POSITIONS_PER_DAY,
# cross-day one_at_a_time) -- and found the old config was the WORST cell
# in the entire 4x3 grid: every other premium band beat it at every hedge
# distance tested, and all 12 cells were profitable with z from 2.94 to
# 5.57 (Bonferroni bar for 12 comparisons is ~2.9 -- every cell clears
# it, this is not a single lucky cell).
#
#   old (30-60 / 150):  n=108  total Rs 64,236   z=3.49  maxDD -Rs 10,332
#   new (40-70 / 100):  n=125  total Rs 78,006   z=5.48  maxDD  -Rs 5,099
#
# 40-70/100 was chosen over the grid's highest RAW total (65-100/200:
# Rs 133,390) for return-per-unit-of-drawdown instead: 15.30 vs 8.98,
# more than double, at roughly a third of the drawdown. The full grid
# (12 cells, both totals and drawdown-adjusted returns) is in
# logs/sweep_spread_config.json and README.md's 2026-08-02 entry --
# revert to the old values above if live results diverge from this
# backtest, same reasoning as SCORING_MODE in config.py.
SHORT_PREMIUM_MIN = 40.0
SHORT_PREMIUM_MAX = 70.0

# How far OTM (in strike points) the protective hedge leg sits beyond
# the short strike. This IS the max-loss dial -- see config_condor.py's
# note on the same tradeoff. Narrower than the condor's 300 points on
# purpose: this position is actively managed overnight, not left to run
# a full week, so it doesn't need as wide a berth.
#
# CHANGED 2026-08-02 alongside the premium band above -- see that
# comment for the sweep evidence. Was 150.
HEDGE_DISTANCE_POINTS = 100

# --- Position sizing / capital ---
LOTS_PER_LEG = 1
NIFTY_LOT_SIZE = 65   # duplicated here so this file has no import-order dependency on config.py

# --- Risk gates (see directional_spread_risk_checker.py) ---
MAX_CONCURRENT_POSITIONS = 1
MAX_NEW_POSITIONS_PER_DAY = 1   # at most one NEW entry per day even if flat and bias stays strong all day
MIN_NET_CREDIT = 8.0
MAX_CAPITAL_AT_RISK = 30000.0

# --- Active position management ---
# Unlike the condor (run to expiry unless breached), this strategy closes
# itself early on either a profit target or a stop loss -- standard
# credit-spread practice, since the bulk of a spread's theta decay is
# captured well before expiry and holding for the last few points of
# credit disproportionately extends gap-risk exposure for little
# additional reward.
PROFIT_TARGET_PCT_OF_MAX_PROFIT = 60.0   # close once this % of the max possible credit is captured
STOP_LOSS_PCT_OF_MAX_LOSS = 50.0         # close once mark-to-market loss reaches this % of max possible loss

# Milestones tracked on EVERY position (win, loss, or expiry), independent
# of the two live thresholds above -- same purpose as config.RR_MILESTONES
# for the momentum scanner: the dataset for answering "is 60% the right
# profit target?" with evidence instead of intuition. Expressed as % of
# max_profit_inr because this strategy has no symmetric "R" the way a
# fixed-stop long option does -- max profit and max loss are related
# through the hedge width, not equal by construction, so the momentum
# scanner's R-multiple concept doesn't transfer here.
PROFIT_MILESTONES_PCT = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

# If spot gets within this many points of the short strike, that's a
# "breach warning" -- staged for human review rather than closed
# automatically, same reasoning as config_condor.py's identical setting.
BREACH_WARNING_BUFFER_POINTS = 40

# --- Timing ---
# Widened from 30s (2026-07-31): the bias score this strategy enters on
# doesn't move within seconds like the momentum scanner's setups do, so
# there's no accuracy cost to polling less often -- but every poll shares
# Dhan's per-account rate limit with main_live.py and main_condor.py (see
# dhan_rate_limiter.py), and this process was one of three contributing to
# 429 storms that reached main_live.py during a live trade on 2026-07-31.
# 90s keeps it responsive to a same-day bias flip while cutting its share
# of shared request volume to a third of what it was.
POLL_INTERVAL_SECONDS = 90

# --- Approval (see main_directional_spread.py) ---
# Same meaning as config_condor.AUTO_APPROVE_NEW_POSITIONS: a candidate
# that clears the risk check is still written through trade_staging.py
# first (so it stays visible in the same audit trail/dashboard as a
# manually-approved one), but auto-approved and auto-executed rather
# than waiting for approve_orders.py. Nothing here ever calls a broker
# API regardless of this setting.
AUTO_APPROVE_NEW_POSITIONS = True

STATE_PATH = "state/directional_spread_position.json"
JOURNAL_PATH = "logs/directional_spread_journal.jsonl"
