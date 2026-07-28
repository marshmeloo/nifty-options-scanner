"""
Turns the 4 legs found by condor_scanner.py into a priced CondorPlan:
net credit, max profit, max loss (worst of the two wings -- price can
only breach one side by expiry), and breakeven points.
"""

import config_condor as ccfg
from models import CondorPlan


def build_condor_plan(legs: dict, expiry: str) -> CondorPlan:
    """
    `legs` is condor_scanner.find_condor_legs()'s output. Returns None if
    any leg is missing -- an incomplete condor (e.g. no strike available
    far enough out for a hedge) is not a position to open partially.
    """
    short_ce, short_pe = legs["short_ce"], legs["short_pe"]
    hedge_ce, hedge_pe = legs["hedge_ce"], legs["hedge_pe"]

    if not all([short_ce, short_pe, hedge_ce, hedge_pe]):
        return None

    lot_size = ccfg.NIFTY_LOT_SIZE
    lots = ccfg.LOTS_PER_LEG

    net_credit = (short_ce.ltp + short_pe.ltp) - (hedge_ce.ltp + hedge_pe.ltp)
    net_credit_inr = round(net_credit * lot_size * lots, 2)

    ce_wing_width = hedge_ce.strike - short_ce.strike
    pe_wing_width = short_pe.strike - hedge_pe.strike
    max_loss_points = max(ce_wing_width, pe_wing_width) - net_credit
    max_loss_inr = round(max_loss_points * lot_size * lots, 2)

    breakeven_upper = short_ce.strike + net_credit
    breakeven_lower = short_pe.strike - net_credit

    return CondorPlan(
        expiry=expiry,
        short_ce_strike=short_ce.strike,
        short_ce_premium=short_ce.ltp,
        short_pe_strike=short_pe.strike,
        short_pe_premium=short_pe.ltp,
        hedge_ce_strike=hedge_ce.strike,
        hedge_ce_premium=hedge_ce.ltp,
        hedge_pe_strike=hedge_pe.strike,
        hedge_pe_premium=hedge_pe.ltp,
        lots=lots,
        net_credit=round(net_credit, 2),
        net_credit_inr=net_credit_inr,
        max_loss_inr=max_loss_inr,
        max_profit_inr=net_credit_inr,
        breakeven_upper=breakeven_upper,
        breakeven_lower=breakeven_lower,
    )
