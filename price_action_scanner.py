"""
Turns a price_structure.StructureSignal into a Setup: which single option
to buy (CE on a bullish signal, PE on a bearish one) and at what strike.

Deliberately much thinner than scanner.py -- there is no multi-factor
score here, no OI/IV/PCR/RSI/ROC. The entire "why" for this trade is the
structural signal itself; this module's only job is candidate/strike
selection (premium band + liquidity), the same responsibility
scanner.py's premium filter and _is_liquid_enough() have there.
"""

import config_price_action as pcfg
from models import Setup
from price_structure import StructureSignal


def _is_liquid_enough(q) -> bool:
    """Same reasoning as scanner.py's own liquidity screen: a quoted
    premium is not a tradeable contract without volume/OI/spread behind
    it. No config knob is added here beyond the premium band -- this
    strategy has no OI-buildup or IV read to gate on, so only spread
    (when a book is available) is checked."""
    max_spread = getattr(pcfg, "MAX_SPREAD_PCT", None)
    if max_spread is not None:
        spread = getattr(q, "spread_pct", None)
        if spread is not None and spread > max_spread:
            return False
    return True


def _find_candidate(snapshot, option_type: str):
    """
    The nearest-the-money quote of `option_type` that sits inside the
    configured premium band and passes the liquidity screen. Nearest-
    the-money (rather than "first in the chain") keeps the delta close
    to the structural signal's own risk assumptions -- a deep OTM strike
    at the same premium would have a very different delta and stop
    translation in the plan generator.
    """
    best = None
    for q in snapshot.chain:
        if q.option_type != option_type:
            continue
        if q.ltp < pcfg.PREMIUM_MIN or q.ltp > pcfg.PREMIUM_MAX:
            continue
        if not _is_liquid_enough(q):
            continue
        distance = abs(q.strike - snapshot.spot)
        if best is None or distance < best[0]:
            best = (distance, q)
    return best[1] if best else None


def scan(snapshot, signal: StructureSignal) -> list:
    """
    Returns a list of at most one Setup (kept as a list for the same
    reason scanner.scan() returns one: it slots into the existing
    plan_generator/risk_checker/decision pipeline unchanged). Empty list
    when there is no signal, or no contract in the premium band survives
    the liquidity screen.
    """
    if signal is None:
        return []

    option_type = "CE" if signal.direction == "bullish" else "PE"
    quote = _find_candidate(snapshot, option_type)
    if quote is None:
        return []

    reasons = list(signal.reasons)
    reasons.append(
        f"Structural {signal.direction} break: entry ref {signal.entry_reference:.1f}, "
        f"stop ref {signal.stop_reference:.1f} (index points)"
    )

    return [
        Setup(
            symbol=quote.symbol,
            strike=quote.strike,
            option_type=quote.option_type,
            expiry=quote.expiry,
            reasons=reasons,
            score=1.0,  # binary signal, not a graded score -- see this module's docstring
        )
    ]
