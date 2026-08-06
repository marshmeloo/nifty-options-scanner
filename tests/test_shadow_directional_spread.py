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
    """
    A chain with premiums spanning the short band and room for a hedge.

    Premium decays with distance on the OTM side only, and is bumped well
    above any premium band on the ITM side: find_short_strike filters on
    premium alone with no moneyness awareness of its own, so a chain
    where CE and PE decay symmetrically by absolute distance lets it pick
    an ITM strike on the wrong side of spot -- which then prices a
    NEGATIVE net credit (hedge costs more than the short collects) and
    gets correctly rejected by directional_spread_risk_checker's
    MIN_NET_CREDIT gate. Not a hypothetical: this is exactly what the
    unfixed version of this fixture produced for the bearish/CE case.
    """
    quotes = []
    for i in range(-12, 13):
        strike = spot + i * 50
        offset = strike - spot
        for opt in ("CE", "PE"):
            if opt == "CE":
                otm_distance, itm_bump = max(offset, 0.0), max(-offset, 0.0) * 5.0
            else:
                otm_distance, itm_bump = max(-offset, 0.0), max(offset, 0.0) * 5.0
            ltp = max(1.0, 200.0 - otm_distance * 0.35 + itm_bump)
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
    """
    Isolate run_policy to exactly ONE known day. Both load_day AND
    available_days must be mocked: without the latter, _window_days
    would fall back to the real 493-day snapshot directory on disk and
    silently replay `cycles` again for every real date it finds inside
    the nominal expiry window -- not a hypothetical, this is exactly what
    broke here the first time the multi-day walk was added.
    """
    monkeypatch.setattr(sds.snapshot_recorder, "load_day", lambda d, **kw: cycles if d == day else [])
    monkeypatch.setattr(sds.snapshot_recorder, "available_days", lambda: [day])


def test_no_position_when_bias_below_threshold(monkeypatch):
    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("neutral/range", 0.0, []))
    assert sds.run_policy("2026-06-01") == []


def test_opens_a_spread_when_bias_is_strong(monkeypatch):
    """
    Guards the context=None bug: a strong bias MUST produce a position.
    If this ever returns [], the pipeline is broken, not the market quiet.
    """
    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))

    spreads = sds.run_policy("2026-06-01")
    assert spreads, "a strong bias must open a spread"
    s = spreads[0]
    assert s.direction == "PE"          # bullish is expressed by SELLING puts
    assert s.hedge_strike < s.short_strike
    assert s.net_credit > 0


def test_bearish_bias_sells_call_spread(monkeypatch):
    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bearish", -3.0, []))
    s = sds.run_policy("2026-06-01")[0]
    assert s.direction == "CE"
    assert s.hedge_strike > s.short_strike


def test_one_at_a_time_matches_live_single_position_slot(monkeypatch):
    """Live's state file holds exactly one position; the default must too."""
    _mock_single_day(monkeypatch, _cycles(n=40))
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

    _mock_single_day(monkeypatch, cycles)
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

    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))
    sds.run_policy("2026-06-01")

    assert not sentinel_state.exists()
    assert not sentinel_journal.exists()


def test_at_most_one_new_position_per_day_by_default(monkeypatch):
    """
    Found by comparing a real backtest run against config: one_at_a_time
    alone only blocks entry while a position is still OPEN, so a position
    that closes early (stop_loss within minutes) left the door open for a
    second brand-new entry later the SAME day -- something live's
    opened_today count would refuse. 21 of 158 positions across 12 days
    violated this before the default was added, worth Rs 14,303 (15%
    of that run's total).
    """
    cycles = _cycles(n=40)  # entries could otherwise open, close fast, reopen same day
    _mock_single_day(monkeypatch, cycles)
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    # Force every position to resolve almost immediately, so a second
    # same-day entry would be possible if the cap weren't enforced.
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: "profit_target")

    spreads = sds.run_policy("2026-06-01")
    assert len(spreads) == dcfg.MAX_NEW_POSITIONS_PER_DAY


def test_max_positions_per_day_override_is_respected(monkeypatch):
    """
    Overriding the POLICY alone isn't enough: directional_spread_risk_checker
    reads dcfg.MAX_NEW_POSITIONS_PER_DAY directly at call time regardless
    of what the backtest Policy claims, so a caller testing a wider daily
    cap must patch the live config to match -- otherwise the real risk
    gate correctly refuses a 2nd/3rd same-day entry the live system would
    never have taken either.
    """
    cycles = _cycles(n=40)
    _mock_single_day(monkeypatch, cycles)
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: "profit_target")
    monkeypatch.setattr(dcfg, "MAX_NEW_POSITIONS_PER_DAY", 3)

    spreads = sds.run_policy("2026-06-01", sds.SpreadPolicy(max_positions_per_day=3))
    assert len(spreads) == 3


def test_time_window_is_respected(monkeypatch):
    _mock_single_day(monkeypatch, _cycles(n=40))
    monkeypatch.setattr(sds, "compute_market_bias",
                        lambda snap, ctx: ("bullish", 3.0, []))
    spreads = sds.run_policy(
        "2026-06-01", sds.SpreadPolicy(start_time="10:00", end_time="10:30"))
    for s in spreads:
        opened = datetime.fromisoformat(s.opened_at).time()
        assert opened >= datetime.strptime("10:00", "%H:%M").time()
        assert opened <= datetime.strptime("10:30", "%H:%M").time()


# --- multi-day expiry-bounded walk --------------------------------------
#
# The strategy is explicitly held overnight to expiry. Resolving a
# position within the single day it opened force-closed everything at
# 15:30 day one, banking the theta gain from a few hours' hold while
# never seeing the gap/breach risk that actually threatens these
# positions -- it produced a 73% "win rate" that measured only the calm
# middle of the P&L path. These tests pin the multi-day walk that
# replaced it, bounded to the position's own real expiry week (see
# _window_days) since the underlying data is a ROLLING nearest-expiry
# series -- the same (strike, option_type) key on two different weeks can
# be two different actual contracts.

def test_nominal_expiry_before_the_tuesday_switch_is_thursday():
    # 2025-08-25 is a Monday, before the 2025-09-01 Thu->Tue changeover.
    d = sds.date(2025, 8, 25)
    assert sds._nominal_expiry_date(d) == sds.date(2025, 8, 28)  # Thursday


def test_nominal_expiry_on_or_after_the_switch_is_tuesday():
    # 2026-06-01 is a Monday, well after the changeover.
    d = sds.date(2026, 6, 1)
    assert sds._nominal_expiry_date(d) == sds.date(2026, 6, 2)  # Tuesday


def test_nominal_expiry_of_the_expiry_day_itself_is_that_same_day():
    d = sds.date(2026, 6, 2)  # a Tuesday
    assert sds._nominal_expiry_date(d) == d


def test_position_walks_forward_across_a_day_boundary(monkeypatch):
    """
    A managed exit on day 2 must be reachable -- the whole point of this
    fix. day1 has no exit-triggering move; day2 does.
    """
    day1 = _cycles(n=5, start="2026-06-01T09:15:00")   # Monday
    day2 = _cycles(n=5, start="2026-06-02T09:15:00")   # Tuesday (nominal expiry)

    monkeypatch.setattr(sds.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sds.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))

    spreads = sds.run_policy("2026-06-01", day_cache={}, all_days=["2026-06-01", "2026-06-02"])
    assert spreads
    s = spreads[0]
    closed = datetime.fromisoformat(s.closed_at)
    assert closed.date() == datetime(2026, 6, 2).date(), (
        "the position never left day 1's data -- the cross-day walk did not run"
    )


def test_walk_never_reads_a_cycle_beyond_the_positions_own_expiry(monkeypatch):
    """
    The rolling-series identity hazard this whole design exists to avoid:
    a cycle recorded AFTER the position's nominal expiry must never be
    consulted, since by then the same strike key refers to next week's
    contract, not the one that was actually opened.
    """
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")     # Monday, entry day
    day2 = _cycles(n=3, start="2026-06-02T09:15:00")     # Tuesday, nominal expiry
    day3 = _cycles(n=3, start="2026-06-03T09:15:00")     # Wednesday -- next week's contract

    seen_day3 = []

    def fake_load(d, **kw):
        if d == "2026-06-03":
            seen_day3.append(d)
        return {"2026-06-01": day1, "2026-06-02": day2, "2026-06-03": day3}.get(d, [])

    monkeypatch.setattr(sds.snapshot_recorder, "load_day", fake_load)
    monkeypatch.setattr(sds.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02", "2026-06-03"])
    # No managed exit ever fires, forcing the walk to run out the window.
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: None)

    sds.run_policy("2026-06-01", day_cache={},
                   all_days=["2026-06-01", "2026-06-02", "2026-06-03"])

    assert not seen_day3, "day 3 (past nominal expiry) must never be loaded for this position"


def test_unresolved_position_when_history_ends_inside_its_expiry_week(monkeypatch):
    """
    A position opened on the very last recorded cycle, with no further
    data before its own nominal expiry, must be left unresolved -- never
    fabricate a close from data that doesn't exist.
    """
    day1 = _cycles(n=1, start="2026-06-01T09:15:00")  # entry cycle, then nothing

    monkeypatch.setattr(sds.snapshot_recorder, "load_day",
                        lambda d, **kw: day1 if d == "2026-06-01" else [])
    monkeypatch.setattr(sds.snapshot_recorder, "available_days", lambda: ["2026-06-01"])
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: None)

    spreads = sds.run_policy("2026-06-01", day_cache={}, all_days=["2026-06-01"])
    assert spreads
    assert spreads[0].pnl_inr is None
    assert sds.unresolved_count(spreads) == 1


def test_risk_gate_rejects_a_spread_that_fails_min_net_credit(monkeypatch):
    """
    MIN_NET_CREDIT/MAX_CAPITAL_AT_RISK scale directly with whatever
    premium band and hedge distance a caller is testing -- exactly what a
    config sweep varies. A variant that would be rejected live must be
    rejected here too, not silently traded.
    """
    _mock_single_day(monkeypatch, _cycles())
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    monkeypatch.setattr(dcfg, "MIN_NET_CREDIT", 10_000.0)  # impossibly high

    assert sds.run_policy("2026-06-01") == []


def test_run_all_enforces_one_at_a_time_across_a_day_boundary(monkeypatch):
    """
    The actual bug: open_until used to be a fresh local variable inside
    run_policy, reset to None every time the day loop called it again --
    so a position opened day 1 and still open on day 2 had no way to
    block a fresh entry on day 2. Caught by auditing a real run: 29 of
    137 positions overlapped illegally. run_all() must not repeat it.
    """
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")   # Monday, opens a position
    day2 = _cycles(n=3, start="2026-06-02T09:15:00")   # Tuesday -- position still open here

    monkeypatch.setattr(sds.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sds.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    # Never let the day-1 position resolve on its own, so it is
    # GUARANTEED to still be open when day 2's scan runs.
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: None)

    spreads = sds.run_all(["2026-06-01", "2026-06-02"], sds.SpreadPolicy())

    opened_days = [s.opened_at[:10] for s in spreads]
    assert opened_days.count("2026-06-01") == 1
    assert opened_days.count("2026-06-02") == 0, (
        "a position from 2026-06-01 was still open on 2026-06-02 -- "
        "one_at_a_time must have blocked a new entry that day"
    )


def test_run_all_matches_a_manually_chained_scan_day_sequence(monkeypatch):
    """Pins run_all as exactly 'thread open_until through _scan_day per day', nothing more."""
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")
    day2 = _cycles(n=3, start="2026-06-02T09:15:00")
    monkeypatch.setattr(sds.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sds.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))

    via_run_all = sds.run_all(["2026-06-01", "2026-06-02"], sds.SpreadPolicy())

    cache = {}
    open_until = None
    manual = []
    for day, cycles in (("2026-06-01", day1), ("2026-06-02", day2)):
        s, open_until = sds._scan_day(day, sds.SpreadPolicy(), cycles, cache,
                                      ["2026-06-01", "2026-06-02"], open_until)
        manual.extend(s)

    assert [s.opened_at for s in via_run_all] == [s.opened_at for s in manual]


def test_expiry_settlement_falls_back_to_intrinsic_for_a_missing_leg(monkeypatch):
    """Mirrors directional_spread_tracker.close_position's own fallback exactly."""
    day1 = _cycles(n=3, start="2026-06-01T09:15:00")
    day2 = _cycles(n=2, start="2026-06-02T09:15:00")   # nominal expiry day
    day2[-1][0].chain.clear()   # both legs unpriced on the very last cycle

    monkeypatch.setattr(sds.snapshot_recorder, "load_day",
                        lambda d, **kw: {"2026-06-01": day1, "2026-06-02": day2}.get(d, []))
    monkeypatch.setattr(sds.snapshot_recorder, "available_days",
                        lambda: ["2026-06-01", "2026-06-02"])
    monkeypatch.setattr(sds, "compute_market_bias", lambda snap, ctx: ("bullish", 3.0, []))
    monkeypatch.setattr(sds, "check_managed_exit", lambda mtm, plan: None)

    spreads = sds.run_policy("2026-06-01", day_cache={}, all_days=["2026-06-01", "2026-06-02"])
    s = spreads[0]
    assert s.exit_reason == "expiry_settlement"
    assert s.pnl_inr is not None, "intrinsic fallback should still produce a real settlement"


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
