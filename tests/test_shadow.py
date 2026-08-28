"""
Tests for shadow.py -- simulating a trading policy against recorded
history without risking capital.

The most important tests here pin a real bug found while using the tool
for the first time on a genuine complete day (2026-07-29): the
stop_after_loss policy used to end the trading day the instant ANY
simulated trade resolved non-positive, including trades that only
finished negative at EOD_CLOSE -- an outcome only knowable at 15:30, by
which point there is no more day left to stop trading during. Worse,
because every trade is resolved instantly by walking forward through
already-recorded prices, the flag was being set using information from
the future relative to the scan cycle that triggered it, silently
blocking entries that (in real time) happened before the loss was
knowable at all.

Run: python -m pytest tests/ -q
"""

import datetime

import pytest

import config
from shadow import (
    Policy, ShadowTrade, _closeable_price, _finalise, walk_trade_forward,
    day_is_complete, warn_if_incomplete,
)


def _series(entries):
    """entries: list of (minute_offset, bid, ask, ltp) -> price-index series."""
    t0 = datetime.datetime(2026, 7, 29, 9, 15)
    return [(t0 + datetime.timedelta(minutes=m), bid, ask, ltp) for m, bid, ask, ltp in entries]


def _trade(entry=100.0, stop=70.0, target=160.0):
    return ShadowTrade(
        strike=24000.0, option_type="PE", opened_at="2026-07-29T09:15:00",
        entry=entry, stop=stop, target=target, score=6.0, adjusted_score=6.0,
    )


# --------------------------------------------------------------------------
# _closeable_price / walk_trade_forward mechanics
# --------------------------------------------------------------------------

def test_closeable_price_uses_bid_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", True)
    assert _closeable_price(bid=95.0, ltp=97.0) == 95.0


def test_closeable_price_falls_back_to_ltp_without_a_bid(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", True)
    assert _closeable_price(bid=None, ltp=97.0) == 97.0


def test_walk_forward_hits_target(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", False)
    index = {("24000.0", "PE"): _series([(5, None, None, 120.0), (10, None, None, 165.0)])}
    trade = _trade()
    entry_ts = datetime.datetime(2026, 7, 29, 9, 15)
    result = walk_trade_forward(index, ("24000.0", "PE"), entry_ts, trade)
    assert result.outcome == "WIN"
    assert result.exit_price == 165.0


def test_walk_forward_hits_stop(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", False)
    index = {("24000.0", "PE"): _series([(5, None, None, 85.0), (10, None, None, 65.0)])}
    trade = _trade()
    result = walk_trade_forward(index, ("24000.0", "PE"), datetime.datetime(2026, 7, 29, 9, 15), trade)
    assert result.outcome == "LOSS"
    assert result.exit_price == 65.0


def test_walk_forward_never_resolves_closes_at_eod(monkeypatch):
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", False)
    index = {("24000.0", "PE"): _series([(5, None, None, 110.0), (300, None, None, 115.0)])}
    trade = _trade()
    result = walk_trade_forward(index, ("24000.0", "PE"), datetime.datetime(2026, 7, 29, 9, 15), trade)
    assert result.outcome == "EOD_CLOSE"
    assert result.exit_price == 115.0


def test_walk_forward_ignores_prices_at_or_before_entry(monkeypatch):
    """A price stamped exactly at entry time must not be treated as a later tick."""
    monkeypatch.setattr(config, "USE_BID_ASK_FILLS", False)
    entry_ts = datetime.datetime(2026, 7, 29, 9, 15)
    index = {("24000.0", "PE"): [(entry_ts, None, None, 200.0),  # same instant, must be skipped
                                 (entry_ts + datetime.timedelta(minutes=5), None, None, 110.0)]}
    trade = _trade()
    result = walk_trade_forward(index, ("24000.0", "PE"), entry_ts, trade)
    assert result.exit_price == 110.0  # not 200.0, and outcome resolves at EOD/next tick


# --------------------------------------------------------------------------
# The stop_after_loss timing bug
# --------------------------------------------------------------------------

def test_finalise_marks_genuine_stop_hit_as_LOSS():
    trade = _trade(entry=100.0, stop=70.0, target=160.0)
    t0 = datetime.datetime(2026, 7, 29, 9, 15)
    result = _finalise(trade, t0 + datetime.timedelta(minutes=5), 65.0, "LOSS", peak=100.0, trough=65.0, lots=1)
    assert result.outcome == "LOSS"


def test_eod_negative_is_not_a_LOSS_outcome():
    """
    The core distinction the bug missed: a trade that merely finishes
    negative at end of day is EOD_CLOSE, not LOSS. Only a genuine
    stop-hit is a discrete, real-time event a live trader could act on.
    """
    trade = _trade(entry=100.0, stop=70.0, target=160.0)
    t0 = datetime.datetime(2026, 7, 29, 15, 25)
    result = _finalise(trade, t0, 92.0, "EOD_CLOSE", peak=105.0, trough=90.0, lots=1)
    assert result.outcome == "EOD_CLOSE"
    assert result.net_r < 0          # negative...
    assert result.outcome != "LOSS"  # ...but NOT the outcome stop_after_loss should act on


def test_stop_after_loss_gate_uses_the_closed_at_timestamp_not_open_order():
    """
    Direct regression test for the bug: reproduces run_policy's gating
    logic in isolation. A trade opened at 09:30 that doesn't resolve
    (genuinely stop out) until 11:00 must not block a candidate cycle at
    09:45 -- that entry happens in real time BEFORE the loss is knowable.
    It SHOULD block a candidate cycle at 11:30, which comes after.

    This mirrors the exact gating condition in run_policy(): a cycle is
    skipped once `loss_known_at is not None and ts >= loss_known_at`.
    """
    loss_known_at = None

    # Trade A: opened 09:30, walks forward and doesn't actually stop out
    # until 11:00 (e.g. it hovered near breakeven for 90 minutes).
    trade_a = _trade()
    trade_a.outcome = "LOSS"
    trade_a.closed_at = datetime.datetime(2026, 7, 29, 11, 0).isoformat()

    if trade_a.outcome == "LOSS" and trade_a.closed_at:
        closed = datetime.datetime.fromisoformat(trade_a.closed_at)
        if loss_known_at is None or closed < loss_known_at:
            loss_known_at = closed

    cycle_0945 = datetime.datetime(2026, 7, 29, 9, 45)
    cycle_1130 = datetime.datetime(2026, 7, 29, 11, 30)

    assert not (loss_known_at is not None and cycle_0945 >= loss_known_at), (
        "a candidate at 09:45 must still be allowed -- the 11:00 stop-out isn't knowable yet"
    )
    assert (loss_known_at is not None and cycle_1130 >= loss_known_at), (
        "a candidate at 11:30 must be blocked -- the loss is now known"
    )


def test_stop_after_loss_end_to_end_does_not_block_earlier_same_day_entries(monkeypatch):
    """
    End-to-end version of the same bug using the real run_policy(), with
    a scanner/plan pipeline stubbed out so the test is deterministic:
    trade 1 opens early and doesn't stop out until late morning; trade 2
    is a genuinely separate signal that fires in between. Trade 2 must
    still be taken.
    """
    import shadow

    t0 = datetime.datetime(2026, 7, 29, 9, 15)

    # Two fake "cycles": snapshot content doesn't matter here because scan/
    # plan/risk are monkeypatched below to hand back fixed setups.
    class FakeSnapshot:
        def __init__(self, ts):
            self.timestamp = ts
            self.chain = []
            self.oi_analysis = None
            self.spot = 24000.0

    cycles = [
        (FakeSnapshot(t0 + datetime.timedelta(minutes=0)), [], {}),    # 09:15 open trade A
        (FakeSnapshot(t0 + datetime.timedelta(minutes=15)), [], {}),   # 09:30 -- would open trade B
        (FakeSnapshot(t0 + datetime.timedelta(minutes=200)), [], {}),  # ~12:35, after A's real stop-out
    ]

    class FakeSetup:
        def __init__(self, strike, opt, reasons=("x",), score=6.0):
            self.strike, self.option_type, self.reasons, self.score = strike, opt, list(reasons), score
            self.expiry = "2026-08-04"

    class FakePlan:
        def __init__(self, entry, stop, target=None):
            self.entry, self.stop, self.lots = entry, stop, 1
            self.target = target or entry + (entry - stop) * 2

    class FakeVerdict:
        decision = "APPROVED"

    setups_by_cycle = {
        0: [FakeSetup(24000.0, "PE")],
        1: [FakeSetup(24050.0, "PE")],
        2: [],
    }

    call_count = {"i": -1}

    def fake_scan(snapshot, price_levels=None, context=None):
        call_count["i"] += 1
        return setups_by_cycle.get(call_count["i"], [])

    monkeypatch.setattr(shadow, "scan", fake_scan)
    monkeypatch.setattr(shadow, "compute_market_bias", lambda snap, ctx: ("neutral/range", 0.0, []))
    monkeypatch.setattr(shadow.tt, "apply_learned_adjustment", lambda score, reasons: (score, []))
    monkeypatch.setattr(shadow, "build_plan",
                        lambda snap, setup, atr=None: FakePlan(entry=100.0, stop=70.0))
    monkeypatch.setattr(shadow, "check", lambda plan, a, b, c: FakeVerdict())
    monkeypatch.setattr(shadow, "snapshot_recorder", type("_", (), {"load_day": staticmethod(lambda d: cycles)}))

    # Trade A (24000 PE, opened 09:15) stops out at 11:00 -- long after
    # the 09:30 cycle that should open trade B.
    index = {
        ("24000.0", "PE"): _series([(105, None, None, 65.0)]),          # 11:00 -> stop hit
        ("24050.0", "PE"): _series([(60, None, None, 140.0)]),          # 10:15 -> resolves fine
    }
    monkeypatch.setattr(shadow, "build_price_index", lambda cycles: index)

    policy = Policy(name="test", min_score=5.0, stop_after_loss=True, one_at_a_time=False)
    trades = shadow.run_policy("2026-07-29", policy)

    opened_strikes = {t.strike for t in trades}
    assert 24000.0 in opened_strikes, "trade A must open"
    assert 24050.0 in opened_strikes, (
        "trade B (09:30, before A's 11:00 stop-out was knowable) must NOT be "
        "blocked -- this is exactly the look-ahead bug that was fixed"
    )


# --------------------------------------------------------------------------
# Incomplete-day guard
# --------------------------------------------------------------------------

def test_day_is_complete_true_when_recording_reaches_close():
    class FakeSnap:
        def __init__(self, t):
            self.timestamp = t
    cycles = [(FakeSnap(datetime.datetime(2026, 7, 29, 15, 29)), [], {})]
    assert day_is_complete(cycles) is True


def test_day_is_complete_false_when_truncated_midday():
    class FakeSnap:
        def __init__(self, t):
            self.timestamp = t
    cycles = [(FakeSnap(datetime.datetime(2026, 7, 29, 11, 49)), [], {})]
    assert day_is_complete(cycles) is False


def test_warn_if_incomplete_returns_none_for_a_complete_day():
    class FakeSnap:
        def __init__(self, t):
            self.timestamp = t
    cycles = [(FakeSnap(datetime.datetime(2026, 7, 29, 15, 29)), [], {})]
    assert warn_if_incomplete("2026-07-29", cycles) is None


def test_warn_if_incomplete_flags_a_partial_day():
    class FakeSnap:
        def __init__(self, t):
            self.timestamp = t
    cycles = [(FakeSnap(datetime.datetime(2026, 7, 29, 11, 49)), [], {})]
    warning = warn_if_incomplete("2026-07-29", cycles)
    assert warning is not None
    assert "INCOMPLETE" in warning


# --- risk-gate state (daily-loss breaker + exposure cap) ---------------
#
# shadow.py passed hardcoded 0.0/0.0 to risk_checker.check() until
# 2026-08-01, so MAX_DAILY_LOSS_PCT and MAX_TOTAL_EXPOSURE_PCT were dead
# in every backtest -- exactly the defect trade_tracker.compute_risk_state
# was written to fix on the LIVE side. It went unnoticed because baseline
# scoring never traded often enough to breach either limit; a variant
# trading 5x a day breached the daily-loss cap on 2% of days carrying 41%
# of its P&L.

from shadow import risk_state_at


def _pos(key, entry, stop, lots, opened, closed, net_inr):
    t0 = datetime.datetime(2026, 7, 29, 9, 15)
    return {
        "key": key, "entry": entry, "stop": stop, "lots": lots,
        "opened_ts": t0 + datetime.timedelta(minutes=opened),
        "closed_ts": None if closed is None else t0 + datetime.timedelta(minutes=closed),
        "net_inr": net_inr,
    }


def _at(minute):
    return datetime.datetime(2026, 7, 29, 9, 15) + datetime.timedelta(minutes=minute)


def test_realized_loss_is_invisible_until_the_trade_actually_closes():
    """
    Every simulated trade is resolved instantly by walking forward, so its
    loss sits in memory long before it happens in simulated time. Counting
    it early would trip the breaker at 10:00 on a 14:00 loss -- the same
    look-ahead already fixed once in stop_after_loss.
    """
    positions = [_pos(("24000.0", "PE"), 100.0, 90.0, 1, opened=0, closed=300, net_inr=-25_000)]

    _exp, loss_before = risk_state_at({}, positions, _at(60))
    _exp, loss_after = risk_state_at({}, positions, _at(301))

    assert loss_before == 0.0, "a loss that has not happened yet must not gate anything"
    assert loss_after > 0.0


def test_daily_loss_pct_is_expressed_against_total_capital():
    positions = [_pos(("24000.0", "PE"), 100.0, 90.0, 1, opened=0, closed=10, net_inr=-15_000)]
    _exp, loss = risk_state_at({}, positions, _at(20))
    assert loss == round(15_000 / config.TOTAL_CAPITAL * 100, 4)


def test_a_profitable_day_reports_zero_loss_not_a_negative():
    """The breaker only cares about losses; a green day must read 0.0."""
    positions = [_pos(("24000.0", "PE"), 100.0, 90.0, 1, opened=0, closed=10, net_inr=+40_000)]
    _exp, loss = risk_state_at({}, positions, _at(20))
    assert loss == 0.0


def test_open_positions_count_toward_exposure_only_while_open():
    positions = [_pos(("24000.0", "PE"), 100.0, 90.0, 2, opened=10, closed=60, net_inr=0.0)]
    lot = getattr(config, "NIFTY_LOT_SIZE", 65)
    expected = (100.0 - 90.0) * lot * 2 / config.TOTAL_CAPITAL * 100

    before, _l = risk_state_at({}, positions, _at(5))
    during, _l = risk_state_at({}, positions, _at(30))
    after, _l = risk_state_at({}, positions, _at(90))

    assert before == 0.0
    assert round(during, 4) == round(expected, 4)
    assert after == 0.0, "a closed position must stop consuming exposure"


def test_unrealized_loss_on_an_open_position_counts_toward_the_breaker():
    """Live's compute_risk_state includes unrealized loss; so must this."""
    key = ("24000.0", "PE")
    index = {key: _series([(0, 100.0, 101.0, 100.0), (30, 60.0, 61.0, 60.0)])}
    positions = [_pos(key, 100.0, 90.0, 1, opened=0, closed=None, net_inr=None)]

    _exp, loss = risk_state_at(index, positions, _at(30))
    lot = getattr(config, "NIFTY_LOT_SIZE", 65)
    assert loss == round((100.0 - 60.0) * lot / config.TOTAL_CAPITAL * 100, 4)


def test_unrealized_uses_the_last_price_at_or_before_now_never_a_future_one():
    key = ("24000.0", "PE")
    index = {key: _series([(0, 100.0, 101.0, 100.0),
                           (30, 60.0, 61.0, 60.0),
                           (90, 10.0, 11.0, 10.0)])}
    positions = [_pos(key, 100.0, 90.0, 1, opened=0, closed=None, net_inr=None)]

    _exp, at_30 = risk_state_at(index, positions, _at(30))
    _exp, at_90 = risk_state_at(index, positions, _at(90))
    assert at_30 < at_90, "the 90-minute price must not leak into the 30-minute reading"


# --------------------------------------------------------------------------
# _StructureCache -- the stale forming-candle bug (found 2026-08-28)
#
# The cache key used to be (len(candles), candles[-1].timestamp), which
# CANNOT distinguish two cycles inside the same still-forming candle even
# though that candle's high/low/close/volume have moved. run_policy()
# therefore scanned on structure up to a full candle stale, while live
# (main_live*.py) recomputes analyze_with_context()/compute_atr() fresh
# every cycle and has no cache at all. See _StructureCache's own docstring
# for the measured 2026-08-17 case (NIFTY 24300 CE: 6.0 live, 3.0 stale --
# straddling MIN_CONVICTION_SCORE_TO_TRACK, so the backtest never took a
# trade live really did take).
# --------------------------------------------------------------------------

def _candle(minute, close, high=None, low=None, volume=1000):
    from models import Candle
    return Candle(
        timestamp=datetime.datetime(2026, 8, 17, 13, minute),
        open=100.0, high=high if high is not None else close,
        low=low if low is not None else close, close=close, volume=volume,
    )


def test_structure_cache_recomputes_while_the_last_candle_is_still_forming(monkeypatch):
    """The regression this whole fix exists for: same candle COUNT, same
    last-candle TIMESTAMP, but the forming candle's OHLCV has moved -- the
    cache must NOT serve the earlier answer."""
    import price_action
    from shadow import _StructureCache

    calls = []
    monkeypatch.setattr(price_action, "analyze_with_context",
                        lambda candles: (calls.append(candles[-1].close), ([], None))[1])
    monkeypatch.setattr(price_action, "compute_atr", lambda candles: 1.0)

    cache = _StructureCache()
    forming_early = [_candle(0, close=24338.85, high=24339.30, volume=256492)]
    forming_later = [_candle(0, close=24343.25, high=24347.45, volume=1284466)]

    cache.get(forming_early)
    cache.get(forming_later)

    assert calls == [24338.85, 24343.25], (
        "the forming candle moved; the cache served a stale structure")


def test_structure_cache_still_caches_when_nothing_changed(monkeypatch):
    """The fix must not turn the cache off -- an identical candle series
    across two cycles still recomputes only once."""
    import price_action
    from shadow import _StructureCache

    calls = []
    monkeypatch.setattr(price_action, "analyze_with_context",
                        lambda candles: (calls.append(1), ([], None))[1])
    monkeypatch.setattr(price_action, "compute_atr", lambda candles: 1.0)

    cache = _StructureCache()
    candles = [_candle(0, close=24338.85, high=24339.30, volume=256492)]

    cache.get(candles)
    cache.get(list(candles))  # a different list object, identical content

    assert calls == [1], "identical candles should not trigger a recompute"


def test_structure_cache_recomputes_when_a_new_candle_appears(monkeypatch):
    import price_action
    from shadow import _StructureCache

    calls = []
    monkeypatch.setattr(price_action, "analyze_with_context",
                        lambda candles: (calls.append(len(candles)), ([], None))[1])
    monkeypatch.setattr(price_action, "compute_atr", lambda candles: 1.0)

    cache = _StructureCache()
    cache.get([_candle(0, close=24338.85)])
    cache.get([_candle(0, close=24338.85), _candle(1, close=24350.0)])

    assert calls == [1, 2]


# --------------------------------------------------------------------------
# Backtest-vs-live fidelity fixes (2026-08-28). Each of these closed a gap
# where shadow.py silently modelled a DIFFERENT strategy than the one live
# runs -- see BACKLOG.md for how they were found and measured.
# --------------------------------------------------------------------------

class _Q:
    """Minimal stand-in for OptionQuote's fields these helpers touch."""
    def __init__(self, strike=24000.0, option_type="CE", ltp=100.0, iv=12.0,
                 delta=None, bid=None, ask=None, expiry="2026-09-03"):
        self.strike, self.option_type, self.ltp, self.iv = strike, option_type, ltp, iv
        self.delta, self.bid, self.ask, self.expiry = delta, bid, ask, expiry


class _Snap:
    def __init__(self, chain, spot=24000.0, ts=None):
        self.chain = chain
        self.spot = spot
        self.timestamp = ts or datetime.datetime(2026, 8, 28, 11, 0)


def test_fill_missing_delta_populates_only_what_is_missing():
    """Reconstructed history has no Greeks, so plan_generator fell back to a
    FLAT 30% stop while live computed 15-24% from ATR x delta."""
    from shadow import fill_missing_delta
    missing = _Q(strike=24000.0, option_type="CE", delta=None)
    already = _Q(strike=24100.0, option_type="CE", delta=0.42)
    snap = _Snap([missing, already])

    n = fill_missing_delta(snap)

    assert n == 1
    assert missing.delta is not None and 0 < missing.delta < 1
    assert already.delta == 0.42, "a real delta must never be overwritten"


def test_fill_missing_delta_signs_puts_negative():
    from shadow import fill_missing_delta
    ce, pe = _Q(option_type="CE"), _Q(option_type="PE")
    fill_missing_delta(_Snap([ce, pe]))
    assert ce.delta > 0 and pe.delta < 0


def test_fill_missing_delta_skips_quotes_with_no_iv():
    """No IV means no Black-Scholes input -- must leave delta None so the
    caller still takes its documented flat-stop fallback."""
    from shadow import fill_missing_delta
    q = _Q(iv=0)
    assert fill_missing_delta(_Snap([q])) == 0
    assert q.delta is None


def test_fill_missing_book_straddles_ltp_and_leaves_real_books_alone():
    """Without this the backtest transacted at LTP on BOTH legs and paid no
    spread, while live pays the ask and receives the bid."""
    from shadow import fill_missing_book
    synth = _Q(ltp=100.0, bid=None, ask=None)
    real = _Q(ltp=100.0, bid=99.0, ask=101.0)

    n = fill_missing_book(_Snap([synth, real]), spread_pct=0.266)

    assert n == 1
    assert synth.bid < 100.0 < synth.ask
    assert (synth.ask - synth.bid) == pytest.approx(100.0 * 0.00266, abs=0.02)
    assert (real.bid, real.ask) == (99.0, 101.0), "a real book must never be overwritten"


def test_fill_missing_book_disabled_by_zero_spread():
    from shadow import fill_missing_book
    q = _Q(ltp=100.0)
    assert fill_missing_book(_Snap([q]), spread_pct=0) == 0
    assert q.bid is None and q.ask is None


def test_walk_forward_applies_the_breakeven_arm(monkeypatch):
    """config.BREAKEVEN_ARM_R runs live (11% of real Sentinel trades closed
    BREAKEVEN_STOP) but shadow could not produce that outcome at all until
    2026-08-28."""
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", 0.5)
    # entry 100, stop 70 -> 1R = 30. Peak 118 clears 0.5R (=115), then the
    # price falls back through entry: breakeven, not a slide to the stop.
    index = {("24000.0", "PE"): _series([
        (1, 118.0, 119.0, 118.0),
        (2, 99.0, 100.0, 99.0),
    ])}
    t = walk_trade_forward(index, ("24000.0", "PE"),
                           datetime.datetime(2026, 7, 29, 9, 15), _trade())
    assert t.outcome == "BREAKEVEN_STOP"


def test_breakeven_arm_does_not_fire_before_it_arms(monkeypatch):
    """A trade that never reached 0.5R must still run to its original stop."""
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", 0.5)
    index = {("24000.0", "PE"): _series([
        (1, 104.0, 105.0, 104.0),   # only +0.13R, nowhere near arming
        (2, 69.0, 70.0, 69.0),
    ])}
    t = walk_trade_forward(index, ("24000.0", "PE"),
                           datetime.datetime(2026, 7, 29, 9, 15), _trade())
    assert t.outcome == "LOSS"


def test_breakeven_arm_never_pre_empts_a_target_hit(monkeypatch):
    """Same ordering live uses: an outright win beats the breakeven floor."""
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", 0.5)
    index = {("24000.0", "PE"): _series([(1, 160.0, 161.0, 160.0)])}
    t = walk_trade_forward(index, ("24000.0", "PE"),
                           datetime.datetime(2026, 7, 29, 9, 15), _trade())
    assert t.outcome == "WIN"


def test_breakeven_arm_off_reproduces_old_behaviour(monkeypatch):
    monkeypatch.setattr(config, "BREAKEVEN_ARM_R", None)
    index = {("24000.0", "PE"): _series([
        (1, 118.0, 119.0, 118.0),
        (2, 99.0, 100.0, 99.0),
        (3, 69.0, 70.0, 69.0),
    ])}
    t = walk_trade_forward(index, ("24000.0", "PE"),
                           datetime.datetime(2026, 7, 29, 9, 15), _trade())
    assert t.outcome == "LOSS"


def test_config_overrides_are_restored_after_a_replay(monkeypatch):
    """Bank Nifty replays apply that process's config patches for their
    duration (the same way main_live_banknifty_sentinel.py does). A
    backtest must not leave global config changed for whatever runs next."""
    import shadow as _shadow
    before = (config.NIFTY_LOT_SIZE, config.PREMIUM_MIN, config.PREMIUM_MAX)
    monkeypatch.setattr(_shadow, "snapshot_recorder",
                        type("_", (), {"load_day": staticmethod(lambda d, **kw: [])}))
    _shadow.run_policy("2026-08-27", Policy(
        name="bn", config_overrides=_shadow.BANKNIFTY_SENTINEL_OVERRIDES))
    assert (config.NIFTY_LOT_SIZE, config.PREMIUM_MIN, config.PREMIUM_MAX) == before


def test_banknifty_overrides_match_the_live_process_file():
    """These exist only to mirror main_live_banknifty_sentinel.py. If that
    file changes a value and this constant doesn't, Bank Nifty replays
    silently model a different process -- so read the real file."""
    import re, shadow as _shadow
    from pathlib import Path as _P
    source = (_P(__file__).parent.parent / "main_live_banknifty_sentinel.py").read_text(encoding="utf-8")
    for key, expected in _shadow.BANKNIFTY_SENTINEL_OVERRIDES.items():
        m = re.search(rf"^config\.{key}\s*=\s*([0-9.]+)", source, re.M)
        assert m, f"{key} is no longer patched by main_live_banknifty_sentinel.py"
        assert float(m.group(1)) == float(expected), (
            f"{key}: live file says {m.group(1)}, BANKNIFTY_SENTINEL_OVERRIDES says {expected}")


def test_nifty_replay_calls_load_day_with_no_extra_kwargs(monkeypatch):
    """The default NIFTY path must keep calling load_day(day) exactly as it
    always has -- existing callers and stubs pass only the day."""
    import shadow as _shadow
    seen = {}

    def _stub(d):           # deliberately accepts ONLY the day
        seen["day"] = d
        return []

    monkeypatch.setattr(_shadow, "snapshot_recorder",
                        type("_", (), {"load_day": staticmethod(_stub)}))
    _shadow.run_policy("2026-08-27", Policy(name="nifty"))
    assert seen["day"] == "2026-08-27"


def test_banknifty_reconstructed_history_exists_in_dev():
    """Guards a wrong claim made on 2026-08-28: that Bank Nifty had no
    reconstructed history and every Bank Nifty conclusion was extrapolated
    from NIFTY. It has 1,244 days; the error was checking the PRODUCTION
    checkout, which keeps only what its own processes record.

    Skips rather than fails where the history genuinely isn't present --
    production is a legitimate place to run the suite, and this is a
    statement about the DEV research data, not about the code.
    """
    import snapshot_recorder as sr
    from pathlib import Path as _P
    bn = _P(__file__).parent.parent / "logs" / "snapshots_banknifty"
    if not bn.exists():
        pytest.skip("no Bank Nifty snapshot directory in this checkout")
    days = sr.available_days(snapshot_dir=bn)
    if len(days) < 100:
        pytest.skip(f"only {len(days)} Bank Nifty days here -- production checkout")
    first = next(sr.load_day(sorted(days)[0], snapshot_dir=bn, symbol="BANKNIFTY"), None)
    assert first is not None, "Bank Nifty history present but unreadable"
    assert first[0].source == "dhan_historical", (
        "expected reconstructed history, got " + str(first[0].source))
