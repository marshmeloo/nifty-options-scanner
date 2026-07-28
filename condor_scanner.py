"""
Strike selection for the iron condor strategy: pick a short CE and short
PE whose premium falls in config_condor.SHORT_PREMIUM_MIN/MAX, then find
the protective hedge strike on each side, config_condor.HEDGE_DISTANCE_POINTS
further OTM.
"""

import config_condor as ccfg


def find_short_strike(chain: list, option_type: str):
    """
    Among quotes of the given option_type whose premium falls within the
    configured band, return the highest-OI one (most liquid). Returns
    None if nothing in the chain matches -- this is a legitimate "no
    setup this week" outcome, not an error; the premium band can simply
    not line up with any strike some weeks.
    """
    candidates = [
        q for q in chain
        if q.option_type == option_type
        and ccfg.SHORT_PREMIUM_MIN <= q.ltp <= ccfg.SHORT_PREMIUM_MAX
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.oi)


def find_hedge_strike(chain: list, option_type: str, short_strike: float):
    """
    Find the protective long leg: for CE, the strike >= short_strike +
    HEDGE_DISTANCE_POINTS closest to that target (further OTM = cheaper,
    so overshooting the target distance is fine, undershooting reduces
    protection -- prefer the nearest strike AT OR BEYOND the target).
    For PE, mirror image (<= short_strike - HEDGE_DISTANCE_POINTS).
    Returns None if the chain doesn't extend that far out.
    """
    same_side = [q for q in chain if q.option_type == option_type]
    if option_type == "CE":
        target = short_strike + ccfg.HEDGE_DISTANCE_POINTS
        beyond = [q for q in same_side if q.strike >= target]
        if not beyond:
            return None
        return min(beyond, key=lambda q: q.strike)
    else:  # PE
        target = short_strike - ccfg.HEDGE_DISTANCE_POINTS
        beyond = [q for q in same_side if q.strike <= target]
        if not beyond:
            return None
        return max(beyond, key=lambda q: q.strike)


def find_condor_legs(chain: list) -> dict:
    """
    Returns {"short_ce", "short_pe", "hedge_ce", "hedge_pe"} -> OptionQuote
    or None for any leg that couldn't be found. condor_plan_generator.py
    is responsible for deciding whether a partial result (some legs
    missing) means "no trade this week."
    """
    short_ce = find_short_strike(chain, "CE")
    short_pe = find_short_strike(chain, "PE")

    hedge_ce = find_hedge_strike(chain, "CE", short_ce.strike) if short_ce else None
    hedge_pe = find_hedge_strike(chain, "PE", short_pe.strike) if short_pe else None

    return {"short_ce": short_ce, "short_pe": short_pe, "hedge_ce": hedge_ce, "hedge_pe": hedge_pe}
