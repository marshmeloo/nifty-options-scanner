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
SHORT_PREMIUM_MIN = 30.0
SHORT_PREMIUM_MAX = 60.0

# How far OTM (in strike points) the protective hedge leg sits beyond
# the short strike. This IS the max-loss dial -- see config_condor.py's
# note on the same tradeoff. Narrower than the condor's 300 points on
# purpose: this position is actively managed overnight, not left to run
# a full week, so it doesn't need as wide a berth.
HEDGE_DISTANCE_POINTS = 150

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

# If spot gets within this many points of the short strike, that's a
# "breach warning" -- staged for human review rather than closed
# automatically, same reasoning as config_condor.py's identical setting.
BREACH_WARNING_BUFFER_POINTS = 40

# --- Timing ---
# Polls at the SAME cadence as the momentum scanner (main_live.py), not
# the condor's coarser 5-minute poll -- this strategy can open on any
# day the bias reads strongly enough, not just once a week, so it needs
# to notice that as promptly as the momentum scanner notices its own
# setups.
POLL_INTERVAL_SECONDS = 30

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
