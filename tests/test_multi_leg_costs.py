"""
Tests for spread_cost_study.py's band-aware spread lookup and
multi-leg cost model -- added for the iron condor, whose four legs sit
in very different premium bands (shorts ~Rs 25, hedges under Rs 10)
and therefore face very different real spreads.

Run: python -m pytest tests/test_multi_leg_costs.py -q
"""
import pytest

import spread_cost_study as scs


def test_deep_otm_leg_gets_far_worse_spread_than_near_atm_leg():
    """The whole reason this exists: a Rs 6 hedge does NOT trade at a
    Rs 25 short's spread. Measured, not assumed -- see the module's
    MEASURED_SPREAD_BY_BAND docstring."""
    cheap = scs.spread_pct_for_premium(6.0, "median")
    short = scs.spread_pct_for_premium(25.0, "median")
    assert cheap > short * 2          # 0.823% vs 0.301%
    assert cheap == pytest.approx(0.823)
    assert short == pytest.approx(0.301)


def test_spread_pct_bands_are_continuous_and_cover_extremes():
    for premium in (0.5, 19.99, 20.0, 49.99, 50.0, 99.9, 100.0, 199.9, 200.0, 5000.0):
        assert scs.spread_pct_for_premium(premium, "p90") > 0


def test_p90_is_never_better_than_median_in_any_band():
    for (lo, hi) in scs.MEASURED_SPREAD_BY_BAND:
        probe = lo + 0.5 if hi > lo + 1 else lo
        assert (scs.spread_pct_for_premium(probe, "p90")
                >= scs.spread_pct_for_premium(probe, "median"))


def test_multi_leg_costs_charges_each_leg_at_its_own_band():
    """A condor's cheap hedges must cost proportionally more to cross
    than a naive flat-rate model would say."""
    legs = [27.0, 24.0, 6.0, 8.0]     # real live condor shape
    banded = scs.multi_leg_costs(legs, "median", lots=1, lot_size=65,
                                 expiry_settled=True, sold_legs=[True, True, False, False])
    # Same legs costed as if all sat in the shorts' band:
    flat_pct = scs.spread_pct_for_premium(25.0, "median") / 100.0
    flat_spread = sum(p * flat_pct * 65 for p in legs)
    assert banded["spread"] > flat_spread


def test_expiry_settled_costs_less_than_traded_out():
    legs = [27.0, 24.0, 6.0, 8.0]
    settled = scs.multi_leg_costs(legs, "median", expiry_settled=True,
                                  sold_legs=[True, True, False, False])
    traded = scs.multi_leg_costs(legs, "median", expiry_settled=False,
                                 sold_legs=[True, True, False, False])
    assert settled["total"] < traded["total"]
    assert settled["spread"] == pytest.approx(traded["spread"] / 2)


def test_sold_legs_flag_changes_stt_and_therefore_taxes():
    """STT applies on the sell side only -- a structure that SELLS its
    expensive legs is taxed differently from one that buys them."""
    legs = [27.0, 24.0, 6.0, 8.0]
    sell_expensive = scs.multi_leg_costs(legs, "median", expiry_settled=True,
                                         sold_legs=[True, True, False, False])
    sell_cheap = scs.multi_leg_costs(legs, "median", expiry_settled=True,
                                     sold_legs=[False, False, True, True])
    assert sell_expensive["taxes"] != sell_cheap["taxes"]


def test_total_is_taxes_plus_spread():
    r = scs.multi_leg_costs([27.0, 24.0], "p90", expiry_settled=True, sold_legs=[True, False])
    assert r["total"] == pytest.approx(r["taxes"] + r["spread"])
