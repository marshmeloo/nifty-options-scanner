"""
Tests for shadow_directional_spread.py.

The failure this pins hardest is the one it actually had: passing
context=None into compute_market_bias left PCR as the only bias input,
scoring 0.0 "neutral/range" on all 709 cycles of a real recorded day. The
backtest reported "no positions" and looked like a legitimate no-signal
result rather than a broken pipeline. A backtest that silently evaluates
nothing is worse than one that crashes.

Run: python -m pytest tests/ -q
"""

from datetime import datetime, timedelta

import pytest

import config_directional_spread as dcfg
import shadow_directional_spread as sds
from models import MarketSnapshot, OptionQuote


def _chain(spot=24000.0):
    """A chain with premiums spanning the short band and room for a hedge."""
    quotes = []
    for i in range(-12, 13):
        strike = spot + i * 50
        for opt in ("CE", "PE"):
            # Rough moneyness-driven premium so exactly one strike per side
            # lands in the SHORT_PREMIUM band.
            distance = abs(strike - spot)
            ltp = max(1.0, 200.0 - distance * 0.35)
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


def test_no_position_when_bias_below_threshold(monkeypatch):
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("neutral/range", 0.0, []))
    assert sds.run_policy("2026-06-01") == []


def test_opens_a_spread_when_bias_is_strong(monkeypatch):
    """
    Guards the context=None bug: a strong bias MUST produce a position.
    If this ever returns [], the pipeline is broken, not the market quiet.
    """
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))

    spreads = sds.run_policy("2026-06-01")
    assert spreads, "a strong bias must open a spread"
    s = spreads[0]
    assert s.direction == "PE"          # bullish is expressed by SELLING puts
    assert s.hedge_strike < s.short_strike
    assert s.net_credit > 0


def test_bearish_bias_sells_call_spread(monkeypatch):
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bearish", -3.0, []))
    s = sds.run_policy("2026-06-01")[0]
    assert s.direction == "CE"
    assert s.hedge_strike > s.short_strike


def test_one_at_a_time_matches_live_single_position_slot(monkeypatch):
    """Live's state file holds exactly one position; the default must too."""
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles(n=40))
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))

    serial = sds.run_policy("2026-06-01", sds.SpreadPolicy(one_at_a_time=True))
    for a, b in zip(serial, serial[1:]):
        assert a.closed_at is not None
        assert datetime.fromisoformat(b.opened_at) > datetime.fromisoformat(a.closed_at)


def test_cycle_with_missing_leg_quote_is_skipped_not_marked_flat(monkeypatch):
    """
    A missing quote is missing information, not a valid mark of zero --
    the distinction behind the 2026-07-30 condor "MTM unavailable" bug.
    """
    cycles = _cycles(n=10)
    # Strip every quote from the cycle right after entry.
    cycles[1][0].chain.clear()

    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: cycles)
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))

    s = sds.run_policy("2026-06-01")[0]
    # It survived the blank cycle and still resolved against later ones.
    assert s.exit_reason is not None
    assert s.cycles_held < len(cycles) - 1


def test_run_policy_touches_no_state_file_or_journal(monkeypatch, tmp_path):
    """
    replay.py once wrote 35 synthetic trades into the real journal. This
    module must be structurally unable to repeat that.
    """
    import directional_spread_tracker as dst

    sentinel_state = tmp_path / "state.json"
    sentinel_journal = tmp_path / "journal.jsonl"
    monkeypatch.setattr(dst, "STATE_PATH", sentinel_state, raising=False)
    monkeypatch.setattr(dst, "JOURNAL_PATH", sentinel_journal, raising=False)

    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))
    sds.run_policy("2026-06-01")

    assert not sentinel_state.exists()
    assert not sentinel_journal.exists()


def test_time_window_is_respected(monkeypatch):
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d: _cycles(n=40))
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))
    spreads = sds.run_policy(
        "2026-06-01", sds.SpreadPolicy(start_time="10:00", end_time="10:30"))
    for s in spreads:
        opened = datetime.fromisoformat(s.opened_at).time()
        assert opened >= datetime.strptime("10:00", "%H:%M").time()
        assert opened <= datetime.strptime("10:30", "%H:%M").time()


def test_context_cache_returns_none_without_candles():
    assert sds._ContextCache().get([]) is None


def test_summarise_handles_no_positions():
    assert "no positions" in sds.summarise([], "empty")


def test_exit_reason_breakdown_counts_reasons():
    made = [sds.ShadowSpread(direction="PE", short_strike=1, hedge_strike=0,
                             opened_at="x", net_credit=1.0, max_profit_inr=1.0,
                             max_loss_inr=1.0, bias_label="bullish", bias_score=3.0,
                             exit_reason=r)
            for r in ("profit_target", "profit_target", "stop_loss")]
    assert sds.exit_reason_breakdown(made) == {"profit_target": 2, "stop_loss": 1}
