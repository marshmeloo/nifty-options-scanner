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
# Only NEW candidate strikes in this LTP (premium) range get flagged by
# scanner.py. Cuts out both near-worthless deep-OTM lottery tickets (too
# cheap) and expensive deep-ITM contracts that behave almost like the
# underlying (too pricey for typical premium-buying setups).
#
# IMPORTANT: this filter is applied in scanner.py at candidate-selection
# time ONLY. It must never be applied when building the chain itself
# (dhan_source.py / nse_source.py) -- doing that used to silently drop
# already-open trades' quotes from the snapshot the moment their premium
# moved outside this band (which is normal as a position runs toward its
# target), making them permanently untrackable ("current ?" forever) and
# corrupting OI analytics (max pain/PCR need every strike). Fixed 2026-07-22.
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

# --- OI analytics (Max Pain / OI walls / net delta OI) ---
# See oi_analytics.py. This is a chain-wide read, separate from the
# per-strike buildup classification above.
NET_DELTA_OI_NEUTRAL_BAND = 5000  # net delta OI within +/- this many contracts counts as "neutral", not a lean
OI_CONCENTRATION_TOP_N = 5         # how many strikes to surface in the OI concentration table

# --- Data source fallback (Dhan -> NSE -> TradingView) ---
# See resilient_source.py. Dhan is the primary source (full chain + Greeks).
# If Dhan errors or times out, fall back to NSE's public option-chain API
# (full chain, no Greeks). If that also fails, fall back to TradingView for
# spot price and candles ONLY -- TradingView has no public option-chain/OI
# data, so in that last-resort tier the scanner runs in price-action-only
# mode (no OI-based setups) until a chain source recovers.
NSE_REQUEST_TIMEOUT = 10
NSE_SESSION_WARMUP_TIMEOUT = 5
TRADINGVIEW_REQUEST_TIMEOUT = 10
FALLBACK_RETRY_COOLDOWN_SECONDS = 60  # don't hammer a source that just failed; wait this long before retrying it

# --- Pre-market plan generator ---
# See premarket.py. Runs once before market open to build a brief:
# previous session recap, overnight global cues, FII/DII flow, projected
# levels, and any known event for the day.
PREMARKET_LOOKBACK_DAYS = 10        # trading days of daily candles used for level projection
GLOBAL_CUES_REQUEST_TIMEOUT = 10

# Maintain this yourself -- recurring/scheduled events worth flagging in
# the brief (RBI MPC decisions, Union Budget, US FOMC, etc). Key is
# "YYYY-MM-DD", value is a short label. Not fetched from anywhere
# automatically; NSE/RBI don't publish a clean free API for this.
KNOWN_EVENT_DATES = {
    # "2026-08-06": "RBI MPC decision",
    # "2026-09-17": "US Fed FOMC decision",
}

# A same-day expiry gets flagged in the brief as higher-theta-decay /
# higher-whipsaw risk (see get_nearest_expiry usage in premarket.py) --
# NSE has changed the NIFTY weekly expiry weekday more than once, so this
# is computed from the actual expiry date returned by the API rather
# than a hardcoded day of week.

# --- News tracking / event-risk flags ---
# See news_source.py. Keyword-tagged RSS headlines, rolled up into a
# single "elevated" / "normal" risk read for the day -- not sentiment
# analysis, just "is today a day known event categories are in the news."
NEWS_REQUEST_TIMEOUT = 10
NEWS_MAX_HEADLINES_SHOWN = 10
NEWS_RISK_ELEVATED_THRESHOLD = 3   # sum of distinct-category weights (see EVENT_CATEGORIES) that trips "elevated"
NEWS_CACHE_MINUTES = 15            # main_live.py re-fetches news at most this often, not every 30s poll

# If True, risk_checker.check() REJECTS new trades outright on an
# elevated-news-risk day. If False (default), elevated risk is only
# surfaced as an advisory reason on the verdict -- it doesn't block
# anything on its own. Start conservative (False) until you've seen how
# often this actually fires; a keyword match doesn't necessarily mean
# today's specific setup is dangerous.
NEWS_RISK_BLOCKS_NEW_TRADES = False

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
