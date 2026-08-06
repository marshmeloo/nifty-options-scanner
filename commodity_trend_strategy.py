"""
Trend-following strategies for MCX commodity futures (Gold, Silver),
built in response to the price-action zone strategy's finding
(BACKLOG.md, 2026-08-06): gold and silver spent 2021-2026 in an
unusually persistent one-way bull run, structurally the opposite of
what a mean-reversion-to-zone rule needs. A trend-following shape --
Supertrend, moving-average crossover -- fits that market character
directly instead of fighting it.

RSI IS DELIBERATELY NOT THE PRIMARY SIGNAL HERE: it is a mean-reversion
oscillator (overbought/oversold), and can stay "overbought" for months
in a real trend -- the same failure mode already found with the zone
strategy, just via a different mechanism. It is exposed as an optional
FILTER (avoid entering against an extreme reading) rather than a
standalone entry signal.

Both strategies here are "always reversing" systems: enter on a flip,
hold until the opposite flip, no separate profit target -- this is the
idiomatic way Supertrend in particular is used (the indicator line
itself is a trailing stop that rides the trend), and MA crossover is
given the same shape for a fair, comparable backtest rather than
inventing a different exit rule per strategy.

No pandas/numpy in this project (see requirements.txt) -- indicators
are hand-rolled plain Python, same convention as price_action.py's own
swing-point detection.

NOTHING HERE PLACES A REAL BROKER ORDER -- backtest only.
"""

from dataclasses import dataclass
from datetime import date as _date
from typing import Optional

import commodity_source as csrc

LOT_SIZE = {"GOLD": 1.0, "SILVER": 1.0}

# Same rollover-seam duplicate dates documented in BACKLOG.md and
# already excluded in shadow_commodity_futures.py -- kept identical
# here rather than re-derived, since it's the same underlying data.
KNOWN_BAD_DATES = {
    _date(2022, 9, 22), _date(2023, 5, 5), _date(2023, 5, 8),
    _date(2023, 5, 9), _date(2023, 5, 10), _date(2023, 10, 20), _date(2024, 8, 22),
}


def clean_daily_candles(symbol: str) -> list:
    candles = csrc.load_daily(symbol)
    return [c for c in candles if c.timestamp.date() not in KNOWN_BAD_DATES]


# --- Indicators -------------------------------------------------------

def true_range(prev_close: Optional[float], high: float, low: float) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_series(candles: list, period: int) -> list:
    """Wilder's ATR (the standard smoothing Supertrend itself is defined
    against). First `period` entries are None -- not enough history yet
    to seed the smoothed average."""
    trs = [true_range(candles[i - 1].close if i > 0 else None, c.high, c.low)
          for i, c in enumerate(candles)]
    out = [None] * len(candles)
    if len(candles) < period:
        return out
    seed = sum(trs[1:period + 1]) / period  # first true ATR uses period TRs, excluding the undefined bar-0 TR
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def sma_series(candles: list, period: int) -> list:
    out = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        out[i] = sum(c.close for c in candles[i - period + 1:i + 1]) / period
    return out


def rsi_series(candles: list, period: int = 14) -> list:
    """Wilder's RSI. Exposed for the optional filter use-case described
    in the module docstring -- not used by either backtest below unless
    a caller explicitly passes rsi_filter=True."""
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(candles)):
        change = candles[i].close - candles[i - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = float("inf") if avg_loss == 0 else avg_gain / avg_loss
        out[i + 1] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + rs)
    return out


def supertrend_series(candles: list, period: int = 10, multiplier: float = 3.0) -> list:
    """
    Returns a list of ("up"|"down"|None, value) per bar -- "up" means
    price is trading above the line (the line acts as a trailing stop
    below price, i.e. a long trend); "down" is the mirror. None where
    ATR isn't warmed up yet.

    Standard iterative definition (period=10, multiplier=3 are the
    common TradingView/broker-platform defaults, used here as the
    starting point to sweep around, not assumed correct).
    """
    atr = atr_series(candles, period)
    n = len(candles)
    direction = [None] * n
    line = [None] * n
    final_upper = [None] * n
    final_lower = [None] * n

    for i in range(n):
        if atr[i] is None:
            continue
        c = candles[i]
        mid = (c.high + c.low) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        prev_i = i - 1
        if prev_i >= 0 and final_upper[prev_i] is not None:
            final_upper[i] = basic_upper if (basic_upper < final_upper[prev_i] or candles[prev_i].close > final_upper[prev_i]) else final_upper[prev_i]
            final_lower[i] = basic_lower if (basic_lower > final_lower[prev_i] or candles[prev_i].close < final_lower[prev_i]) else final_lower[prev_i]
        else:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower

        if prev_i >= 0 and direction[prev_i] is not None:
            if direction[prev_i] == "down":
                direction[i] = "up" if c.close > final_upper[i] else "down"
            else:
                direction[i] = "down" if c.close < final_lower[i] else "up"
        else:
            direction[i] = "up" if c.close > final_upper[i] else "down"

        line[i] = final_lower[i] if direction[i] == "up" else final_upper[i]

    return list(zip(direction, line))


# --- Backtest ----------------------------------------------------------

@dataclass
class TrendTrade:
    symbol: str
    strategy: str
    direction: str       # "up" (long) or "down" (short)
    opened_at: str
    entry: float
    initial_stop: float  # the trailing-stop/line value AT ENTRY, for R-normalisation only
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    net_r: Optional[float] = None
    net_inr: Optional[float] = None


def _finalise(trade: TrendTrade, closed_at, exit_price: float, lot_size: float) -> TrendTrade:
    risk_per_unit = abs(trade.entry - trade.initial_stop)
    pnl_per_unit = (exit_price - trade.entry) if trade.direction == "up" else (trade.entry - exit_price)
    trade.closed_at = closed_at.isoformat()
    trade.exit_price = exit_price
    trade.net_inr = round(pnl_per_unit * lot_size, 2)
    trade.net_r = round(pnl_per_unit / risk_per_unit, 4) if risk_per_unit else None
    return trade


def backtest_supertrend(symbol: str, period: int = 10, multiplier: float = 3.0) -> list:
    """
    Always-in-the-market reversal system: flip position on every
    Supertrend direction change. First flip after warm-up opens the
    first trade; every later flip closes the open trade at that bar's
    close and immediately opens the opposite one.
    """
    candles = clean_daily_candles(symbol)
    lot_size = LOT_SIZE[symbol]
    st = supertrend_series(candles, period, multiplier)

    trades = []
    open_trade = None
    prev_direction = None

    for i, (direction, line) in enumerate(st):
        if direction is None:
            continue
        if prev_direction is None:
            prev_direction = direction
            continue
        if direction != prev_direction:
            c = candles[i]
            if open_trade is not None:
                trades.append(_finalise(open_trade, c.timestamp.date(), c.close, lot_size))
            open_trade = TrendTrade(symbol=symbol, strategy="supertrend", direction=direction,
                                    opened_at=c.timestamp.date().isoformat(), entry=c.close, initial_stop=line)
        prev_direction = direction

    return trades


def backtest_ma_crossover(symbol: str, fast: int = 20, slow: int = 50, atr_period: int = 14) -> list:
    """
    Same always-in-the-market shape as Supertrend, for a fair
    comparison. MA crossover has no natural stop-line the way
    Supertrend does, so the initial risk reference for R-normalisation
    is 1x ATR at entry -- a conventional, not fitted, choice.
    """
    candles = clean_daily_candles(symbol)
    lot_size = LOT_SIZE[symbol]
    fast_ma = sma_series(candles, fast)
    slow_ma = sma_series(candles, slow)
    atr = atr_series(candles, atr_period)

    trades = []
    open_trade = None
    prev_state = None  # "up" if fast > slow, "down" if fast < slow

    for i in range(len(candles)):
        if fast_ma[i] is None or slow_ma[i] is None or atr[i] is None:
            continue
        state = "up" if fast_ma[i] > slow_ma[i] else "down"
        if prev_state is None:
            prev_state = state
            continue
        if state != prev_state:
            c = candles[i]
            if open_trade is not None:
                trades.append(_finalise(open_trade, c.timestamp.date(), c.close, lot_size))
            initial_stop = c.close - atr[i] if state == "up" else c.close + atr[i]
            open_trade = TrendTrade(symbol=symbol, strategy="ma_crossover", direction=state,
                                    opened_at=c.timestamp.date().isoformat(), entry=c.close, initial_stop=initial_stop)
        prev_state = state

    return trades
