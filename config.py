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

# --- Scoring mode (versioned -- see scanner.py's SCORING_MODE handling) ---
#
# "legacy": the original multi-component scorer below (FVG/S-R/OB, IV
#   percentile, PCR, RSI, momentum, volume -- all weighted and summed).
#
# "momentum_only": adopted 2026-08-02 after a 493-day, 2-year forward-
#   return study (component_study.py) found momentum ROC alignment was by
#   far the strongest predictive component (z > 30, robust across every
#   moneyness band) while carrying the SMALLEST weight in the legacy
#   scorer (+/-0.25). Backtesting five re-weighted variants
#   (compare_variants.py) against the same 493 days, gross expectancy per
#   trade ranked: legacy +0.006R -> combined (momentum up-weighted, IV and
#   support-level scoring removed) +0.241R -> momentum_only (score driven
#   by momentum alignment alone) +0.158R/trade but +Rs 470,031 total
#   (5.15 trades/day) vs combined's Rs 118,689 (1.51/day), with the
#   MAX_DAILY_LOSS_PCT / MAX_TOTAL_EXPOSURE_PCT gates enforced in the
#   backtest (see shadow.risk_state_at). See BACKLOG.md and README.md's
#   2026-08-02 entry for the full writeup, including what this can and
#   cannot prove: it is an IN-SAMPLE result on data with no bid/ask (LTP
#   fills only), so it is an optimistic ceiling, not a live guarantee.
#
# Switching back to "legacy" is a one-line change -- nothing else in the
# codebase needs to be touched. logic_version.py fingerprints this value,
# so results under the two modes are never silently pooled.
SCORING_MODE = "momentum_only"

# Scores applied to a candidate under SCORING_MODE == "momentum_only".
# Only the aligned score can clear MIN_CONVICTION_SCORE_TO_TRACK (5.0) by
# default, so live behaviour is: take momentum-aligned candidates, skip
# everything else. Named constants rather than inline numbers so the
# backtest tooling (scorer_variants.py) and live scanner cannot drift
# apart on what "momentum_only" means.
MOMENTUM_ONLY_ALIGNED_SCORE = 6.0
MOMENTUM_ONLY_AGAINST_SCORE = 0.0
MOMENTUM_ONLY_NEUTRAL_SCORE = 3.0   # no ROC signal this cycle -- stays below the bar

# --- Tie-break among candidates with IDENTICAL scores ---
#
# WHY THIS EXISTS AS AN EXPLICIT SETTING
# --------------------------------------
# Momentum ROC is a CHAIN-WIDE read: every strike on the same side gets
# the same alignment verdict, so under SCORING_MODE="momentum_only"
# every qualifying strike scores exactly MOMENTUM_ONLY_ALIGNED_SCORE.
# Measured live 2026-08-05 11:51: 14 PE strikes all tied at 6.0,
# including near-ATM ones whose OI buildup (+343%, +203%) and IV
# (6th/3rd percentile -- cheap) were far stronger than the strike that
# actually got picked (+111%, 78th percentile -- rich).
#
# With every score tied, Python's stable sort left the winner decided by
# CHAIN ORDER (ascending strike), which is not a trading judgement at
# all. Worse, it resolves ASYMMETRICALLY by side: ascending strike means
# "cheapest, deepest OTM" for a PE but "most expensive still under
# PREMIUM_MAX, i.e. near-ATM/ITM" for a CE. Confirmed in the live
# journal -- PE entries cluster at the PREMIUM_MIN floor (11.30, 13.80,
# 15.90, 19.25) while CE entries cluster at the PREMIUM_MAX cap (103.10,
# 107.30, 119.45, 141.20). Nobody chose that.
#
# Modes:
#   "chain_order"       -- the historical accidental behaviour, kept as
#                          the default because the 493-day study that
#                          adopted momentum_only ran WITH it, so it is
#                          baked into that validated result. Changing the
#                          default without re-backtesting would silently
#                          move the strategy off its own evidence.
#   "nearest_atm"       -- prefer the strike closest to spot. The momentum
#                          thesis is "the index moves in direction X"; a
#                          near-ATM option has the delta to actually
#                          capture that move, where a deep-OTM one needs
#                          a far larger move to pay.
#   "highest_oi_change" -- prefer the strongest OI buildup, i.e. let the
#                          positioning signal break the tie that momentum
#                          alone cannot.
#
# Adopt a non-default only on backtest evidence, same rule as every other
# tuning decision in this project.
TIEBREAK_MODE = "chain_order"

# --- Scanner thresholds ---
IV_PERCENTILE_HIGH = 75         # flag as "IV rich" above this percentile
IV_PERCENTILE_LOW = 25          # flag as "IV cheap" below this percentile

# This tool only ever BUYS premium (plan_generator.py always places the
# stop below entry and the target above it -- there is no short-premium
# path in the momentum scanner). So rich and cheap IV are NOT symmetric:
# buying expensive volatility means vega works against you and the move
# has to be bigger just to break even. Both used to score +1.0, which
# rewarded buying an overpriced option exactly as much as an underpriced
# one. If you ever add a premium-SELLING strategy here, this asymmetry
# has to flip for it (config_condor.py is separate and unaffected).
IV_CHEAP_SCORE = 1.0
IV_RICH_SCORE = -1.0
OI_BUILDUP_PCT = 15.0           # % change in OI (single expiry, single strike) to flag buildup
PCR_BULLISH_ABOVE = 1.2         # put-call ratio above this = bullish bias
PCR_BEARISH_BELOW = 0.8         # put-call ratio below this = bearish bias
VWAP_DEVIATION_PCT = 0.3        # % deviation from VWAP to flag as a momentum signal

# --- OI + price buildup classification ---
# Raw OI% alone doesn't tell you if buyers or writers are behind the move;
# combined with premium direction it does. See _classify_buildup in
# dhan_source.py for the four cases.
# --- Intraday OI freshness ---
# Dhan's `previous_oi` is the PREVIOUS SESSION's closing OI, so
# oi_change_pct is a day-cumulative figure that only grows as the session
# runs. Classifying buildup from it means a day-scale read is driving
# 30-second-cadence entry decisions: on 2026-07-28 the 24050 PE was
# classified "long_buildup" at 10:21 (+112%), 13:14 (+338%) and 14:32
# (+217%) -- three entries, all reading the same, through completely
# different intraday price action, because OI vs YESTERDAY had of course
# risen in all three cases.
#
# dhan_source.py now keeps a short rolling per-contract history and
# classifies buildup from the change over the last
# OI_INTRADAY_LOOKBACK_MINUTES instead. The day-cumulative figure is kept
# alongside it (oi_analytics.py's "OI added today" genuinely needs it,
# and "OI is 3x yesterday" is useful context in its own right).
OI_INTRADAY_LOOKBACK_MINUTES = 15
# Don't append a new history sample more often than this. The fast
# position check re-fetches the chain every 5s; without spacing, history
# would balloon and every write would rewrite a large file.
OI_HISTORY_SAMPLE_SPACING_SECONDS = 60
# Minimum intraday OI move (%) to classify a buildup. Much smaller than
# OI_BUILDUP_PCT because this measures minutes, not a whole session.
OI_INTRADAY_BUILDUP_PCT = 2.0

LONG_BUILDUP_SCORE = 1.0        # price up + OI up: buyers accumulating, genuinely bullish for this contract
SHORT_COVERING_SCORE = 0.75     # price up + OI down: writers exiting, also bullish for this contract
SHORT_BUILDUP_SCORE = -1.0      # price down + OI up: writers piling in, bearish for THIS contract's premium
LONG_UNWINDING_SCORE = -0.5     # price down + OI down: longs capitulating, bearish for this contract

# --- Trade plan defaults ---
DEFAULT_STOP_LOSS_PCT = 30.0    # % of premium, used only if no explicit stop is computed
DEFAULT_TARGET_RR = 2.0         # target expressed as reward:risk multiple of the stop distance

# --- Breakeven-arm exit (added 2026-08-14) ---
# Once a trade's favorable excursion reaches this many R, arm a breakeven
# stop: if price later falls back to entry, close it there instead of
# letting it round-trip into a real loss. Validated across the full
# 2020-08..2026-08 reconstructed history (10,660 trades): drawdown
# 44.8%->15.8%, net P&L +Rs16.6L->+Rs39.2L (STT-only) / +Rs14.3L->+Rs36.8L
# (real spread-inclusive), never made a single one of 73 calendar months
# worse, and independently confirmed against the real live journal (77
# trades: -Rs17,923 actual -> -Rs3,486 simulated). See the NIFTY momentum
# research note for the full backtest. Set to None to disable and hold
# every trade to its original target/stop/EOD close, as before.
#
# A trade that arms and later closes at breakeven is NOT dropped from
# tracking -- see _seed_breakeven_shadow()/update_breakeven_shadows():
# the same contract keeps being watched (no real position, no further
# P&L impact) until it reaches DEFAULT_TARGET_RR, the original stop, or
# end of day, so there is an ongoing, real answer to "would this trade
# have gone on to actually hit target after being stopped out early" --
# exactly the question this rule's own adoption should keep being
# checked against, not just backtested once and trusted forever.
BREAKEVEN_ARM_R = 0.5

# --- Market-bias gating (see scanner.compute_market_bias) ---
# compute_market_bias() produces a top-down bullish/bearish/neutral read
# from trend, RSI, ROC and PCR. Until this setting existed it was logged
# and then DISCARDED -- nothing filtered candidates by it, so the system
# could and did buy puts while its own bias module read the tape the
# other way. tag_bias_conflicts() noticed CE and PE both being approved
# at one strike and responded by appending a string; the trade still
# opened.
#
#   "block"    -- reject candidates opposing a strong directional bias
#   "penalise" -- subtract BIAS_CONFLICT_PENALTY from their score instead
#   "off"      -- previous behaviour: log the bias, gate nothing
#
# Starting at "penalise" rather than "block" on purpose: the bias read is
# itself unvalidated, and a hard block on an unvalidated signal can
# silently halve the trade count for reasons nobody notices. Penalising
# keeps genuinely strong counter-bias setups reachable while making the
# conflict cost something. Revisit once there is replay evidence that
# counter-bias trades really do underperform.
BIAS_GATING_MODE = "penalise"
BIAS_CONFLICT_PENALTY = 1.0
# |bias score| at or above which the read counts as "strong" enough to
# act on at all. compute_market_bias labels +-1.0 as directional.
BIAS_STRONG_THRESHOLD = 1.5

# --- Transaction costs (see costs.py) ---
# Every P&L figure was GROSS until these existed. At this trade size that
# is not a rounding error: a round trip on a ~Rs 77 premium costs roughly
# 0.038R in statutory charges alone, versus a measured gross expectancy
# of -0.028R at the 2R target. Costs are larger than the entire measured
# edge deficit.
#
# THESE ARE ASSUMPTIONS -- replace with your broker's actual contract
# note values. Brokerage models differ and statutory rates change by
# circular. Note that STT on options is charged on the SELL side only,
# and on premium rather than notional.
BROKERAGE_PER_ORDER = 20.0      # flat per executed order (typical discount broker)
STT_RATE_SELL = 0.001           # 0.10% of premium, sell side only
EXCHANGE_TXN_RATE = 0.00035     # NSE options, applied to both legs
SEBI_TURNOVER_RATE = 0.000001   # SEBI turnover fee
STAMP_DUTY_RATE_BUY = 0.00003   # buy side only
GST_RATE = 0.18                 # on brokerage + exchange + SEBI

# --- Realistic fills (see models.OptionQuote.buy_price / sell_price) ---
# When True, a new position is priced at the ASK and an exit at the BID,
# instead of marking both at LTP. LTP is the last TRADED price: it can be
# stale and it sits on whichever side that trade hit, so pricing both
# legs there quietly books a profit that never existed. On a 76-rupee
# premium with a 1-point spread that bias is ~2.4% of premium per round
# trip, against a 1R stop of ~30% -- roughly 0.08R of pure fiction on
# every trade, which is larger than the entire measured edge.
#
# Falls back to LTP per-quote wherever the source publishes no book
# (CSV/TradingView tiers), and flags on the trade which basis was used.
USE_BID_ASK_FILLS = True

# --- Liquidity screen (applied at candidate selection, see scanner.py) ---
# A tradeable premium is not the same as a tradeable contract. Without
# these, a strike showing a plausible price on 50 lots of volume can be
# planned, sized and journalled at a fill that could never have happened.
# Set any of these to None to disable that particular check.
#
# NOTE: like PREMIUM_MIN/MAX, these apply ONLY to NEW candidate selection
# and must never filter the chain itself -- open trades still need their
# quote found even if the strike goes illiquid (2026-07-22 lesson).
MIN_OI_TO_TRADE = 5000        # contracts of open interest
MIN_VOLUME_TO_TRADE = 1000    # contracts traded today
MAX_SPREAD_PCT = 3.0          # bid-ask spread as % of mid; None to disable

# --- Market regime context (see market_regime.py) ---
# How far back to build the "what does a normal day's range look like"
# reference distribution. Refetched at most once a day and cached in
# state/regime_baseline.json.
#
# Why this is logged every cycle: reconstructing it after the fact on
# 2026-07-30 showed all seven recorded sessions had a daily range below
# the 6-month median, between the 1st and 45th percentile -- while 48%
# of normal NIFTY days move at least 1.0% and 20% move at least 1.5%,
# neither of which we had recorded even once. A directional strategy was
# being judged entirely on the calmest sliver of conditions. That should
# be visible while a session runs, not excavated a week later.
REGIME_LOOKBACK_DAYS = 180
REGIME_QUIET_PERCENTILE = 25      # below this percentile of trailing range -> "quiet"
REGIME_VOLATILE_PERCENTILE = 75   # above -> "volatile"

# --- Snapshot recording (see snapshot_recorder.py / replay.py) ---
# Records the raw option chain + candles behind every decision cycle, so
# any future version of the decision logic can be replayed against the
# same market history offline.
#
# Why this is on by default: without it, data collection and decision
# logic are welded together -- the only way to gather evidence is to run
# the live logic, so every change to that logic throws away all prior
# evidence. At ~4 trades/day and a per-trade R standard deviation near
# 1.0, proving an edge forward would take 800+ trades (~200 trading days)
# of UNCHANGED logic. Recording decouples the two: history accumulates no
# matter what the code does.
#
# Cost is ~9 MB/day raw, roughly 1 MB/day gzipped at a 30s cadence.
# Recording is best-effort and can never interrupt the live loop.
RECORD_SNAPSHOTS = True

# --- R-multiple milestone tracking ---
# "R" is the trade's own risk unit: 1R = entry - stop. A trade that gains
# exactly its risk distance has reached +1.0R. DEFAULT_TARGET_RR above is
# therefore literally "how many R we're aiming for."
#
# Every open trade records which of these multiples it actually touched
# and when. This is evidence-gathering ONLY -- it changes no behaviour and
# does not move the target. The point is to be able to answer, from real
# data rather than intuition, "if the target had been 1.2R instead of
# 2.0R, how many of these trades would have won?"
#
# Motivation: across the first 29 journalled trades only 2 ever reached
# +60% (the 2.0R target on a 30% stop) at ANY point while open, and the
# median best-move-ever was +20.3%. That suggests the target is set well
# beyond what these setups deliver -- but 29 trades is far too small a
# sample to retune a live parameter on, so the system keeps trading its
# current target while logging what it WOULD have hit.
RR_MILESTONES = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

# --- Volatility-derived plan geometry ---
# The flat percentages above take no account of what the underlying is
# actually capable of moving. On 2026-07-28's 24050 PE (real intraday
# range 51.15 - 102.55) the fixed +60% target sat at 122.72, about 20
# points ABOVE the highest price the option traded all day -- unreachable
# from the moment it was set -- while the -30% stop at 53.69 sat INSIDE
# the option's normal oscillation band. Stop inside the noise and target
# outside the range is losing geometry no matter how good the signal is.
#
# When enabled and the data allows it, stop/target distance is derived
# from what the underlying actually moves: ATR over ATR_PERIOD candles,
# scaled into premium terms by the option's delta, since a delta-0.4
# option moves ~0.4 points per point of NIFTY.
USE_VOLATILITY_BASED_PLAN = True
ATR_PERIOD = 14
# How many ATRs of adverse underlying movement the stop should sit beyond.
# Below 1.0 the stop is inside a single average candle's range and will be
# taken out by noise alone.
STOP_ATR_MULTIPLE = 1.5
# Guard rails: even volatility-derived stops stay inside these premium
# percentages, so a freak ATR reading can't produce an absurd plan.
MIN_STOP_PCT = 15.0
MAX_STOP_PCT = 40.0

# --- Price-action / structure thresholds (OB, FVG, S/R, sweeps) ---
SWING_LOOKBACK = 5               # candles on each side to confirm a swing high/low
OB_MIN_MOVE_PCT = 0.3            # min % move away from a candle to qualify it as an order block
SR_CLUSTER_TOLERANCE_PCT = 0.15  # swing points within this % of each other cluster into one S/R level
SR_MIN_TOUCHES = 2               # minimum touches for a cluster to count as a real S/R level
SWEEP_WICK_MIN_PCT = 0.1         # min wick-beyond-level size (%) to count as a liquidity sweep
PRICE_LEVEL_PROXIMITY_PCT = 0.25 # how close spot must be to a level for it to count as confluence
CANDLE_INTERVAL_MINUTES = "5"    # Dhan intraday interval: "1","5","15","25","60"

# How many candles a one-off structural event (FVG, OB, sweep, breakout,
# pullback) stays "current" for. Support/resistance is exempt -- it's
# defined by repeated touches over time, not a single event.
#
# Why this exists: price_action.analyze() used to return every level ever
# detected in the series, forever. Combined with scanner.py adding a flat
# score per nearby level, a strike's score could only ever ratchet UPWARD
# through a session. Measured on 2026-07-28's 24050 PE: 0 levels at 09:16,
# 14 by 14:00, never once decreasing -- and the raw score climbed 6.0 ->
# 11.0 almost entirely on that count, which is what let the same failed
# setup re-qualify at a HIGHER conviction after being stopped out twice.
# 24 candles at the default 5-minute interval is about two hours.
LEVEL_MAX_AGE_CANDLES = 24

# Cap on how much ALL price-action levels combined can contribute to one
# strike's score, and how much a single level is worth. Uncapped
# per-level accumulation is what let structure noise dominate the score.
MAX_LEVEL_SCORE_CONTRIBUTION = 2.0
LEVEL_SCORE_EACH = 0.5
SWEEP_SCORE_EACH = 0.75

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

# --- Bank Nifty context / divergence ---
# See banknifty_context.py. Context-only signal (not traded) -- checks
# whether Bank Nifty is confirming or fighting NIFTY's move today, since
# financials are ~30-35% of NIFTY 50's weight.
BANKNIFTY_CACHE_MINUTES = 5   # main_live.py refetches Bank Nifty at most this often, not every 30s poll

# --- Smart money / participant-wise OI (see participant_oi.py) ---
# How far FII+Pro's combined net-call minus net-put position (in
# contracts) has to lean before counting as "bullish"/"bearish" rather
# than "neutral". This is an unresearched starting assumption -- NIFTY's
# index options OI runs in the hundreds of thousands to millions of
# contracts most days, so tune this once you've seen a few real reports.
SMART_MONEY_NEUTRAL_BAND = 50000

# --- Volume profile (see volume_profile.py) ---
# Built from OHLCV bars (Dhan doesn't give us a tick-level trade tape),
# using the standard "spread each candle's volume evenly across its
# high-low range" approximation. Bin size is your resolution/noise
# tradeoff -- 10 points is a starting assumption for NIFTY's current
# level (~24-25k), not a researched optimum.
VOLUME_PROFILE_BIN_POINTS = 10
VALUE_AREA_TARGET_PCT = 0.70   # standard convention (roughly "one std dev" of volume)
HVN_MULTIPLE = 1.5             # bin volume >= this x average -> high-volume node
LVN_MULTIPLE = 0.5             # bin volume <= this x average -> low-volume node

# --- Fast position check (added 2026-07-27) ---
# Between full scan cycles (POLL_INTERVAL_SECONDS, 30s), a big favorable
# move can spike through target and back again without ever being seen
# by a 30s-granularity snapshot -- e.g. price touches 160 against a 160
# target, then drops to 150 seconds later, and neither snapshot around
# it ever shows 160. This runs a MUCH lighter check every
# FAST_CHECK_INTERVAL_SECONDS, but ONLY re-checks already-open trades'
# target/stop -- it does not scan for new candidates, run OI analytics,
# news, etc. Currently still re-fetches the FULL option chain each time
# (simple, safe, reuses fully-tested code) -- see BACKLOG.md for the
# lighter-weight version (Dhan's marketfeed/ltp endpoint, needs security-
# ID resolution) planned before going live with real capital.
FAST_CHECK_INTERVAL_SECONDS = 5

# --- Trade tracking ---
# The scanner re-evaluates the whole chain every cycle, which is correct
# for FINDING setups but wrong for TRACKING one: without a cap, "highest
# scoring option this cycle" silently becomes a new plan every 90 seconds
# even when it's really the same underlying setup drifting. These two
# settings force it to commit to a small number of high-conviction trades
# per day and follow each one to its actual outcome instead.
MAX_NEW_TRADES_PER_DAY = 999         # effectively uncapped -- training/evaluation phase, more trades = faster sample building
MIN_CONVICTION_SCORE_TO_TRACK = 5.0  # well above the 1.5 watchlist bar — only strong setups get tracked
# --- Expiry-day rules ---
# On 2026-07-28 all four trades were same-day-expiry LONG options, and
# nothing in the scanner, plan generator, or risk checker knew it. That
# matters for a premium BUYER specifically: on expiry day an option is
# almost entirely extrinsic value decaying to zero by 15:30, so theta is
# at its most brutal and a fixed reward-multiple target gets less
# reachable with every hour that passes. The last trade of that day was
# opened at 14:32 and closed at 15:29 for -16.8%.
#
# These are deliberately conservative rather than a ban: same-day expiry
# is tradeable, it just needs a higher bar and a cutoff after which
# buying decaying premium stops making sense.
EXPIRY_DAY_EXTRA_CONVICTION = 1.5   # added to MIN_CONVICTION_SCORE_TO_TRACK on expiry day
EXPIRY_DAY_NO_NEW_TRADES_AFTER = "14:00"   # IST; no new same-day-expiry longs after this

JOURNAL_LOOKBACK_FOR_LEARNING = 100  # how many recent journal entries to consider for tag win-rate adjustment

# --- Learned tag adjustment (see trade_tracker.apply_learned_adjustment) ---
# REWRITTEN 2026-07-30. The previous design penalised a tag by a flat
# -0.5 whenever its win rate sat below an ABSOLUTE 0.4, with only 3
# samples required. Measured against two real sessions, that was wrong
# in three separate ways at once:
#
#   1. NOT ENOUGH DATA. At n=3, a 1W/2L record has a 95% confidence
#      interval of roughly 6%-79% -- indistinguishable from a coin flip.
#      Every penalised tag's interval spanned 50%.
#   2. WRONG BASELINE. The journal's own base win rate was 22%, so ALL
#      five tags with samples sat below the 0.4 threshold and every one
#      was penalised -- while every one was actually AT OR ABOVE the
#      system's own average. The adjustment was penalising tags for the
#      strategy's overall losing record, not discriminating between them.
#   3. PERVERSE SCALING. Because every tag was "weak" and penalties
#      stacked across correlated tags (fvg and long_buildup co-occur
#      constantly), the total penalty grew with the raw score
#      (correlation +0.385; mean penalty 0.5 at weak candidates rising
#      to 1.88 at score 5.0). More confirming signals meant a bigger
#      handicap -- a progressive tax on conviction, exactly backwards.
#      On 2026-07-30 it alone blocked 15 candidates that had cleared
#      the raw bar, contributing to a second consecutive zero-trade day.
#
# The replacement measures each tag against the journal's OWN base win
# rate and shrinks toward it (empirical Bayes), so a small sample barely
# moves the score at all and only a genuinely differentiated tag with
# real weight behind it can shift anything.

# Below this many closed WIN/LOSS trades in total, apply NO adjustment
# whatsoever. With single-digit trade counts there is nothing to learn
# and pretending otherwise actively suppressed trading.
MIN_TRADES_FOR_ANY_ADJUSTMENT = 30

# A tag needs at least this many of its own outcomes before it's
# considered at all. Shrinkage below already handles small samples
# gracefully; this is a second, blunter floor.
MIN_TAG_SAMPLES_FOR_ADJUSTMENT = 10

# Strength of the prior, in "virtual trades held at the base rate".
# A tag's effective rate is (wins + k*base) / (n + k). At n=9 with k=25
# a 2W/7L tag shrinks to within 0.1pp of the base rate -- i.e. ~zero
# adjustment, which is the correct answer for that little evidence.
TAG_PRIOR_STRENGTH = 25

# Score points per 1.0 of (shrunk_rate - base_rate). A tag genuinely
# running 25pp above base with heavy sample weight earns about +0.5.
TAG_ADJUSTMENT_SCALE = 2.0

# Hard cap on the TOTAL adjustment across all of a candidate's tags,
# in either direction. Tags are strongly correlated, so uncapped
# summing double-counts the same underlying market condition.
MAX_TOTAL_TAG_ADJUSTMENT = 1.0

# Display-only labels for summarize_recent_lessons(); these no longer
# drive the adjustment itself (see the base-relative logic above).
WEAK_TAG_WIN_RATE = 0.4
STRONG_TAG_WIN_RATE = 0.65

# Re-entry gate after a stop-loss.
#
# The pathology this targets (seen on 2026-07-28) isn't "re-entered the
# same strike" -- it's "re-entered the same strike at the same PRICE with
# the same plan that just failed." 24050 PE was entered at 75.7, stopped;
# re-entered at 76.7, stopped; re-entered at 76.7 again. All three had
# effectively identical entry/target/stop, so the third attempt was
# re-running an experiment that had already failed twice.
#
# A blanket time cooldown was considered first and rejected: it both
# over-blocks (a genuinely different setup at a different premium is a
# different bet, and shouldn't be blocked just because the clock hasn't
# run out) and under-blocks (on the real 2026-07-28 data a 60-minute
# window caught only one of the two re-entries -- the other came 2h31m
# after its stop). Price similarity catches BOTH, with no arbitrary clock.
#
# A new entry is blocked if a trade on the same strike+type was stopped
# out earlier today at an entry price within this % of the new one.
# Only a LOSS arms this -- a WIN or EOD_CLOSE isn't evidence the setup
# was wrong. State resets daily along with the rest of open_trades.json.
REENTRY_PRICE_TOLERANCE_PCT = 3.0

# Direction-scoped chase cooldown -- a DIFFERENT pathology from the
# price-tolerance gate above, which is keyed by (strike, option_type)
# and therefore blind to a losing read repeated across DIFFERENT
# strikes. Seen on 2026-08-10: 8 CE trades, 8 different strikes
# (24550 through 24900), one after another as each got stopped out --
# no two shared a strike, so REENTRY_PRICE_TOLERANCE_PCT never fired
# once. Rs -5,153 for the day.
#
# Measured against the full live trade journal (44 trades, 2026-07-21
# through 2026-08-10) before adding this: 93% of trades (41/44)
# belonged to a same-day, same-direction cluster opened within this
# many minutes of the prior same-direction trade's close. Actual
# cluster P&L: Rs -12,476. Blocking continuation ONLY after a loss
# (matching REENTRY_PRICE_TOLERANCE_PCT's own "only a LOSS arms this"
# rule -- a win isn't evidence the read was wrong) would have kept
# Rs +518 instead. Still true with 2026-08-10 excluded entirely
# (Rs -7,324 actual vs Rs +1,735 with the rule), so this isn't just
# today's outlier.
#
# 30 minutes matches the gap used in that measurement, not a separately
# tuned value -- revisit if live data suggests a different window.
DIRECTION_CHASE_COOLDOWN_MINUTES = 30
