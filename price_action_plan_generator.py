"""
Turns a Setup (from price_action_scanner.py) + its StructureSignal into a
concrete TradePlan. Mirrors plan_generator.py's build_plan() shape, but
the stop distance comes from the structural break itself rather than
ATR: the doc's own rule already gives an index-point risk distance
(entry_reference to stop_reference), which just needs converting into
option premium points via delta -- the same conversion plan_generator.py
does for its ATR-based stop, kept here as its own copy rather than a
shared helper so this strategy's risk sizing can't silently drift if the
momentum scanner's ever changes.
"""

import config_price_action as pcfg
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


def _stop_distance(entry: float, quote, index_risk_points: float) -> tuple:
    """
    Returns (stop_distance_in_premium_points, how_it_was_derived).

    index_risk_points is |entry_reference - stop_reference| from the
    StructureSignal -- how far the underlying itself moved to invalidate
    the setup. Converted to premium points via the option's own delta,
    same as plan_generator.py's ATR path, then clamped into the
    configured MIN/MAX_STOP_PCT band so a very tight or very wide
    structural stop can't produce a degenerate position size.
    """
    flat = entry * (pcfg.MIN_STOP_PCT / 100)
    if not quote.delta:
        return flat, f"flat {pcfg.MIN_STOP_PCT}% of premium (no delta on this quote)"

    premium_move = index_risk_points * abs(quote.delta)
    low = entry * (pcfg.MIN_STOP_PCT / 100)
    high = entry * (pcfg.MAX_STOP_PCT / 100)
    clamped = max(low, min(high, premium_move))

    note = (
        f"{index_risk_points:.1f} index pts x delta {abs(quote.delta):.2f} "
        f"= {premium_move:.2f} premium pts"
    )
    if clamped != premium_move:
        note += f", clamped to {clamped:.2f} by the {pcfg.MIN_STOP_PCT}-{pcfg.MAX_STOP_PCT}% band"
    return clamped, note


def build_plan(snapshot, setup, signal) -> TradePlan:
    """
    `signal` is the StructureSignal that produced this setup -- carries
    the index-point stop distance the structural break itself implies.
    """
    quote = _find_quote(snapshot, setup)
    if quote is None:
        raise ValueError("Matching option quote not found in snapshot for this setup")

    use_book = quote.has_book
    entry = quote.buy_price if use_book else quote.ltp
    entry_basis = (
        f"ask {quote.ask} (bid {quote.bid}, spread {quote.spread_pct}%)"
        if use_book else f"LTP {quote.ltp} (no book available from this source)"
    )

    index_risk_points = abs(signal.entry_reference - signal.stop_reference)
    risk_per_unit, stop_basis = _stop_distance(entry, quote, index_risk_points)
    stop = round(max(0.05, entry - risk_per_unit), 2)
    risk_per_unit = entry - stop  # recompute after the floor/rounding
    target = round(entry + risk_per_unit * pcfg.TARGET_RR, 2)

    invalidation = (
        f"Close back inside the broken consolidation "
        f"(structural stop reference {signal.stop_reference:.1f})"
    )

    max_risk_rupees = pcfg.TOTAL_CAPITAL * (pcfg.MAX_RISK_PER_TRADE_PCT / 100)
    risk_per_lot = risk_per_unit * pcfg.NIFTY_LOT_SIZE
    lots_by_risk = int(max_risk_rupees // risk_per_lot) if risk_per_lot > 0 else 0
    lots = max(0, min(lots_by_risk, pcfg.MAX_LOTS_PER_TRADE))

    capital_at_risk = round(lots * risk_per_lot, 2)
    risk_pct_of_capital = round((capital_at_risk / pcfg.TOTAL_CAPITAL) * 100, 2)

    if risk_pct_of_capital == 0:
        risk_level = "None (0 lots -- risk budget too small for this setup)"
    elif risk_pct_of_capital <= pcfg.MAX_RISK_PER_TRADE_PCT * 0.5:
        risk_level = "Low"
    elif risk_pct_of_capital <= pcfg.MAX_RISK_PER_TRADE_PCT:
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
