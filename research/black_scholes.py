"""
Black-Scholes gamma, computed from spot/strike/time-to-expiry/IV.

WHY THIS EXISTS
---------------
Gamma Exposure (GEX) analysis (see gamma_exposure.py) needs per-contract
gamma. Dhan's live option chain reports it directly (side["greeks"]["gamma"]),
but historical_source.py's reconstructed snapshots don't -- Dhan's Expired
Options Data endpoint returns OHLC/OI/IV only, no Greeks (see that module's
own docstring). Backtesting GEX over years of history therefore needs a
way to compute gamma ourselves from what IS available historically: spot,
strike, time to expiry, and IV -- all of which historical_source.py
already reconstructs.

VALIDATED, NOT ASSUMED: compared this module's output against Dhan's own
live-reported gamma on 2026-08-18, 12 real strikes (both CE and PE) on
the following week's expiry (6.8 days out, so T was large enough for the
comparison to mean anything -- an earlier attempt against the SAME-DAY
expiry after market close came back nonsensical, T~0 and degenerate IVs
around 1-5%, an artifact of checking a dead contract after hours, not a
flaw in the formula). Match was consistently within 1-5% across every
strike checked -- see test_black_scholes.py's
test_matches_real_dhan_gamma_20260818 for the exact recorded numbers.
Close enough to trust as a historical proxy; not claimed to be exact
(Dhan's own model may account for dividend yield, a different day-count
convention, or American- vs European-style pricing nuances this doesn't).

r (risk-free rate) is a fixed assumption rather than a live input. Gamma
is only weakly sensitive to r for the short-dated (mostly <=1 week)
weekly options this project trades, so a reasonable constant costs
little accuracy while avoiding a dependency on a rate feed this project
doesn't otherwise have.
"""

import math
from datetime import datetime

RISK_FREE_RATE = 0.065   # a standard approximate INR short-term rate; see module docstring


def gamma(spot: float, strike: float, time_to_expiry_years: float, iv: float,
          r: float = RISK_FREE_RATE) -> float:
    """
    Black-Scholes gamma -- identical for calls and puts at the same
    strike/expiry/IV, since gamma doesn't depend on option side.

    `iv` as a fraction (0.12 for 12%), not a percentage -- callers
    reading Dhan's implied_volatility field (already a percentage, e.g.
    12.34) must divide by 100 first.

    Returns 0.0 for a contract with no time left or no volatility
    (deep past expiry, or a placeholder/zero IV) rather than raising --
    these are normal inputs from a live chain near expiry or a strike
    with no quotes yet, not errors a caller should have to guard against
    individually.
    """
    if time_to_expiry_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot / strike) + (r + iv ** 2 / 2) * time_to_expiry_years
    ) / (iv * sqrt_t)
    phi_d1 = math.exp(-d1 ** 2 / 2) / math.sqrt(2 * math.pi)
    return phi_d1 / (spot * iv * sqrt_t)


def delta(spot: float, strike: float, time_to_expiry_years: float, iv: float,
          option_type: str, r: float = RISK_FREE_RATE) -> float:
    """
    Black-Scholes delta. N(d1) for a call, N(d1) - 1 for a put (so a put
    is negative), sharing d1 with gamma() above.

    `iv` as a fraction (0.12 for 12%), same convention as gamma().

    WHY THIS EXISTS, separately from gamma(): plan_generator._stop_distance
    sizes the stop as ATR x |delta| x STOP_ATR_MULTIPLE, and falls back to
    a FLAT config.DEFAULT_STOP_LOSS_PCT when a quote has no delta.
    Reconstructed history has no Greeks, so every historical backtest was
    silently taking that fallback -- measured 2026-08-28 over 1,085
    reconstructed trades: stop = 30.0% of premium on EVERY one of them
    (median, mean, and every individual trade), against live's real
    ATR x delta result of 15-24% (usually the 15% MIN_STOP_PCT floor).
    The backtest was giving every trade ~2x the stop room live gives it,
    which makes R a different unit on each side and every R-multiple
    incomparable. See BACKLOG.md.

    Returns 0.0 on the same degenerate inputs gamma() returns 0.0 for, so
    a caller that treats "no delta" as "fall back to flat" keeps behaving
    exactly as it did rather than being handed a misleading 0.5.
    """
    if time_to_expiry_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (
        math.log(spot / strike) + (r + iv ** 2 / 2) * time_to_expiry_years
    ) / (iv * sqrt_t)
    n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2)))
    return n_d1 if str(option_type).upper() == "CE" else n_d1 - 1.0


def time_to_expiry_years(expiry: str, now: datetime = None) -> float:
    """
    Years between `now` and `expiry`'s effective close, for feeding
    into gamma()/other BS calculations.

    Expiry's effective moment is 15:30 IST (the exchange's nominal
    close), not midnight -- an option is economically dead at the close
    of its expiry day, not at the start of it. `expiry` is a
    "YYYY-MM-DD" string (matching every expiry value already used
    throughout this project, e.g. MarketSnapshot.chain's OptionQuote.expiry).

    Returns 0.0 (not negative) once expiry has passed, matching
    gamma()'s own "no time left" handling.
    """
    now = now or datetime.now()
    expiry_close = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30)
    seconds_left = (expiry_close - now).total_seconds()
    if seconds_left <= 0:
        return 0.0
    return seconds_left / (365 * 24 * 3600)
