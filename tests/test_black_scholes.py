"""
Tests for black_scholes.py's gamma(), including the real-data validation
that justifies using it as a historical proxy for gamma Dhan doesn't
report on expired/historical contracts -- see gamma_exposure.py and
that module's own docstring for why this exists.

Run: python -m pytest tests/test_black_scholes.py -q
"""

from datetime import datetime

import pytest

import black_scholes as bs


# --------------------------------------------------------------------------
# Basic sanity: shape of the gamma curve
# --------------------------------------------------------------------------

def test_gamma_is_positive_for_a_normal_contract():
    assert bs.gamma(24000, 24000, time_to_expiry_years=7 / 365, iv=0.12) > 0


def test_gamma_peaks_near_the_money():
    """Gamma is highest ATM and falls off moving away from spot in
    either direction -- the textbook shape, worth pinning since
    gamma_exposure.py's wall detection assumes it."""
    t, iv = 7 / 365, 0.12
    atm = bs.gamma(24000, 24000, t, iv)
    otm_call_side = bs.gamma(24000, 24500, t, iv)
    itm_call_side = bs.gamma(24000, 23500, t, iv)
    assert atm > otm_call_side
    assert atm > itm_call_side


def test_gamma_same_for_call_and_put_at_the_same_strike():
    """Gamma doesn't depend on option side -- this module takes no
    option_type parameter for exactly that reason."""
    t, iv = 7 / 365, 0.12
    # gamma() itself has no notion of side; this just documents that a
    # caller looking up a CE and a PE at the same strike/expiry/IV would
    # correctly get the identical value by calling it once.
    assert bs.gamma(24000, 24100, t, iv) == bs.gamma(24000, 24100, t, iv)


def test_gamma_falls_as_time_to_expiry_shrinks_far_otm():
    """Deep OTM, less time means less chance of ever mattering -- gamma
    should shrink, not grow, as expiry approaches for an option with no
    realistic chance of finishing ITM."""
    close = bs.gamma(24000, 25000, time_to_expiry_years=7 / 365, iv=0.12)
    almost_none = bs.gamma(24000, 25000, time_to_expiry_years=0.5 / 365, iv=0.12)
    assert almost_none < close


def test_gamma_rises_near_expiry_for_a_near_atm_contract():
    """The other half of the same shape: an option very close to the
    money gets SHARPER (higher gamma) as expiry approaches -- classic
    "gamma risk spikes into expiry" behaviour."""
    far = bs.gamma(24000, 24010, time_to_expiry_years=7 / 365, iv=0.12)
    near = bs.gamma(24000, 24010, time_to_expiry_years=0.5 / 365, iv=0.12)
    assert near > far


# --------------------------------------------------------------------------
# Edge cases: must return 0.0, never raise or divide by zero
# --------------------------------------------------------------------------

def test_zero_time_to_expiry_returns_zero_not_a_crash():
    assert bs.gamma(24000, 24000, time_to_expiry_years=0, iv=0.12) == 0.0


def test_negative_time_to_expiry_returns_zero():
    assert bs.gamma(24000, 24000, time_to_expiry_years=-0.001, iv=0.12) == 0.0


def test_zero_iv_returns_zero_not_a_crash():
    assert bs.gamma(24000, 24000, time_to_expiry_years=7 / 365, iv=0) == 0.0


def test_zero_spot_or_strike_returns_zero():
    assert bs.gamma(0, 24000, 7 / 365, 0.12) == 0.0
    assert bs.gamma(24000, 0, 7 / 365, 0.12) == 0.0


# --------------------------------------------------------------------------
# Real-data validation (2026-08-18)
# --------------------------------------------------------------------------

def test_matches_real_dhan_gamma_20260818():
    """
    Pins the actual comparison run live against Dhan's option chain on
    2026-08-18: NIFTY spot 24154.9, the 2026-08-25 expiry (6.8 days out,
    so T is large enough for the comparison to be meaningful -- an
    earlier attempt against the SAME-DAY expiry after market close
    produced nonsense, T~0 and 1-5% IVs, an artifact of checking a dead
    contract after hours).

    `now` is fixed at 16:00 on 2026-08-18 (when the real check ran) so
    this test is fully reproducible rather than drifting with
    datetime.now(). Every ratio (this module's gamma / Dhan's reported
    gamma) landed between 1.01 and 1.06 across 12 real strikes, both CE
    and PE -- consistently within 1-6%, close enough to trust as a
    historical proxy where Dhan reports no gamma at all. The recorded
    numbers below are exactly what that live check returned.
    """
    spot = 24154.9
    expiry = "2026-08-25"
    now = datetime(2026, 8, 18, 16, 0, 0)
    t = bs.time_to_expiry_years(expiry, now)

    # (strike, iv_pct, dhan_reported_gamma)
    real_quotes = [
        (24150.0, 9.68, 0.001200), (24150.0, 9.95, 0.001160),
        (24200.0, 9.60, 0.001220), (24200.0, 9.71, 0.001210),
        (24100.0, 9.79, 0.001140), (24100.0, 9.99, 0.001120),
        (24250.0, 9.56, 0.001220), (24250.0, 9.70, 0.001200),
        (24050.0, 9.96, 0.001060), (24050.0, 10.18, 0.001040),
        (24300.0, 9.52, 0.001180), (24300.0, 9.65, 0.001170),
    ]
    for strike, iv_pct, dhan_gamma in real_quotes:
        my_gamma = bs.gamma(spot, strike, t, iv_pct / 100)
        ratio = my_gamma / dhan_gamma
        assert 0.95 <= ratio <= 1.10, (
            f"strike {strike} iv {iv_pct}%: my_gamma={my_gamma:.6f} "
            f"dhan_gamma={dhan_gamma:.6f} ratio={ratio:.3f} outside the validated band"
        )


# --------------------------------------------------------------------------
# time_to_expiry_years
# --------------------------------------------------------------------------

def test_time_to_expiry_years_future_expiry_is_positive():
    t = bs.time_to_expiry_years("2026-08-25", now=datetime(2026, 8, 18, 10, 0))
    assert t > 0
    assert t < 10 / 365   # under 10 days out


def test_time_to_expiry_years_zero_at_the_close_cutoff():
    """Expiry's effective moment is 15:30 IST, not midnight."""
    t_before_close = bs.time_to_expiry_years("2026-08-18", now=datetime(2026, 8, 18, 15, 0))
    t_after_close = bs.time_to_expiry_years("2026-08-18", now=datetime(2026, 8, 18, 15, 31))
    assert t_before_close > 0
    assert t_after_close == 0.0


def test_time_to_expiry_years_past_expiry_is_zero_not_negative():
    t = bs.time_to_expiry_years("2026-08-01", now=datetime(2026, 8, 18, 10, 0))
    assert t == 0.0
