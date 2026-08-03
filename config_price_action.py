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
TOTAL_CAPITAL = 500000
MAX_RISK_PER_TRADE_PCT = 1.0
MAX_LOTS_PER_TRADE = 1
NIFTY_LOT_SIZE = 65
