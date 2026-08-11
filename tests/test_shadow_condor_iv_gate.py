"""
Tests for shadow_condor.py's optional entry-side IV-regime gate
(CondorPolicy.min_iv_rank / min_iv_percentile / vix_history).

condor_scanner.py has NO IV check today -- it sells the same way every
week regardless of volatility level. This exists to MEASURE whether an
entry filter would help, not because the live scanner has one yet.

Run: python -m pytest tests/test_shadow_condor_iv_gate.py -q
"""
from datetime import datetime, timedelta

import pytest

import config_condor as ccfg
import shadow_condor as sc
from models import MarketSnapshot, OptionQuote


def _chain(spot=24000.0):
    quotes = []
    for i in range(-60, 61):
        strike = spot + i * 50
        offset = strike - spot
        for opt in ("CE", "PE"):
            if opt == "CE":
                otm_distance, itm_bump = max(offset, 0.0), max(-offset, 0.0) * 5.0
            else:
                otm_distance, itm_bump = max(-offset, 0.0), max(offset, 0.0) * 5.0
            ltp = max(1.0, 220.0 - otm_distance * 0.16 + itm_bump)
            quotes.append(OptionQuote(
                symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=opt,
                ltp=round(ltp, 2), oi=10000, oi_change_pct=0.0, volume=100,
                iv=14.0, iv_percentile=50.0, timestamp=datetime.now(),
            ))
    return quotes


def _cycles(n=20, spot=24000.0, start="2026-06-01T09:20:00"):
    base = datetime.fromisoformat(start)
    return [(MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0,
                            chain=_chain(spot), timestamp=base + timedelta(minutes=5 * i),
                            source="dhan_historical"), [], {})
           for i in range(n)]


def _mock_day(monkeypatch, cycles, day="2026-06-01"):
    monkeypatch.setattr(sc.snapshot_recorder, "load_day", lambda d, **kw: cycles if d == day else [])
    monkeypatch.setattr(sc.snapshot_recorder, "available_days", lambda: [day])


def _vix_history_with(day: str, value: float, lookback_lo=5.0, lookback_hi=25.0, n=252):
    """A synthetic VIX history where `day` closes at `value`, with
    `n` prior days spanning [lookback_lo, lookback_hi] so a real
    rank/percentile is computable."""
    from datetime import date, timedelta as td
    d = date.fromisoformat(day)
    hist = []
    for i in range(n, 0, -1):
        frac = (n - i) / n
        v = lookback_lo + frac * (lookback_hi - lookback_lo)
        hist.append(((d - td(days=i)).isoformat(), round(v, 2)))
    hist.append((day, value))
    return hist


def test_default_policy_ignores_iv_entirely_and_opens_normally(monkeypatch):
    """No vix_history, no thresholds -- must behave exactly as before
    this feature existed."""
    _mock_day(monkeypatch, _cycles())
    condors, skips = sc.run_policy("2026-06-01")
    assert condors, f"expected a condor, got skips={skips}"
    assert skips["iv_regime_too_low"] == 0


def test_low_iv_day_blocks_entry_when_min_rank_set(monkeypatch):
    _mock_day(monkeypatch, _cycles())
    vix_hist = _vix_history_with("2026-06-01", value=6.0)  # near the bottom of its own range
    policy = sc.CondorPolicy(min_iv_rank=50.0, vix_history=vix_hist)
    condors, skips = sc.run_policy("2026-06-01", policy)
    assert condors == []
    assert skips["iv_regime_too_low"] > 0


def test_high_iv_day_allows_entry_when_min_rank_set(monkeypatch):
    _mock_day(monkeypatch, _cycles())
    vix_hist = _vix_history_with("2026-06-01", value=24.0)  # near the top of its own range
    policy = sc.CondorPolicy(min_iv_rank=50.0, vix_history=vix_hist)
    condors, skips = sc.run_policy("2026-06-01", policy)
    assert condors, f"expected a condor on a high-IV day, got skips={skips}"


def test_missing_vix_data_fails_closed_not_open(monkeypatch):
    """A day with no VIX history at all (or not enough trailing data)
    must NOT be treated as passing the filter -- an unevaluable filter
    is not evidence the regime is favourable."""
    _mock_day(monkeypatch, _cycles())
    policy = sc.CondorPolicy(min_iv_rank=10.0, vix_history=[])  # empty -- can't evaluate
    condors, skips = sc.run_policy("2026-06-01", policy)
    assert condors == []
    assert skips["iv_regime_too_low"] > 0


def test_both_thresholds_must_pass_when_both_set(monkeypatch):
    _mock_day(monkeypatch, _cycles())
    vix_hist = _vix_history_with("2026-06-01", value=15.0)  # mid-range: passes a loose bar, fails a strict one
    lenient = sc.CondorPolicy(min_iv_rank=10.0, min_iv_percentile=10.0, vix_history=vix_hist)
    strict = sc.CondorPolicy(min_iv_rank=90.0, min_iv_percentile=10.0, vix_history=vix_hist)

    condors_lenient, _ = sc.run_policy("2026-06-01", lenient)
    condors_strict, skips_strict = sc.run_policy("2026-06-01", strict)

    assert condors_lenient, "mid-range IV should clear a loose bar on both thresholds"
    assert condors_strict == [], "mid-range IV should NOT clear a 90 rank bar"
    assert skips_strict["iv_regime_too_low"] > 0
