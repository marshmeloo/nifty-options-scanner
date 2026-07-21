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


def build_plan(snapshot, setup) -> TradePlan:
    quote = _find_quote(snapshot, setup)
    if quote is None:
        raise ValueError("Matching option quote not found in snapshot for this setup")

    entry = quote.ltp
    stop = round(entry * (1 - config.DEFAULT_STOP_LOSS_PCT / 100), 2)
    risk_per_unit = entry - stop
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
    )
