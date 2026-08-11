"""
Tests for shadow_condor.py.

Two failure classes drove this design and are pinned directly here:

1. The rolling-series identity hazard (see shadow_directional_spread's
   module docstring) applies identically to condor legs, so the same
   expiry-week bounding is reused rather than reimplemented.
2. The cross-day one_at_a_time bug found while building the directional
   spread's multi-day walk -- open_until as a fresh local variable per
   day silently stops being enforced once positions span multiple days.
   Caught there via a real run (29/137 overlaps); pinned here directly
   via run_all() before it could ever produce a real number.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_condor as ccfg
import shadow_condor as sc
from models import MarketSnapshot, OptionQuote


def _chain(spot=24000.0):
    """
    A synthetic chain wide enough that both the SHORT_PREMIUM band and a
    hedge 300pts further out resolve inside it -- condor legs sit much
    further from spot than the directional spread's (that's the whole
    reason real condor backtests hit a coverage gap against the ~500pt
    reconstructed window), so a unit test meant to exercise successful
    leg selection needs a correspondingly wide fixture, not the live
    chain's real 500pt cap.
    """
    quotes = []
    for i in range(-60, 61):
        strike = spot + i * 50
        offset = strike - spot
        for opt in ("CE", "PE"):
            # OTM side decays with distance from spot, same as before.
            # The ITM side is bumped well above any premium band so
            # find_short_strike (which filters on premium alone, with no
            # moneyness awareness of its own) can never accidentally pick
            # a strike on the wrong side of spot for CE vs PE.
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


def _snapshot(ts, spot=24000.0):
    return MarketSnapshot(symbol="NIFTY", spot=spot, vwap=spot, pcr=1.0,
                          chain=_chain(spot), timestamp=ts, source="dhan_historical")


def _cycles(n=20, spot=24000.0, start="2026-06-01T09:20:00"):
    base = datetime.fromisoformat(start)
    return [(_snapshot(base + timedelta(minutes=5 * i), spot), [], {}) for i in range(n)]


def _mock_single_day(monkeypatch, cycles, day="2026-06-01"):
    monkeypatch.setattr(sc.snapshot_recorder, "load_day", lambda d, **kw: cycles if d == day else [])
    monkeypatch.setattr(sc.snapshot_recorder, "available_days", lambda: [day])


def test_opens_a_condor_when_legs_are_available(monkeypatch):
    _mock_single_day(monkeypatch, _cycles())
    condors, skips = sc.run_policy("2026-06-01")
    assert condors, f"expected a condor to open, got skips={skips}"
    c = condors[0]
    assert c.hedge_ce_strike > c.short_ce_strike
    assert c.hedge_pe_strike < c.short_pe_strike
    assert c.net_credit > 0


def test_min_days_to_expiry_gate_skips_entries_too_close_to_expiry(monkeypatch):
    """
    2026-06-02 is a Tuesday -- its OWN nominal expiry (post-2025-09-01
    regime). With min_days_to_expiry=1 no entry should be allowed on its
    own expiry day, since live would roll to next week's contract, which
    this rolling-series data cannot represent.
    """
    cycles = _cycles(start="2026-06-02T09:20:00")
    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: cycles if d == "2026-06-02" else [])
    monkeypatch.setattr(sc.snapshot_recorder, "available_days", lambda: ["2026-06-02"])

    condors, skips = sc.run_policy("2026-06-02")
    assert condors == []
    # Not counted as a rejected candidate -- the strategy never looked.
    assert skips == {"no_candidate": 0, "coverage_gap": 0, "risk_rejected": 0, "iv_regime_too_low": 0}


def test_coverage_gap_is_distinguished_from_no_candidate(monkeypatch):
    """
    A leg existing but falling outside the reconstructed window is a
    DATA gap, not "no legitimate setup" -- conflating them would hide how
    much of history this backtest cannot actually see.
    """
    narrow = []
    base = datetime.fromisoformat("2026-06-01T09:20:00")
    for i in range(3):
        ts = base + timedelta(minutes=5 * i)
        chain = []
        for k in range(-3, 4):   # narrow chain: no room for the 300pt hedge
            strike = 24000.0 + k * 50
            for opt in ("CE", "PE"):
                chain.append(OptionQuote(
                    symbol="NIFTY", expiry="rolling:week1", strike=strike, option_type=opt,
                    ltp=25.0 if abs(k) >= 2 else 100.0, oi=10000, oi_change_pct=0.0,
                    volume=100, iv=14.0, iv_percentile=50.0, timestamp=ts,
                ))
        narrow.append((MarketSnapshot(symbol="NIFTY", spot=24000.0, vwap=24000.0, pcr=1.0,
                                      chain=chain, timestamp=ts, source="dhan_historical"),
                      [], {}))

    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: narrow if d == "2026-06-01" else [])
    monkeypatch.setattr(sc.snapshot_recorder, "available_days", lambda: ["2026-06-01"])

    condors, skips = sc.run_policy("2026-06-01")
    assert condors == []
    assert skips["coverage_gap"] > 0
    assert skips["no_candidate"] == 0


def test_no_early_exit_exists_only_expiry_settlement(monkeypatch):
    """
    condor_tracker.py only STAGES a breach warning, it never auto-closes.
    Every resolved condor in this backtest must therefore show
    exit_reason == "expiry_settlement", never a profit_target/stop_loss
    style early exit -- there is no such mechanism live to model.
    """
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")
    day2 = _cycles(n=3, start="2026-06-02T09:15:00")
    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sc.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])

    condors, _skips = sc.run_policy("2026-06-01", day_cache={},
                                    all_days=["2026-06-01", "2026-06-02"])
    assert condors
    assert condors[0].exit_reason == "expiry_settlement"


def test_run_all_enforces_one_at_a_time_across_a_day_boundary(monkeypatch):
    """
    The exact bug found in the directional spread module before this one
    was written defensively against it: open_until as a fresh local
    variable per day silently stops blocking new entries once a position
    spans multiple days. A condor position spans a whole expiry week, so
    the exposure here is larger, not smaller.
    """
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")
    day2 = _cycles(n=3, start="2026-06-02T09:15:00")
    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sc.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])

    condors, _cov = sc.run_all(["2026-06-01", "2026-06-02"], sc.CondorPolicy())
    opened_days = [c.opened_at[:10] for c in condors]
    assert opened_days.count("2026-06-01") == 1
    assert opened_days.count("2026-06-02") == 0, (
        "the day-1 condor should still be open (its expiry is later that week) "
        "and must block a fresh day-2 entry"
    )


def test_run_policy_touches_no_state_file_or_journal(monkeypatch, tmp_path):
    import condor_tracker as ct

    sentinel_state = tmp_path / "state.json"
    sentinel_journal = tmp_path / "journal.jsonl"
    monkeypatch.setattr(ct, "STATE_PATH", sentinel_state, raising=False)
    monkeypatch.setattr(ct, "JOURNAL_PATH", sentinel_journal, raising=False)

    _mock_single_day(monkeypatch, _cycles())
    sc.run_policy("2026-06-01")

    assert not sentinel_state.exists()
    assert not sentinel_journal.exists()


def test_unresolved_condor_when_history_ends_inside_its_expiry_week(monkeypatch):
    day1 = _cycles(n=1, start="2026-06-01T09:15:00")
    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: day1 if d == "2026-06-01" else [])
    monkeypatch.setattr(sc.snapshot_recorder, "available_days", lambda: ["2026-06-01"])

    condors, _skips = sc.run_policy("2026-06-01", day_cache={}, all_days=["2026-06-01"])
    assert condors
    assert condors[0].pnl_inr is None
    assert sc.unresolved_count(condors) == 1


def test_expiry_settlement_falls_back_to_intrinsic_for_a_missing_leg(monkeypatch):
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")
    day2 = _cycles(n=2, start="2026-06-02T09:15:00")
    day2[-1][0].chain.clear()

    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sc.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])

    condors, _skips = sc.run_policy("2026-06-01", day_cache={},
                                    all_days=["2026-06-01", "2026-06-02"])
    c = condors[0]
    assert c.exit_reason == "expiry_settlement"
    assert c.pnl_inr is not None


def test_breach_days_are_counted_but_never_cause_an_exit(monkeypatch):
    """
    Spot walking through the short strikes must be visible in reporting
    (breach_days) without altering exit_reason -- condor_tracker.py only
    stages a warning for human review.
    """
    day1 = _cycles(n=2, spot=24000.0, start="2026-06-01T09:15:00")
    day2 = _cycles(n=2, spot=24000.0, start="2026-06-02T09:15:00")
    # Drive spot hard through the short CE strike region on day 2.
    for snap, _c, _m in day2:
        snap.spot = 24900.0
        for q in snap.chain:
            q.strike = 24900.0 - 400 + (q.strike - 24000.0)

    monkeypatch.setattr(sc.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sc.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])

    condors, _skips = sc.run_policy("2026-06-01", day_cache={},
                                    all_days=["2026-06-01", "2026-06-02"])
    assert condors
    assert condors[0].exit_reason == "expiry_settlement"  # never auto-closed by the breach


def test_summarise_handles_no_positions():
    assert "no positions" in sc.summarise([], "empty")


def test_coverage_summary_aggregates_across_days():
    skips = [{"no_candidate": 2, "coverage_gap": 5, "risk_rejected": 1},
            {"no_candidate": 1, "coverage_gap": 0, "risk_rejected": 0}]
    assert sc.coverage_summary(skips) == {"no_candidate": 3, "coverage_gap": 5, "risk_rejected": 1, "iv_regime_too_low": 0}


def test_risk_gate_rejects_a_condor_that_fails_min_net_credit(monkeypatch):
    """
    MIN_NET_CREDIT/MAX_CAPITAL_AT_RISK scale directly with whatever
    premium band and hedge distance a caller is testing -- exactly what a
    config sweep varies. A variant that would be rejected live must be
    rejected here too, not silently traded.
    """
    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(ccfg, "MIN_NET_CREDIT", 10_000.0)  # impossibly high -- nothing can clear it
    condors, skips = sc.run_policy("2026-06-01")
    assert condors == []
    assert skips["risk_rejected"] > 0
