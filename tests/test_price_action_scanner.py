"""
Tests for price_action_scanner.py and price_action_plan_generator.py --
turning a price_structure.StructureSignal into a Setup and then a
TradePlan for a single-leg CE/PE buy.

Run: python -m pytest tests/ -q
"""

import pytest

import config_price_action as pcfg
import price_action_plan_generator as pag
import price_action_scanner as pas
from models import MarketSnapshot, OptionQuote
from price_structure import StructureSignal


def _quote(strike, option_type, ltp, delta=-0.4, bid=None, ask=None):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-07-28", strike=strike, option_type=option_type,
        ltp=ltp, oi=1000, oi_change_pct=5.0, volume=500, iv=14.0,
        iv_percentile=40.0, delta=delta, bid=bid, ask=ask,
    )


def _snapshot(chain, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0, chain=chain)


def _signal(direction="bullish", entry_ref=24050.0, stop_ref=24000.0):
    zone = type("Zone", (), {"kind": "support" if direction == "bullish" else "resistance",
                              "low": 23990.0, "high": 24010.0, "note": "test zone"})()
    return StructureSignal(
        direction=direction, entry_reference=entry_ref, stop_reference=stop_ref,
        zone=zone, reasons=["structural break test"],
    )


# --- price_action_scanner.scan -------------------------------------------

def test_no_signal_yields_no_setup():
    snap = _snapshot([_quote(24050, "CE", 80.0)])
    assert pas.scan(snap, None) == []


def test_bullish_signal_picks_a_ce_near_the_money_in_band():
    chain = [
        _quote(23900, "CE", 250.0),   # outside premium band (too rich)
        _quote(24000, "CE", 90.0),    # nearest to spot, in band
        _quote(24100, "CE", 60.0),    # further from spot, in band
        _quote(24000, "PE", 85.0),    # wrong side, must be ignored
    ]
    snap = _snapshot(chain, spot=24000.0)
    setups = pas.scan(snap, _signal("bullish"))
    assert len(setups) == 1
    setup = setups[0]
    assert setup.option_type == "CE"
    assert setup.strike == 24000
    assert any("Structural bullish break" in r for r in setup.reasons)


def test_bearish_signal_picks_a_pe():
    chain = [_quote(24000, "PE", 90.0), _quote(24000, "CE", 90.0)]
    snap = _snapshot(chain, spot=24000.0)
    setups = pas.scan(snap, _signal("bearish"))
    assert len(setups) == 1
    assert setups[0].option_type == "PE"


def test_premium_band_excludes_out_of_range_quotes():
    chain = [_quote(24000, "CE", pcfg.PREMIUM_MAX + 50)]
    snap = _snapshot(chain)
    assert pas.scan(snap, _signal("bullish")) == []


def test_wide_spread_excludes_a_quote(monkeypatch):
    monkeypatch.setattr(pcfg, "MAX_SPREAD_PCT", 2.0, raising=False)
    chain = [_quote(24000, "CE", 90.0, bid=85.0, ask=95.0)]  # ~11% spread
    snap = _snapshot(chain)
    assert pas.scan(snap, _signal("bullish")) == []


# --- price_action_plan_generator.build_plan -------------------------------

def test_build_plan_converts_index_risk_to_premium_via_delta():
    quote = _quote(24000, "CE", 90.0, delta=0.4)
    snap = _snapshot([quote])
    setup = pas.scan(snap, _signal("bullish", entry_ref=24050.0, stop_ref=24000.0))[0]
    plan = pag.build_plan(snap, setup, _signal("bullish", entry_ref=24050.0, stop_ref=24000.0))

    # 50 index pts x delta 0.4 = 20 premium pts, inside the MIN/MAX_STOP_PCT band
    assert plan.entry == 90.0
    assert plan.stop == pytest.approx(70.0)
    assert "delta" in plan.stop_basis
    assert plan.target == pytest.approx(90.0 + (90.0 - 70.0) * pcfg.TARGET_RR)


def test_build_plan_falls_back_without_delta():
    quote = _quote(24000, "CE", 90.0, delta=None)
    snap = _snapshot([quote])
    setup = pas.scan(snap, _signal("bullish"))[0]
    plan = pag.build_plan(snap, setup, _signal("bullish"))
    assert plan.stop == pytest.approx(90.0 * (1 - pcfg.MIN_STOP_PCT / 100))
    assert "no delta" in plan.stop_basis


def test_build_plan_clamps_extreme_stop_into_configured_band():
    # Huge index risk (500 pts) x delta 0.9 would blow well past MAX_STOP_PCT.
    quote = _quote(24000, "CE", 90.0, delta=0.9)
    snap = _snapshot([quote])
    setup = pas.scan(snap, _signal("bullish", entry_ref=24500.0, stop_ref=24000.0))[0]
    plan = pag.build_plan(snap, setup, _signal("bullish", entry_ref=24500.0, stop_ref=24000.0))
    max_risk = 90.0 * (pcfg.MAX_STOP_PCT / 100)
    assert (90.0 - plan.stop) == pytest.approx(max_risk)
    assert "clamped" in plan.stop_basis


def test_build_plan_uses_ask_when_book_available():
    quote = _quote(24000, "CE", 90.0, bid=88.0, ask=92.0)
    snap = _snapshot([quote])
    setup = pas.scan(snap, _signal("bullish"))[0]
    plan = pag.build_plan(snap, setup, _signal("bullish"))
    assert plan.entry == 92.0
    assert "ask" in plan.entry_basis


def test_build_plan_raises_when_quote_missing():
    snap = _snapshot([_quote(24000, "CE", 90.0)])
    setup = pas.scan(snap, _signal("bullish"))[0]
    setup.strike = 99999  # no longer matches anything in the chain
    with pytest.raises(ValueError):
        pag.build_plan(snap, setup, _signal("bullish"))
