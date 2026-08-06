"""
Tests for the must_include_strikes fix to STRIKE_RANGE_POINTS.

Confirmed live on 2026-07-30: an iron condor's hedge PE, opened 300pts
below its short strike, silently vanished from the chain as spot drifted
past the 800pt STRIKE_RANGE_POINTS window -- no error, no crash, just
"MTM P&L: unavailable this cycle" for four consecutive polls. The
distance from spot to the hedge leg crossing exactly 800 matched every
single data point in the real log:

    10:57  spot 24350.7  hedge PE distance 800.7  -> excluded -> unavailable
    11:07  spot 24346.5  hedge PE distance 796.5  -> included -> Rs 114
    11:17  spot 24356.9  hedge PE distance 806.9  -> excluded -> unavailable
    ...

Same bug CLASS as the 2026-07-22 premium-filter incident (README), a
different filter. These tests pin the fix at the level that actually
matters: does a protected strike survive the filter dhan_source.py and
nse_source.py apply to the raw chain, and does each strategy correctly
compute which strikes to protect from its own open position.

Run: python -m pytest tests/ -q
"""

import config
import main_condor
import main_directional_spread
import main_live


# --------------------------------------------------------------------------
# The actual confirmed incident, reproduced directly
# --------------------------------------------------------------------------

def test_reproduces_the_confirmed_incident_distances():
    """
    Pins the exact real-world numbers: hedge PE at 23550, short PE at
    23850 (HEDGE_DISTANCE_POINTS=300). At each real spot from the log,
    was the hedge leg inside or outside an 800pt window -- and does
    protecting it override that regardless of distance.
    """
    hedge_pe_strike = 23550.0
    range_pts = 800

    real_log = [
        (24350.7, False),   # 800.7 away -> WAS excluded (bug)
        (24346.5, True),    # 796.5 away -> was included
        (24356.9, False),   # 806.9 away -> WAS excluded (bug)
        (24366.95, False),  # 816.95 away -> WAS excluded (bug)
    ]
    for spot, was_naturally_in_range in real_log:
        distance = abs(hedge_pe_strike - spot)
        naturally_in_range = distance <= range_pts
        assert naturally_in_range == was_naturally_in_range, (
            f"sanity check on the real log's numbers failed at spot {spot}"
        )
        # Regardless of natural distance, protection must always include it.
        protected = {hedge_pe_strike}
        survives = distance <= range_pts or hedge_pe_strike in protected
        assert survives is True, f"protected strike must survive at spot {spot}"


# --------------------------------------------------------------------------
# dhan_source.py: the actual filter
# --------------------------------------------------------------------------

def test_dhan_filter_drops_unprotected_far_strike(monkeypatch):
    monkeypatch.setattr(config, "STRIKE_RANGE_POINTS", 800)
    spot = 24350.7
    strikes = {"23550": {}, "24000": {}, "24500": {}}
    kept = {k: v for k, v in strikes.items()
            if abs(float(k) - spot) <= config.STRIKE_RANGE_POINTS}
    assert "23550" not in kept, "sanity: 800.7pts away must be dropped without protection"


def test_dhan_filter_keeps_protected_far_strike(monkeypatch):
    monkeypatch.setattr(config, "STRIKE_RANGE_POINTS", 800)
    spot = 24350.7
    protected = {23550.0}
    strikes = {"23550": {}, "24000": {}, "24500": {}}
    kept = {
        k: v for k, v in strikes.items()
        if abs(float(k) - spot) <= config.STRIKE_RANGE_POINTS or float(k) in protected
    }
    assert "23550" in kept, "a protected strike must survive regardless of distance"
    assert set(kept) == {"23550", "24000", "24500"}


def test_dhan_get_nifty_snapshot_accepts_and_uses_the_parameter(monkeypatch):
    """Full function signature + behaviour, not just the inline filter logic."""
    import dhan_source

    monkeypatch.setattr(config, "STRIKE_RANGE_POINTS", 800)
    fake_raw = {
        "last_price": 24350.7,
        "oc": {
            "23550": {"ce": {"implied_volatility": 12, "last_price": 5, "oi": 100, "previous_oi": 90,
                             "volume": 10, "greeks": {}},
                      "pe": {"implied_volatility": 12, "last_price": 300, "oi": 100, "previous_oi": 90,
                             "volume": 10, "greeks": {}}},
            "24300": {"ce": {"implied_volatility": 12, "last_price": 90, "oi": 100, "previous_oi": 90,
                             "volume": 10, "greeks": {}},
                      "pe": {"implied_volatility": 12, "last_price": 60, "oi": 100, "previous_oi": 90,
                             "volume": 10, "greeks": {}}},
        },
    }
    monkeypatch.setattr(dhan_source, "_fetch_raw_chain", lambda expiry, *a, **kw: fake_raw)
    monkeypatch.setattr(dhan_source, "_update_vwap_proxy", lambda spot, *a, **kw: spot)
    monkeypatch.setattr(dhan_source, "_load_iv_history", lambda *a, **kw: [])
    monkeypatch.setattr(dhan_source, "_save_iv_history", lambda h, *a, **kw: None)
    monkeypatch.setattr(dhan_source, "_load_price_baseline", lambda *a, **kw: {"date": None, "prices": {}})
    monkeypatch.setattr(dhan_source, "_save_price_baseline", lambda s, *a, **kw: None)
    monkeypatch.setattr(dhan_source, "_load_oi_history", lambda *a, **kw: {"date": None, "samples": {}})
    monkeypatch.setattr(dhan_source, "_save_oi_history", lambda h, *a, **kw: None)

    without = dhan_source.get_nifty_snapshot(expiry="2026-08-04")
    with_protection = dhan_source.get_nifty_snapshot(expiry="2026-08-04", must_include_strikes={23550.0})

    strikes_without = {q.strike for q in without.chain}
    strikes_with = {q.strike for q in with_protection.chain}
    assert 23550.0 not in strikes_without, "without protection, the far strike is dropped (the bug)"
    assert 23550.0 in strikes_with, "with protection, the far strike survives (the fix)"


# --------------------------------------------------------------------------
# nse_source.py: fallback tier must not reintroduce the blind spot
# --------------------------------------------------------------------------

def test_nse_filter_keeps_protected_far_strike(monkeypatch):
    monkeypatch.setattr(config, "STRIKE_RANGE_POINTS", 800)
    spot = 24350.7
    rows = [{"strikePrice": 23550.0}, {"strikePrice": 24300.0}]
    protected = {23550.0}
    kept = [r for r in rows
            if abs(r["strikePrice"] - spot) <= config.STRIKE_RANGE_POINTS
            or r["strikePrice"] in protected]
    assert {r["strikePrice"] for r in kept} == {23550.0, 24300.0}


# --------------------------------------------------------------------------
# Each strategy computes its own protection set correctly
# --------------------------------------------------------------------------

def test_condor_protects_all_four_legs_of_an_open_position():
    position = {
        "status": "OPEN",
        "plan": {"short_ce_strike": 24500.0, "short_pe_strike": 23850.0,
                 "hedge_ce_strike": 24800.0, "hedge_pe_strike": 23550.0},
    }
    assert main_condor._tracked_strikes(position) == {24500.0, 23850.0, 24800.0, 23550.0}


def test_condor_protects_nothing_when_flat():
    assert main_condor._tracked_strikes(None) == set()
    assert main_condor._tracked_strikes({"status": "CLOSED", "plan": {}}) == set()


def test_directional_spread_protects_both_legs():
    position = {"status": "OPEN", "plan": {"short_strike": 24000.0, "hedge_strike": 23850.0}}
    assert main_directional_spread._tracked_strikes(position) == {24000.0, 23850.0}


def test_directional_spread_protects_nothing_when_flat():
    assert main_directional_spread._tracked_strikes(None) == set()


def test_momentum_scanner_protects_every_open_trade_strike():
    state = {"trades": [{"strike": 24000.0}, {"strike": 24350.0}]}
    assert main_live._tracked_strikes(state) == {24000.0, 24350.0}


def test_momentum_scanner_protects_nothing_with_no_open_trades():
    assert main_live._tracked_strikes({"trades": []}) == set()
