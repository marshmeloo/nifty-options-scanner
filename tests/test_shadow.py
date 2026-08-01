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
