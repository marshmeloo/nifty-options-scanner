"""
Tests for shadow_long_strangle.py.

Run: python -m pytest tests/test_shadow_long_strangle.py -q
"""
from datetime import datetime

import pytest

import config_condor as ccfg
import shadow_long_strangle as sls
from models import Candle, MarketSnapshot, OptionQuote


def _quote(strike, option_type, ltp):
    return OptionQuote(symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=option_type,
                       ltp=ltp, oi=1000, oi_change_pct=0.0, volume=500, iv=14.0, iv_percentile=50.0)


def _snapshot(ts, chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain,
                          timestamp=ts, source="dhan_historical")


def test_mtm_pnl_is_debit_not_credit_shaped():
    """A long strangle PROFITS when the combined premium is now WORTH
    MORE than what was paid -- the opposite sign convention from the
    condor's credit position."""
    strangle = sls.ShadowStrangle(ce_strike=24000, pe_strike=23800, opened_at="2026-06-01T09:20:00",
                                  net_debit=20.0, net_debit_inr=20.0 * ccfg.NIFTY_LOT_SIZE,
                                  nominal_expiry="2026-06-04")
    # value doubled -> profit
    pnl = sls._mtm_pnl_inr(strangle, {"ce": 25.0, "pe": 15.0})
    assert pnl == pytest.approx((40.0 - 20.0) * ccfg.NIFTY_LOT_SIZE)
    # value collapsed -> loss, capped at what was paid (can't lose more than the debit)
    pnl2 = sls._mtm_pnl_inr(strangle, {"ce": 0.5, "pe": 0.5})
    assert pnl2 == pytest.approx((1.0 - 20.0) * ccfg.NIFTY_LOT_SIZE)


def test_mtm_pnl_none_when_a_leg_price_missing():
    strangle = sls.ShadowStrangle(ce_strike=24000, pe_strike=23800, opened_at="2026-06-01T09:20:00",
                                  net_debit=20.0, net_debit_inr=1300.0, nominal_expiry="2026-06-04")
    assert sls._mtm_pnl_inr(strangle, {"ce": None, "pe": 15.0}) is None


def test_settle_at_expiry_falls_back_to_intrinsic_value():
    strangle = sls.ShadowStrangle(ce_strike=24000, pe_strike=23800, opened_at="2026-06-01T09:20:00",
                                  net_debit=20.0, net_debit_inr=20.0 * ccfg.NIFTY_LOT_SIZE,
                                  nominal_expiry="2026-06-04")
    # neither strike survives to expiry in the chain -> intrinsic value used
    snap = _snapshot(datetime(2026, 6, 4, 15, 30), [], spot=24150.0)
    out = sls._settle_at_expiry(snap, strangle, peak=0.0, trough=0.0, held=5)
    # CE intrinsic = 150, PE intrinsic = 0 -> combined value 150 vs debit 20
    assert out.pnl_inr == pytest.approx((150.0 - 20.0) * ccfg.NIFTY_LOT_SIZE)
    assert out.exit_reason == "expiry_settlement"


def test_run_all_never_places_a_real_order_and_returns_a_list(monkeypatch):
    # days=[] is falsy in Python -- run_all's "days or available_days()"
    # would silently load the WHOLE real dataset instead of nothing if
    # this weren't guarded against (found live: the first version of
    # this test actually did that and took ~2 minutes). Pass a real,
    # non-empty day list with no cached data instead.
    monkeypatch.setattr(sls, "_load_cached", lambda cache, day: [])
    trades, skips = sls.run_all(days=["2026-01-01"])
    assert trades == []
    assert skips == {"no_candidate": 0}
