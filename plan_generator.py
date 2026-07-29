"""
Turns a flagged Setup + the matching option quote into a concrete TradePlan.
All numbers are derived from config parameters, never hardcoded per-trade.
"""

import config
from models import TradePlan


def _find_quote(snapshot, setup):
    for q in snapshot.chain:
        if (
            q.strike == setup.strike
            and q.option_type == setup.option_type
            and q.expiry == setup.expiry
        ):
            return q
    return None


def _stop_distance(entry: float, quote, atr) -> tuple:
    """
    Returns (stop_distance_in_premium_points, how_it_was_derived).

    Preferred path: size the stop off what the underlying actually moves.
    ATR is in NIFTY points; the option's delta converts that into premium
    points (a delta-0.4 option moves ~0.4 premium points per NIFTY point).
    A stop placed STOP_ATR_MULTIPLE ATRs away therefore sits outside
    normal intraday oscillation instead of inside it.

    Falls back to the flat DEFAULT_STOP_LOSS_PCT when volatility sizing is
    disabled or the inputs aren't available -- notably the NSE fallback
    tier, which returns no Greeks at all (delta is None there).
    """
    flat = entry * (config.DEFAULT_STOP_LOSS_PCT / 100)

    if not getattr(config, "USE_VOLATILITY_BASED_PLAN", False):
        return flat, f"flat {config.DEFAULT_STOP_LOSS_PCT}% of premium (volatility sizing disabled)"
    if atr is None or not quote.delta:
        missing = "no ATR (not enough candles)" if atr is None else "no delta on this quote"
        return flat, f"flat {config.DEFAULT_STOP_LOSS_PCT}% of premium ({missing})"

    premium_move = atr * abs(quote.delta) * config.STOP_ATR_MULTIPLE
    # Clamp into the configured band so an outlier ATR can't produce a
    # stop that's either meaninglessly tight or larger than the position.
    low = entry * (config.MIN_STOP_PCT / 100)
    high = entry * (config.MAX_STOP_PCT / 100)
    clamped = max(low, min(high, premium_move))

    note = (
        f"{config.STOP_ATR_MULTIPLE} x ATR({config.ATR_PERIOD}) of {atr} pts "
        f"x delta {abs(quote.delta):.2f} = {premium_move:.2f} premium pts"
    )
    if clamped != premium_move:
        note += f", clamped to {clamped:.2f} by the {config.MIN_STOP_PCT}-{config.MAX_STOP_PCT}% band"
    return clamped, note


def build_plan(snapshot, setup, atr=None) -> TradePlan:
    """
    `atr` is the underlying's Average True Range in NIFTY points (from
    price_action.compute_atr on the same candles the scanner already
    fetched). When supplied, stop/target are sized from real volatility
    rather than flat percentages -- see _stop_distance.
    """
    quote = _find_quote(snapshot, setup)
    if quote is None:
        raise ValueError("Matching option quote not found in snapshot for this setup")

    # Opening a long position crosses the ASK, not the last traded price.
    use_book = getattr(config, "USE_BID_ASK_FILLS", False) and quote.has_book
    entry = quote.buy_price if use_book else quote.ltp
    entry_basis = (
        f"ask {quote.ask} (bid {quote.bid}, spread {quote.spread_pct}%)"
        if use_book else f"LTP {quote.ltp} (no book available from this source)"
    )
    risk_per_unit, stop_basis = _stop_distance(entry, quote, atr)
    stop = round(max(0.05, entry - risk_per_unit), 2)
    risk_per_unit = entry - stop  # recompute after the floor/rounding
    target = round(entry + risk_per_unit * config.DEFAULT_TARGET_RR, 2)

    invalidation = (
        f"Close below stop ({stop}) on a 15-min candle, "
        f"or OI unwind reversing the current buildup direction"
    )

    # Position sizing from risk budget
    max_risk_rupees = config.TOTAL_CAPITAL * (config.MAX_RISK_PER_TRADE_PCT / 100)
    risk_per_lot = risk_per_unit * config.NIFTY_LOT_SIZE
    lots_by_risk = int(max_risk_rupees // risk_per_lot) if risk_per_lot > 0 else 0
    lots = max(0, min(lots_by_risk, config.MAX_LOTS_PER_TRADE))

    capital_at_risk = round(lots * risk_per_lot, 2)
    risk_pct_of_capital = round((capital_at_risk / config.TOTAL_CAPITAL) * 100, 2)

    if risk_pct_of_capital == 0:
        risk_level = "None (0 lots — risk budget too small for this setup)"
    elif risk_pct_of_capital <= config.MAX_RISK_PER_TRADE_PCT * 0.5:
        risk_level = "Low"
    elif risk_pct_of_capital <= config.MAX_RISK_PER_TRADE_PCT:
        risk_level = "Medium"
    else:
        risk_level = "High"

    return TradePlan(
        setup=setup,
        entry=entry,
        target=target,
        stop=stop,
        invalidation=invalidation,
        lots=lots,
        capital_at_risk=capital_at_risk,
        risk_pct_of_capital=risk_pct_of_capital,
        risk_level=risk_level,
        stop_basis=stop_basis,
        entry_basis=entry_basis,
    )
