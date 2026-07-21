"""
Central config for the Nifty options decision-support scaffold.
Every threshold here is a parameter you set, not a hardcoded trading rule.
Tune these based on your own risk appetite and backtesting.
"""

# --- Risk parameters (edit these to match your capital and rules) ---
TOTAL_CAPITAL = 500000          # total capital allocated to options trading
MAX_RISK_PER_TRADE_PCT = 1.0    # max % of capital risked on a single trade
MAX_TOTAL_EXPOSURE_PCT = 20.0   # max % of capital deployed at once across open positions
MAX_DAILY_LOSS_PCT = 3.0        # circuit breaker: stop trading for the day past this loss
MAX_LOTS_PER_TRADE = 1           # hard cap on lot size per trade, independent of capital calc

NIFTY_LOT_SIZE = 65             # update if NSE revises lot size

# --- Scanner thresholds ---
IV_PERCENTILE_HIGH = 75         # flag as "IV rich" above this percentile
IV_PERCENTILE_LOW = 25          # flag as "IV cheap" below this percentile
OI_BUILDUP_PCT = 15.0           # % change in OI (single expiry, single strike) to flag buildup
PCR_BULLISH_ABOVE = 1.2         # put-call ratio above this = bullish bias
PCR_BEARISH_BELOW = 0.8         # put-call ratio below this = bearish bias
VWAP_DEVIATION_PCT = 0.3        # % deviation from VWAP to flag as a momentum signal

# --- OI + price buildup classification ---
# Raw OI% alone doesn't tell you if buyers or writers are behind the move;
# combined with premium direction it does. See _classify_buildup in
# dhan_source.py for the four cases.
LONG_BUILDUP_SCORE = 1.0        # price up + OI up: buyers accumulating, genuinely bullish for this contract
SHORT_COVERING_SCORE = 0.75     # price up + OI down: writers exiting, also bullish for this contract
SHORT_BUILDUP_SCORE = -1.0      # price down + OI up: writers piling in, bearish for THIS contract's premium
LONG_UNWINDING_SCORE = -0.5     # price down + OI down: longs capitulating, bearish for this contract

# --- Trade plan defaults ---
DEFAULT_STOP_LOSS_PCT = 30.0    # % of premium, used only if no explicit stop is computed
DEFAULT_TARGET_RR = 2.0         # target expressed as reward:risk multiple of the stop distance

# --- Price-action / structure thresholds (OB, FVG, S/R, sweeps) ---
SWING_LOOKBACK = 5               # candles on each side to confirm a swing high/low
OB_MIN_MOVE_PCT = 0.3            # min % move away from a candle to qualify it as an order block
SR_CLUSTER_TOLERANCE_PCT = 0.15  # swing points within this % of each other cluster into one S/R level
SR_MIN_TOUCHES = 2               # minimum touches for a cluster to count as a real S/R level
SWEEP_WICK_MIN_PCT = 0.1         # min wick-beyond-level size (%) to count as a liquidity sweep
PRICE_LEVEL_PROXIMITY_PCT = 0.25 # how close spot must be to a level for it to count as confluence
CANDLE_INTERVAL_MINUTES = "5"    # Dhan intraday interval: "1","5","15","25","60"

# --- Strike range filter ---
# Only strikes within this many points of spot are pulled into the chain.
# Deep OTM strikes (far from spot) are usually near-worthless premium and
# just add noise, and including them also distorts the IV cross-sectional
# percentile below. 800-1000 points is roughly 16-20 strikes each side at
# Nifty's normal 50-point spacing, adjust to taste.
STRIKE_RANGE_POINTS = 800

# --- Premium range filter ---
# Only options whose LTP (premium) falls in this range are scanned.
# Cuts out both near-worthless deep-OTM lottery tickets (too cheap) and
# expensive deep-ITM contracts that behave almost like the underlying
# (too pricey for typical premium-buying setups).
PREMIUM_MIN = 10.0
PREMIUM_MAX = 150.0

# --- Trend classification ---
TREND_SWING_LOOKBACK = 3   # how many recent swing highs/lows to use for trend read

# --- Momentum ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
ROC_PERIOD = 10
ROC_SIGNIFICANT_PCT = 0.3   # rate-of-change magnitude to call momentum "meaningful"

# --- Volume confirmation ---
VOLUME_MA_PERIOD = 20
VOLUME_SPIKE_MULTIPLE = 1.5   # candle volume vs its rolling average to count as a spike

# --- Breakout / pullback ---
BREAKOUT_CONFIRM_PCT = 0.05     # close beyond an S/R level by this % counts as a breakout
PULLBACK_PROXIMITY_PCT = 0.15   # how close price must return to a broken level to flag a pullback

# --- Trade tracking ---
# The scanner re-evaluates the whole chain every cycle, which is correct
# for FINDING setups but wrong for TRACKING one: without a cap, "highest
# scoring option this cycle" silently becomes a new plan every 90 seconds
# even when it's really the same underlying setup drifting. These two
# settings force it to commit to a small number of high-conviction trades
# per day and follow each one to its actual outcome instead.
MAX_NEW_TRADES_PER_DAY = 999         # effectively uncapped -- training/evaluation phase, more trades = faster sample building
MIN_CONVICTION_SCORE_TO_TRACK = 5.0  # well above the 1.5 watchlist bar — only strong setups get tracked
JOURNAL_LOOKBACK_FOR_LEARNING = 100  # how many recent journal entries to consider for tag win-rate adjustment
MIN_TAG_SAMPLES_FOR_ADJUSTMENT = 3   # don't trust a win rate until a tag has at least this many outcomes
WEAK_TAG_WIN_RATE = 0.4              # below this win rate, penalize the tag's contribution to score
STRONG_TAG_WIN_RATE = 0.65           # above this win rate, small bonus to the tag's contribution
