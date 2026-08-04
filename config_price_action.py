"""
Config for the pure price-action strategy: no OI, no IV, no PCR -- only
candlestick structure across two timeframes. Based on the multi-timeframe
methodology in the "Price Action Trading" reference doc (Trendwisdom,
2023): mark support/resistance ZONES on a higher timeframe, wait for
price to reach one, then on a lower timeframe wait for a reaction
followed by a break of the recent consolidation (accumulation/
distribution) in the reaction's direction. Stop sits at the far edge of
that consolidation; target is a fixed 2x the resulting risk.

Deliberately a SEPARATE file from config.py -- same reasoning as
config_condor.py/config_directional_spread.py's own headers: this
strategy has a different signal source entirely (structure only, no
option-chain scoring) and is meant to be backtested and evaluated on its
own before it ever runs alongside the other three.
"""

# --- Timeframe pairs to evaluate ---
# The reference doc uses Daily (zone) + 1-Hour (trigger), which implies
# multi-day holds -- a genuinely different animal from the momentum
# scanner's same-day style. Rather than assume which cadence works
# better, both are backtested (see shadow_price_action.py) and intervals
# are expressed in minutes so the SAME structure-detection code runs
# unchanged on either pair; the pair only changes which candle series
# gets fed into it. "daily" is a signal value, not a literal interval,
# since a calendar day isn't a fixed number of minutes.
TIMEFRAME_PAIRS = {
    "daily_hourly": {"htf_interval": "daily", "ltf_interval": 60},
    "intraday": {"htf_interval": 15, "ltf_interval": 5},
}

# --- Swing / structure detection (mirrors price_action.py's own
# defaults, copied rather than imported so this strategy's tuning can
# never silently drift because someone changed the momentum scanner's) ---
SWING_LOOKBACK = 5
TREND_SWING_LOOKBACK = 3
SR_CLUSTER_TOLERANCE_PCT = 0.15
SR_MIN_TOUCHES = 2

# How close price must get to a HTF support/resistance zone to count as
# "reached" it -- the doc's own "shortlist stocks approaching these
# areas" step, made numeric.
ZONE_APPROACH_PCT = 0.3

# --- Impulse vs correction leg classification ---
# The doc's three stated criteria per leg type: candle size, volume, and
# color uniformity. A leg (the run of candles between two consecutive
# swing points) is IMPULSE if its average body size is at least this
# multiple of the recent average AND at least this fraction of its
# candles share the leg's dominant color.
IMPULSE_BODY_MULTIPLE = 1.3
IMPULSE_COLOR_UNIFORMITY_PCT = 70.0
# Volume confirmation is the doc's own "bonus tip", not a hard gate --
# see generate_signal()'s reasons for how it's used (adds confidence,
# never rejects a signal outright the way a hard gate would).
IMPULSE_VOLUME_MULTIPLE = 1.2

# --- Entry / risk ---
# Fixed 2x, per the doc -- deliberately NOT config.DEFAULT_TARGET_RR:
# that constant belongs to the momentum scanner's volatility-sized plan
# and must not silently couple the two strategies' tuning together.
TARGET_RR = 2.0

# Same volatility-based stop-sizing approach as plan_generator.py's
# _stop_distance (structure distance in index points x option delta),
# reusing config.py's ATR/delta constants would couple this strategy's
# risk sizing to the momentum scanner's, so they are copied here.
MIN_STOP_PCT = 15.0
MAX_STOP_PCT = 40.0

# --- Strike selection (buys a single CE or PE, like the momentum
# scanner -- see config.py's PREMIUM_MIN/MAX for the identical reasoning
# on why a band rather than a fixed strike) ---
PREMIUM_MIN = 40.0
PREMIUM_MAX = 200.0

# --- Position sizing / capital (copied from config.py rather than
# shared, so this strategy's risk cannot silently change if the
# momentum scanner's capital assumptions ever do) ---
# Set to Rs 50,000 (2026-08-04), a small dedicated base rather than the
# momentum scanner's Rs 5L.
TOTAL_CAPITAL = 50000
# NOT 1% (momentum's default): with MAX_LOTS_PER_TRADE hard-capped at 1
# below, this percentage only ever functions as a GATE deciding whether
# even a single lot is affordable -- it can never scale UP position
# size the way it does for momentum. At 1% of Rs 50,000 (Rs 500), the
# worst-case 1-lot risk in this strategy's own PREMIUM_MIN/MAX band
# (entry up to 200, stop up to MAX_STOP_PCT=40% away -> Rs 80/pt x 65 lot
# size = Rs 5,200) silently rounds down to 0 lots on almost every real
# signal -- caught by test_main_price_action.py's
# test_forced_signal_opens_a_tracked_position_via_auto_approve before it
# could ship as a strategy that looks alive but never actually trades.
# 15% comfortably covers that worst case (Rs 7,500) with margin.
MAX_RISK_PER_TRADE_PCT = 15.0
MAX_LOTS_PER_TRADE = 1
NIFTY_LOT_SIZE = 65

# --- R-multiple milestones (copied from config.RR_MILESTONES, not
# imported -- same isolation reasoning as every other constant here).
# Used by price_action_tracker.py's excursion tracking. ---
RR_MILESTONES = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

# A long option closes by hitting the BID, not marking at LTP -- unlike
# the historical backtest data (no book at all, see historical_source.py),
# the live Dhan feed does publish one, so this defaults True to match
# every other live tracker's convention (trade_tracker.exit_price_for,
# condor_tracker/directional_spread_tracker's current-price marking).
USE_BID_ASK_FILLS = True

# --- Live runner (main_price_action.py) ---
# Which of TIMEFRAME_PAIRS to actually run live. Both by default -- kept
# as a list (not a single active pair) so either can be disabled without
# code changes if one turns out not to be worth running.
ACTIVE_PAIRS = ["daily_hourly", "intraday"]

# How often the live loop polls. 300s (5 min) matches the finest candle
# interval either pair's LTF ever resamples from (5-min) -- polling
# faster than the data's own granularity would just re-evaluate the same
# candles repeatedly for no benefit.
POLL_INTERVAL_SECONDS = 300

# How far back to fetch real daily NIFTY candles for "daily_hourly"'s HTF
# zones. Generous margin over the SWING_LOOKBACK*2+2 (12) minimum so
# zones have real touch history behind them, not just the bare minimum.
HTF_DAILY_LOOKBACK_DAYS = 90

# How far back to fetch 5-min candles (then resample to 60-min) for
# "daily_hourly"'s LTF. ~10 trading days gives ~60 hourly bars at ~6/
# session -- comfortably past the 12-bar minimum with room to spare.
LTF_HOURLY_LOOKBACK_DAYS = 15

# Same meaning as config_condor.AUTO_APPROVE_NEW_POSITIONS: nothing in
# this project places real broker orders regardless of this flag (see
# trade_staging.py's own docstring) -- "opening a position" only ever
# means writing this strategy's own tracked state. True skips the manual
# approve_orders.py step; False stages every candidate for review first.
AUTO_APPROVE_NEW_POSITIONS = True

STATE_PATH = "state/price_action_position.json"
JOURNAL_PATH = "logs/price_action_journal.jsonl"
