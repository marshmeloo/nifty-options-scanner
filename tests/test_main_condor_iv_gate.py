"""
Tests for main_condor.py's live IV-rank entry gate (config_condor.py's
MIN_IV_RANK_TO_OPEN, added 2026-08-12 after the full PT/IV-rank sweep --
see BACKLOG.md). Verifies the gate actually stops condor_scanner from
being reached when IV rank is too low, doesn't crash on a VIX fetch
failure, and gets out of the way entirely when disabled.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_condor as ccfg
import main_condor as mc
import condor_scanner
from models import MarketSnapshot


def _snapshot():
    return MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0, chain=[], source="test")


@pytest.fixture(autouse=True)
def _stub_market_data(monkeypatch):
    """Every test needs a flat position, a valid far-enough expiry, and a snapshot -- only the IV gate varies."""
    far_expiry = (datetime.now().date() + timedelta(days=10)).isoformat()
    monkeypatch.setattr(mc, "get_expiry_list", lambda: [far_expiry])
    monkeypatch.setattr(mc, "get_nifty_snapshot", lambda **kw: _snapshot())
    monkeypatch.setattr(ccfg, "MIN_DAYS_TO_EXPIRY_TO_OPEN", 1)


def _never_called(*a, **kw):
    raise AssertionError("condor_scanner.find_condor_legs should not have been reached")


def test_low_iv_rank_blocks_before_scanning_for_legs(monkeypatch):
    monkeypatch.setattr(ccfg, "MIN_IV_RANK_TO_OPEN", 15.0)
    monkeypatch.setattr(mc.ivs, "load_or_refresh_history", lambda: [("2020-01-01", 12.0)] * 252)
    monkeypatch.setattr(mc.ivs, "fetch_live_vix", lambda: 11.0)
    monkeypatch.setattr(mc.ivs, "live_iv_rank_and_percentile",
                         lambda hist, vix: {"vix": vix, "iv_rank": 8.0, "iv_percentile": 10.0})
    monkeypatch.setattr(condor_scanner, "find_condor_legs", _never_called)

    mc.run_once({"position": None})  # must not raise -- proves find_condor_legs was never reached


def test_high_iv_rank_clears_the_gate(monkeypatch):
    monkeypatch.setattr(ccfg, "MIN_IV_RANK_TO_OPEN", 15.0)
    monkeypatch.setattr(mc.ivs, "load_or_refresh_history", lambda: [("2020-01-01", 12.0)] * 252)
    monkeypatch.setattr(mc.ivs, "fetch_live_vix", lambda: 20.0)
    monkeypatch.setattr(mc.ivs, "live_iv_rank_and_percentile",
                         lambda hist, vix: {"vix": vix, "iv_rank": 40.0, "iv_percentile": 55.0})

    reached = {"called": False}

    def _mark_reached(chain):
        reached["called"] = True
        # No complete condor -- fine, we only care whether the gate let us in.
        return {"short_ce": None, "short_pe": None, "hedge_ce": None, "hedge_pe": None}

    monkeypatch.setattr(condor_scanner, "find_condor_legs", _mark_reached)
    mc.run_once({"position": None})
    assert reached["called"] is True


def test_vix_fetch_failure_fails_closed_not_crash(monkeypatch):
    monkeypatch.setattr(ccfg, "MIN_IV_RANK_TO_OPEN", 15.0)
    monkeypatch.setattr(mc.ivs, "load_or_refresh_history", lambda: (_ for _ in ()).throw(RuntimeError("network down")))
    monkeypatch.setattr(condor_scanner, "find_condor_legs", _never_called)

    mc.run_once({"position": None})  # must not raise, and must not reach the scanner


def test_insufficient_history_fails_closed(monkeypatch):
    monkeypatch.setattr(ccfg, "MIN_IV_RANK_TO_OPEN", 15.0)
    monkeypatch.setattr(mc.ivs, "load_or_refresh_history", lambda: [])
    monkeypatch.setattr(mc.ivs, "fetch_live_vix", lambda: 20.0)
    monkeypatch.setattr(mc.ivs, "live_iv_rank_and_percentile",
                         lambda hist, vix: {"vix": vix, "iv_rank": None, "iv_percentile": None})
    monkeypatch.setattr(condor_scanner, "find_condor_legs", _never_called)

    mc.run_once({"position": None})


def test_gate_disabled_skips_iv_check_entirely(monkeypatch):
    monkeypatch.setattr(ccfg, "MIN_IV_RANK_TO_OPEN", None)

    def _boom(*a, **kw):
        raise AssertionError("ivs functions should not be touched when the gate is disabled")

    monkeypatch.setattr(mc.ivs, "load_or_refresh_history", _boom)
    monkeypatch.setattr(mc.ivs, "fetch_live_vix", _boom)

    reached = {"called": False}
    empty_legs = {"short_ce": None, "short_pe": None, "hedge_ce": None, "hedge_pe": None}
    monkeypatch.setattr(condor_scanner, "find_condor_legs",
                         lambda chain: reached.__setitem__("called", True) or empty_legs)
    mc.run_once({"position": None})
    assert reached["called"] is True
