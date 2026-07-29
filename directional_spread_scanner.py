"""
Direction + strike selection for the directional credit-spread strategy.

Unlike condor_scanner.py (which always looks for both a CE and a PE
short leg), this picks ONE side based on scanner.compute_market_bias():
bullish -> sell a PUT spread (profits if spot stays above the short
strike), bearish -> sell a CALL spread (profits if spot stays below it).
A neutral/weak bias means no directional edge to sell against, so no
side is selected at all -- that's a legitimate "no trade this cycle"
outcome, not an error.
"""

import config_directional_spread as dcfg


def select_direction(bias_label: str, bias_score: float):
    """
    Returns "PE" (sell a put spread, i.e. bullish bet), "CE" (sell a call
    spread, i.e. bearish bet), or None if the bias isn't strong enough or
    is neutral/range. The option_type returned is the side to SELL, which
    is deliberately the opposite letter from the "obvious" association --
    a bullish bet is expressed by selling PUTS (you profit if price
    doesn't fall through your short strike), not by buying calls.
    """
    if bias_label is None or bias_score is None:
        return None
    if abs(bias_score) < dcfg.BIAS_STRONG_THRESHOLD:
        return None
    if bias_label == "bullish":
        return "PE"
    if bias_label == "bearish":
        return "CE"
    return None


def find_short_strike(chain: list, option_type: str):
    """
    Same approach as condor_scanner.find_short_strike: among quotes of
    the given type whose premium falls in the configured band, prefer
    the highest-OI (most liquid) match over the one closest to spot.
    Returns None if nothing in the chain matches this cycle.
    """
    candidates = [
        q for q in chain
        if q.option_type == option_type
        and dcfg.SHORT_PREMIUM_MIN <= q.ltp <= dcfg.SHORT_PREMIUM_MAX
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.oi)


def find_hedge_strike(chain: list, option_type: str, short_strike: float):
    """
    Protective long leg, HEDGE_DISTANCE_POINTS further OTM than the short
    strike. For a put spread (option_type="PE") that means a LOWER
    strike (further from spot on the downside); for a call spread
    ("CE") a HIGHER strike. Mirrors condor_scanner.find_hedge_strike.
    """
    same_side = [q for q in chain if q.option_type == option_type]
    if option_type == "CE":
        target = short_strike + dcfg.HEDGE_DISTANCE_POINTS
        beyond = [q for q in same_side if q.strike >= target]
        if not beyond:
            return None
        return min(beyond, key=lambda q: q.strike)
    else:  # PE
        target = short_strike - dcfg.HEDGE_DISTANCE_POINTS
        beyond = [q for q in same_side if q.strike <= target]
        if not beyond:
            return None
        return max(beyond, key=lambda q: q.strike)


def find_directional_spread_legs(chain: list, bias_label: str, bias_score: float) -> dict:
    """
    Returns {"direction": "PE"|"CE"|None, "short": OptionQuote|None,
    "hedge": OptionQuote|None}. directional_spread_plan_generator.py
    decides whether a partial or absent result means "no trade this
    cycle" -- this function only ever reports what it found.
    """
    direction = select_direction(bias_label, bias_score)
    if direction is None:
        return {"direction": None, "short": None, "hedge": None}

    short = find_short_strike(chain, direction)
    hedge = find_hedge_strike(chain, direction, short.strike) if short else None
    return {"direction": direction, "short": short, "hedge": hedge}
