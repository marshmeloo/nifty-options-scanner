"""
Backtest driver for the price-action strategy traded DIRECTLY on MCX
commodity futures (Gold, Silver) -- go long/short the future itself on
a price_structure.generate_signal() break, no option premium/strike
selection at all.

WHY FUTURES INSTEAD OF OPTIONS HERE: probed 2026-08-06 (see
BACKLOG.md) -- MCX commodity OPTIONS only have ~1 year of
reconstructable history via Dhan's rollingoption endpoint (vs 5 years
for the futures themselves) and a narrow ATM+/-3 strike window (vs
NIFTY's ATM+/-10). A premium-band sweep on that little data would be
noise, not signal -- the same overfitting risk already documented
twice this session, just worse. The futures themselves have a genuine
5-year continuous daily series (commodity_source.py) with no strike
selection needed at all, so this trades the future directly.

TIMEFRAME PAIR: only "weekly_daily" exists here (HTF=weekly zones,
LTF=daily trigger) -- the shape of NIFTY price-action's "daily_hourly"
pair, one level coarser, because daily is the FINEST granularity we
have 5 years of for commodities (no intraday backfill exists yet, see
BACKLOG.md's Gold/Silver entry). No "intraday" pair equivalent.

CONTAMINATION: excludes the ~27 rollover-seam duplicate dates found in
commodity_source's data (BACKLOG.md) rather than guessing which of two
conflicting OHLC sets is real.

NO CONTRACT EXPIRY TO SETTLE AGAINST (unlike an option): a trade that
hits neither target nor stop is force-closed after MAX_HOLD_DAYS at
that day's close -- a genuinely new rule this strategy needs that the
option-based version didn't (an option had its own real expiry as a
natural forced exit).

NOTHING HERE PLACES A REAL BROKER ORDER -- backtest only.
"""

from dataclasses import dataclass
from datetime import date as _date
from typing import Optional

import commodity_source as csrc
import price_structure as ps

TARGET_RR_DEFAULT = 2.0
MAX_HOLD_DAYS_DEFAULT = 20  # ~1 trading month; no natural expiry to fall back on
SWING_LOOKBACK_MIN_BARS = 12  # generate_signal's own floor: SWING_LOOKBACK*2+2 at defaults

LOT_SIZE = {"GOLD": 1.0, "SILVER": 1.0}  # confirmed from Dhan's instrument master, 2026-08-06

# Rollover-seam duplicate dates found in commodity_source's backfill
# (BACKLOG.md, added 2026-08-06) -- excluded rather than guessed at.
KNOWN_BAD_DATES = {
    _date(2022, 9, 22), _date(2023, 5, 5), _date(2023, 5, 8),
    _date(2023, 5, 9), _date(2023, 5, 10), _date(2023, 10, 20), _date(2024, 8, 22),
}


@dataclass
class FuturesTrade:
    symbol: str
    direction: str          # "bullish" or "bearish"
    opened_at: str
    entry: float
    stop: float
    target: float
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None      # "WIN" | "LOSS" | "TIME_EXIT" | None (unresolved)
    net_r: Optional[float] = None
    net_inr: Optional[float] = None


def _clean_daily_candles(symbol: str) -> list:
    candles = csrc.load_daily(symbol)
    return [c for c in candles if c.timestamp.date() not in KNOWN_BAD_DATES]


def _weekly_bars(daily_candles: list) -> list:
    """Group daily bars into ISO-calendar-week OHLC bars. A trailing
    partial week (fewer days than a full week has seen so far) is still
    emitted AS OF each day in the walk-forward below -- the caller only
    ever passes bars up to "today", so the current week's bar is
    legitimately partial-so-far, same as a live in-progress candle."""
    if not daily_candles:
        return []
    weeks = {}
    order = []
    for c in daily_candles:
        key = c.timestamp.isocalendar()[:2]  # (ISO year, ISO week)
        if key not in weeks:
            weeks[key] = []
            order.append(key)
        weeks[key].append(c)
    from models import Candle
    bars = []
    for key in order:
        group = weeks[key]
        bars.append(Candle(
            timestamp=group[0].timestamp, open=group[0].open,
            high=max(g.high for g in group), low=min(g.low for g in group),
            close=group[-1].close, volume=sum(g.volume for g in group),
        ))
    return bars


def _finalise(trade: FuturesTrade, closed_at, exit_price: float, outcome: str, lot_size: float) -> FuturesTrade:
    risk_per_unit = abs(trade.entry - trade.stop)
    if trade.direction == "bullish":
        pnl_per_unit = exit_price - trade.entry
    else:
        pnl_per_unit = trade.entry - exit_price
    trade.closed_at = closed_at.isoformat()
    trade.exit_price = exit_price
    trade.outcome = outcome
    trade.net_inr = round(pnl_per_unit * lot_size, 2)
    trade.net_r = round(pnl_per_unit / risk_per_unit, 4) if risk_per_unit else None
    return trade


def run(symbol: str, target_rr: float = TARGET_RR_DEFAULT,
       max_hold_days: int = MAX_HOLD_DAYS_DEFAULT, verbose: bool = False) -> list:
    """
    Full walk-forward backtest of the weekly_daily pair against
    `symbol`'s real daily futures history. Returns a list of
    FuturesTrade (only one open position at a time, same "no pyramiding"
    simplicity as shadow_price_action.py).
    """
    candles = _clean_daily_candles(symbol)
    lot_size = LOT_SIZE[symbol]
    trades = []
    open_trade = None
    open_idx = None

    for i in range(SWING_LOOKBACK_MIN_BARS, len(candles)):
        today = candles[i]

        if open_trade is not None:
            # Mark-to-market against today's bar: did stop or target trade first?
            # Conservative: check stop before target on the SAME bar (can't know
            # intrabar order from daily OHLC, and this project's own convention
            # elsewhere is to assume the worse outcome when both are possible).
            hit_stop = (today.low <= open_trade.stop) if open_trade.direction == "bullish" else (today.high >= open_trade.stop)
            hit_target = (today.high >= open_trade.target) if open_trade.direction == "bullish" else (today.low <= open_trade.target)

            if hit_stop:
                trades.append(_finalise(open_trade, today.timestamp.date(), open_trade.stop, "LOSS", lot_size))
                open_trade, open_idx = None, None
            elif hit_target:
                trades.append(_finalise(open_trade, today.timestamp.date(), open_trade.target, "WIN", lot_size))
                open_trade, open_idx = None, None
            elif i - open_idx >= max_hold_days:
                trades.append(_finalise(open_trade, today.timestamp.date(), today.close, "TIME_EXIT", lot_size))
                open_trade, open_idx = None, None
            if verbose and open_trade is None and trades:
                t = trades[-1]
                print(f"  {t.closed_at} CLOSED {t.direction} {t.outcome} net_r={t.net_r}")
            continue

        htf = _weekly_bars(candles[:i + 1])
        ltf = candles[:i + 1]
        try:
            signal = ps.generate_signal(htf, ltf)
        except Exception:
            continue
        if signal is None:
            continue

        entry = signal.entry_reference
        stop = signal.stop_reference
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + target_rr * risk if signal.direction == "bullish" else entry - target_rr * risk

        open_trade = FuturesTrade(symbol=symbol, direction=signal.direction,
                                  opened_at=today.timestamp.date().isoformat(),
                                  entry=entry, stop=stop, target=target)
        open_idx = i
        if verbose:
            print(f"  {today.timestamp.date()} OPEN {signal.direction} entry={entry:.1f} stop={stop:.1f} target={target:.1f}")

    return trades
