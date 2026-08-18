"""
Dealer Gamma Exposure (GEX): net regime, zero-gamma level, and call/put
gamma walls -- computed from a MarketSnapshot's chain using OI and
Black-Scholes gamma (see black_scholes.py).

RESEARCH ONLY as of 2026-08-19. Built to evaluate a specific hypothesis
first raised comparing this project against neogreeks.in's dashboard:
does the gamma regime (whether dealer hedging is expected to DAMPEN or
AMPLIFY price moves) predict differential forward returns for this
project's own momentum-aligned candidates? See gamma_exposure_study.py
for that backtest. NOT wired into any live scoring, filtering, or entry
decision -- this module only computes the read; whether and how to act
on it is a separate decision, after the backtest, not before it.

THE SIGN CONVENTION IS A MODELLING ASSUMPTION, NOT A FACT
-----------------------------------------------------------
GEX from public OI data can only ever be an INFERENCE about dealer
positioning -- nobody outside the dealers themselves actually knows
their book. This module uses the convention most public/retail GEX
trackers use (the one popularised by SqueezeMetrics for US index
options):

    NetGEX = sum over strikes of
        (CallGamma * CallOI - PutGamma * PutOI) * LotSize * Spot^2 * 0.01

i.e. call open interest contributes POSITIVE gamma exposure, put open
interest contributes NEGATIVE. That rests on assuming the public is, on
net, a long-call/long-put counterparty to dealers who are short both --
a simplification, not a verified fact about who holds each side of
every contract. SIGN_CONVENTION exists as an explicit, named constant
rather than being baked into the arithmetic, for exactly this reason:
if the backtest ends up correlating better with the OPPOSITE
convention, that is a real, checkable result to report, not something
a hardcoded formula could hide.

WHY BLACK-SCHOLES GAMMA, NOT DHAN'S LIVE-REPORTED GAMMA
----------------------------------------------------------
Dhan reports gamma directly on a live chain, but historical_source.py's
reconstructed snapshots have no Greeks at all (Dhan's Expired Options
Data endpoint doesn't return them -- see that module's own docstring).
Using black_scholes.gamma() -- validated against Dhan's own live gamma
to within 1-6%, see test_black_scholes.py -- for BOTH live and
historical computation keeps the two comparable. Computing GEX one way
live and a different way historically would make the backtest answer a
different question than "would this have helped live."
"""

from collections import defaultdict
from datetime import datetime

import black_scholes as bs
import historical_source
from models import GammaExposure, MarketSnapshot

# See module docstring's sign-convention section. +1 for calls, -1 for
# puts is the widely-used public default; flip if a backtest supports
# the opposite.
SIGN_CONVENTION = {"CE": 1, "PE": -1}


def compute(snapshot: MarketSnapshot, lot_size: int, now: datetime) -> GammaExposure:
    """
    GammaExposure for one snapshot.

    `now`: deliberately no default. A LIVE caller passes datetime.now();
    a HISTORICAL caller MUST pass the snapshot's own timestamp
    (snapshot.timestamp), never the real wall clock -- computing
    time-to-expiry for a years-old historical contract against TODAY'S
    date would put every one of them long past its own expiry, silently
    zeroing every gamma value and making the whole read meaningless
    without raising any error. Making this required forces every call
    site to state which "now" it means rather than one defaulting away
    the mistake.

    `snapshot.chain` is expected to carry OI and IV for every contract,
    which every live or historical snapshot in this project already
    does -- gamma itself is recomputed here via Black-Scholes rather
    than read off quote.gamma, see module docstring.

    Returns a GammaExposure with every field present but possibly None
    (zero_gamma_level, gamma_call_wall_strike, gamma_put_wall_strike) if
    the chain is empty, has fewer than two strikes, or every contract's
    gamma comes out to zero (e.g. everything already past its own
    expiry).

    This computes the FULL read, including the zero-gamma grid search
    (see _find_zero_gamma_level -- ZERO_GAMMA_GRID_POINTS+1 gamma
    evaluations per contract). A caller that only needs net_gex/regime
    -- e.g. a backtest scoring tens of thousands of cycles, where that
    grid search dominates runtime for no benefit it actually uses --
    should call net_gex_and_regime() instead.
    """
    per_strike, net_gex, regime = _per_strike_and_regime(snapshot, lot_size, now)

    return GammaExposure(
        net_gex=round(net_gex, 2),
        regime=regime,
        zero_gamma_level=_find_zero_gamma_level(snapshot.chain, lot_size, now),
        gamma_call_wall_strike=_wall_strike(per_strike, side_index=1),
        gamma_put_wall_strike=_wall_strike(per_strike, side_index=2),
        per_strike=per_strike,
    )


def net_gex_and_regime(snapshot: MarketSnapshot, lot_size: int, now: datetime) -> tuple:
    """
    (net_gex, regime) only -- the cheap subset of compute(), skipping
    the zero-gamma grid search and wall detection entirely. Same
    net_gex/regime values compute() would return for the same inputs;
    built for callers (gamma_exposure_study.py) that need this evaluated
    across a large number of historical cycles and have no use for the
    rest of the read.
    """
    _per_strike, net_gex, regime = _per_strike_and_regime(snapshot, lot_size, now)
    return round(net_gex, 2), regime


def _per_strike_and_regime(snapshot: MarketSnapshot, lot_size: int, now: datetime) -> tuple:
    by_strike = defaultdict(lambda: {"CE": None, "PE": None})
    for q in snapshot.chain:
        by_strike[q.strike][q.option_type] = q

    per_strike = []
    for strike in sorted(by_strike.keys()):
        sides = by_strike[strike]
        call_gex = _side_gex(sides.get("CE"), snapshot.spot, lot_size, now)
        put_gex = _side_gex(sides.get("PE"), snapshot.spot, lot_size, now)
        net = call_gex * SIGN_CONVENTION["CE"] + put_gex * SIGN_CONVENTION["PE"]
        per_strike.append((strike, call_gex, put_gex, net))

    net_gex = sum(row[3] for row in per_strike)
    regime = "SHORT_GAMMA" if net_gex < 0 else "LONG_GAMMA"
    return per_strike, net_gex, regime


def _resolve_expiry_date(quote_expiry: str, snapshot_date) -> str:
    """
    A real "YYYY-MM-DD" expiry to feed black_scholes.time_to_expiry_years().

    Live and recorded snapshots carry a genuine calendar date in
    quote.expiry. historical_source.py's reconstructed contracts don't
    -- Dhan's rolling-options endpoint never returns an actual expiry
    date, only "this was the nearest weekly one," so quote.expiry there
    is the symbolic placeholder "rolling:week1" (see that module's own
    docstring). Falls back to historical_source.nominal_expiry_date()
    (the next occurrence of NIFTY's real weekly-expiry weekday, correctly
    handling the actual 2025-09-01 Thursday->Tuesday regime change)
    whenever quote_expiry isn't parseable as a real date.
    """
    try:
        datetime.strptime(quote_expiry, "%Y-%m-%d")
        return quote_expiry
    except (ValueError, TypeError):
        return historical_source.nominal_expiry_date(snapshot_date).isoformat()


def _side_gex(quote, spot: float, lot_size: int, now: datetime) -> float:
    """
    |Gamma| * OI * LotSize * Spot^2 * 0.01 for one side of one strike --
    0.0 if there's no quote, no OI, or no time/IV left to compute gamma
    from. Always non-negative: SIGN_CONVENTION applies the direction in
    compute(), not here, so the wall calculation (which cares about
    SIZE of hedging pressure, not direction) can use this directly
    without having to undo a sign first.
    """
    if quote is None or not quote.oi or not quote.iv:
        return 0.0
    expiry = _resolve_expiry_date(quote.expiry, now.date())
    t = bs.time_to_expiry_years(expiry, now)
    g = bs.gamma(spot, quote.strike, t, quote.iv / 100)
    return g * quote.oi * lot_size * spot ** 2 * 0.01


ZERO_GAMMA_GRID_POINTS = 200   # candidate spot levels searched between the chain's lowest and highest strike


def _net_gex_at_hypothetical_spot(chain: list, hypothetical_spot: float, lot_size: int, now) -> float:
    """
    Total signed GEX AS IF spot were at `hypothetical_spot` right now --
    every contract's OI is held fixed (that's real, recorded positioning
    data), but gamma is recomputed at the hypothetical spot, since gamma
    itself is a function of moneyness and changes as spot moves. This is
    what _find_zero_gamma_level walks across a grid of candidate spots;
    it is NOT the same computation as compute()'s own per_strike (which
    evaluates every contract's gamma at the ACTUAL current spot, for
    net_gex/regime/walls -- "what is dealer positioning right now").
    """
    total = 0.0
    for q in chain:
        if not q.oi or not q.iv:
            continue
        expiry = _resolve_expiry_date(q.expiry, now.date())
        t = bs.time_to_expiry_years(expiry, now)
        g = bs.gamma(hypothetical_spot, q.strike, t, q.iv / 100)
        magnitude = g * q.oi * lot_size * hypothetical_spot ** 2 * 0.01
        total += magnitude * SIGN_CONVENTION.get(q.option_type, 0)
    return total


def _find_zero_gamma_level(chain: list, lot_size: int, now):
    """
    The hypothetical SPOT PRICE where aggregate dealer gamma exposure
    flips sign -- the standard GEX definition (see module docstring):
    "how far would spot have to move for the regime itself to flip,"
    not "where across today's strike ladder does concentration change."
    Recomputes gamma at each of ZERO_GAMMA_GRID_POINTS candidate spot
    levels spanning the chain's own lowest to highest strike (OI held
    fixed throughout -- it's real positioning data, not a function of
    the hypothetical), and linearly interpolates between the two grid
    points that bracket a sign change.

    None if the chain has fewer than two strikes, or the sign never
    flips across the whole searched range (e.g. an entirely one-sided
    chain) -- there is nothing to report a crossing FOR, so this
    returns None rather than guessing a value outside the range the
    chain's own data can speak to.
    """
    strikes = sorted({q.strike for q in chain})
    if len(strikes) < 2:
        return None
    lo, hi = strikes[0], strikes[-1]
    step = (hi - lo) / ZERO_GAMMA_GRID_POINTS
    if step <= 0:
        return None

    prev_level, prev_net = None, None
    for i in range(ZERO_GAMMA_GRID_POINTS + 1):
        level = lo + i * step
        net = _net_gex_at_hypothetical_spot(chain, level, lot_size, now)
        if prev_net is not None and (prev_net < 0) != (net < 0):
            span = net - prev_net
            if span == 0:
                return round(level, 1)
            frac = -prev_net / span
            return round(prev_level + frac * (level - prev_level), 1)
        prev_level, prev_net = level, net
    return None


def _wall_strike(per_strike: list, side_index: int):
    """Strike with the largest per-side gamma exposure magnitude
    (side_index 1 = call, 2 = put) -- None if every value is zero."""
    candidates = [(row[0], row[side_index]) for row in per_strike if row[side_index] > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[1])[0]
