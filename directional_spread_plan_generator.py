"""
Turns the legs found by directional_spread_scanner.py into a priced
DirectionalSpreadPlan: net credit, max profit, max loss, and the single
breakeven point (only one, since this is a one-sided spread -- the
condor's two-sided breakeven_upper/breakeven_lower doesn't apply here).
"""

import config_directional_spread as dcfg
from models import DirectionalSpreadPlan


def build_directional_spread_plan(legs: dict, expiry: str, bias_label: str, bias_score: float):
    """
    `legs` is directional_spread_scanner.find_directional_spread_legs()'s
    output. Returns None if there's no direction, or the direction was
    picked but a leg is missing -- an incomplete spread is not a
    position to open partially.
    """
    direction, short, hedge = legs["direction"], legs["short"], legs["hedge"]
    if direction is None or short is None or hedge is None:
        return None

    lot_size = dcfg.NIFTY_LOT_SIZE
    lots = dcfg.LOTS_PER_LEG

    net_credit = short.ltp - hedge.ltp
    net_credit_inr = round(net_credit * lot_size * lots, 2)

    wing_width = (
        short.strike - hedge.strike if direction == "PE"
        else hedge.strike - short.strike
    )
    max_loss_points = wing_width - net_credit
    max_loss_inr = round(max_loss_points * lot_size * lots, 2)

    breakeven = short.strike - net_credit if direction == "PE" else short.strike + net_credit

    return DirectionalSpreadPlan(
        expiry=expiry,
        direction=direction,
        bias_label=bias_label,
        bias_score=bias_score,
        short_strike=short.strike,
        short_premium=short.ltp,
        hedge_strike=hedge.strike,
        hedge_premium=hedge.ltp,
        lots=lots,
        net_credit=round(net_credit, 2),
        net_credit_inr=net_credit_inr,
        max_loss_inr=max_loss_inr,
        max_profit_inr=net_credit_inr,
        breakeven=breakeven,
    )
