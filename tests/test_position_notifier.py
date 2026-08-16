"""
position_notifier.py -- Telegram snapshot of open positions, added
2026-08-16. build_snapshot_message() is tested against hand-built state
dicts shaped like dashboard_server.build_state()'s real output (same
field names verified against dashboard_server.py's own enrichment
functions and dashboard/live_dashboard.html's condor/spread rendering).

Run: python -m pytest tests/test_position_notifier.py -q
"""
from datetime import datetime, time as dtime

import position_notifier as pn


def _empty_index():
    return {
        "open_trades": [], "condor_position": None, "directional_spread_position": None,
        "price_action_trades": [], "totals": {"open_pnl_inr": 0},
    }


def _flat_state():
    return {**_empty_index(), "banknifty": _empty_index()}


def _momentum_trade(strike=24300.0, option_type="CE", entry=145.2, ltp=162.8,
                    pnl_pct=12.1, pnl_inr=1320, r=0.8):
    return {"strike": strike, "option_type": option_type, "entry": entry, "current_ltp": ltp,
            "running_pnl_pct": pnl_pct, "running_pnl_inr": pnl_inr, "current_r": r}


def _condor_position(mtm=850):
    return {"status": "OPEN", "current_mtm_pnl_inr": mtm,
            "plan": {"short_ce_strike": 24300, "short_pe_strike": 24000,
                     "hedge_ce_strike": 24500, "hedge_pe_strike": 23800}}


def _spread_position(direction="PE", mtm=400):
    return {"status": "OPEN", "current_mtm_pnl_inr": mtm,
            "plan": {"direction": direction, "short_strike": 24000, "hedge_strike": 23900}}


# --- market_is_open --------------------------------------------------------

def test_market_is_open_during_regular_hours():
    monday_open = datetime(2026, 8, 17, 10, 0)  # a Monday
    assert pn.market_is_open(monday_open) is True


def test_market_is_open_false_on_weekend():
    saturday = datetime(2026, 8, 15, 10, 0)
    assert pn.market_is_open(saturday) is False


def test_market_is_open_false_before_open():
    before = datetime(2026, 8, 17, 9, 0)
    assert pn.market_is_open(before) is False


def test_market_is_open_true_past_nominal_close_within_cas_window():
    # 15:32 -- past the old 15:30 close, still inside this project's
    # CAS-aware 15:40 cutoff (see BACKLOG.md's CAS entry).
    just_after_nominal_close = datetime(2026, 8, 17, 15, 32)
    assert pn.market_is_open(just_after_nominal_close) is True


def test_market_is_open_false_after_extended_close():
    after = datetime(2026, 8, 17, 15, 45)
    assert pn.market_is_open(after) is False


# --- build_snapshot_message --------------------------------------------------

def test_flat_state_returns_none():
    assert pn.build_snapshot_message(_flat_state()) is None


def test_single_nifty_momentum_trade_is_included():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    state["totals"]["open_pnl_inr"] = 1320

    msg = pn.build_snapshot_message(state)

    assert msg is not None
    assert "NIFTY" in msg
    assert "24300CE" in msg
    assert "entry 145.2" in msg
    assert "162.8" in msg
    assert "+12.1%" in msg
    assert "R +0.80" in msg
    assert "Total open P&L: Rs +1,320" in msg


def test_bank_nifty_section_only_appears_when_it_has_something_open():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    msg = pn.build_snapshot_message(state)
    assert "Bank Nifty" not in msg


def test_both_indices_included_when_both_have_positions():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade(strike=24300.0)]
    state["banknifty"]["open_trades"] = [_momentum_trade(strike=51200.0, option_type="PE", pnl_inr=-870)]
    state["totals"]["open_pnl_inr"] = 1320
    state["banknifty"]["totals"]["open_pnl_inr"] = -870

    msg = pn.build_snapshot_message(state)

    assert "NIFTY" in msg and "Bank Nifty" in msg
    assert "24300CE" in msg
    assert "51200PE" in msg
    assert "Total open P&L: Rs +450" in msg


def test_condor_position_included_when_open():
    state = _flat_state()
    state["condor_position"] = _condor_position()
    msg = pn.build_snapshot_message(state)
    assert "Condor" in msg
    assert "24300CE/24000PE" in msg
    assert "24500CE/23800PE" in msg
    assert "Rs +850" in msg


def test_closed_condor_position_excluded():
    state = _flat_state()
    closed = _condor_position()
    closed["status"] = "CLOSED"
    state["condor_position"] = closed
    msg = pn.build_snapshot_message(state)
    assert msg is None


def test_directional_spread_bull_put_labeled_correctly():
    state = _flat_state()
    state["directional_spread_position"] = _spread_position(direction="PE")
    msg = pn.build_snapshot_message(state)
    assert "Bull put" in msg
    assert "24000/23900" in msg


def test_directional_spread_bear_call_labeled_correctly():
    state = _flat_state()
    state["directional_spread_position"] = _spread_position(direction="CE")
    msg = pn.build_snapshot_message(state)
    assert "Bear call" in msg


def test_price_action_trades_included_under_their_own_label():
    state = _flat_state()
    state["price_action_trades"] = [_momentum_trade(strike=24400.0)]
    msg = pn.build_snapshot_message(state)
    assert "Price action" in msg
    assert "24400CE" in msg


def test_mtm_unavailable_shown_when_mtm_is_none():
    state = _flat_state()
    position = _condor_position()
    position["current_mtm_pnl_inr"] = None
    state["condor_position"] = position
    msg = pn.build_snapshot_message(state)
    assert "MTM unavailable" in msg


def test_all_four_categories_together_for_one_index():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    state["condor_position"] = _condor_position()
    state["directional_spread_position"] = _spread_position()
    state["price_action_trades"] = [_momentum_trade(strike=24500.0)]

    msg = pn.build_snapshot_message(state)

    assert msg.count("NIFTY") >= 1
    assert "Momentum:" in msg
    assert "Condor:" in msg
    assert "Directional spread:" in msg
    assert "Price action:" in msg


def test_total_includes_condor_and_spread_mtm_not_just_momentum():
    """Regression: state['totals']['open_pnl_inr'] only ever covers
    momentum's own open_trades (confirmed by reading dashboard_server's
    build_state() -- condor/spread/price-action are never summed into
    it). Caught by testing against real dev data: a real open condor
    (-273) and spread (-124) with zero momentum trades open showed
    "Total open P&L: Rs +0" -- correct-looking but wrong. The total must
    come from what's actually displayed, not state['totals']."""
    state = _flat_state()
    state["condor_position"] = _condor_position(mtm=-273)
    state["directional_spread_position"] = _spread_position(mtm=-124)
    state["totals"]["open_pnl_inr"] = 0  # exactly what build_state() would report here

    msg = pn.build_snapshot_message(state)

    assert "Total open P&L: Rs -397" in msg


def test_total_sums_every_category_across_both_indices():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade(pnl_inr=1000)]
    state["condor_position"] = _condor_position(mtm=-200)
    state["price_action_trades"] = [_momentum_trade(strike=24500.0, pnl_inr=50)]
    state["banknifty"]["directional_spread_position"] = _spread_position(mtm=300)

    msg = pn.build_snapshot_message(state)

    assert "Total open P&L: Rs +1,150" in msg


# --- check_once --------------------------------------------------------------

def test_check_once_skips_outside_market_hours(monkeypatch):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: False)
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: sent.append(a))
    pn.check_once()
    assert sent == []


def test_check_once_skips_send_when_nothing_open(monkeypatch):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: _flat_state())
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: sent.append(a))
    pn.check_once()
    assert sent == []


def test_check_once_sends_when_something_is_open(monkeypatch):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: state)
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda text, **kw: sent.append(text))
    pn.check_once()
    assert len(sent) == 1
    assert "24300CE" in sent[0]


def test_check_once_does_not_raise_when_send_fails(monkeypatch):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: state)

    def _boom(*a, **kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(pn.telegram_notifier, "send_message", _boom)

    pn.check_once()  # must not raise


def test_check_once_logs_when_market_closed(monkeypatch, caplog):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: False)
    with caplog.at_level("INFO", logger="position_notifier"):
        pn.check_once()
    assert "market closed" in caplog.text


# --- lifecycle (start/stop) messages ------------------------------------------

def test_lifecycle_message_sends_and_does_not_raise_on_success(monkeypatch):
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda text, **kw: sent.append(text))
    pn._send_lifecycle_message("hello")
    assert sent == ["hello"]


def test_lifecycle_message_does_not_raise_on_failure(monkeypatch, caplog):
    def _boom(*a, **kw):
        raise ConnectionError("bad token")
    monkeypatch.setattr(pn.telegram_notifier, "send_message", _boom)
    with caplog.at_level("INFO", logger="position_notifier"):
        pn._send_lifecycle_message("hello")  # must not raise
    assert "FAILED" in caplog.text


def test_run_forever_sends_a_start_message_immediately_even_if_market_closed(monkeypatch):
    lifecycle_calls = []
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: lifecycle_calls.append(text))
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: False)  # market closed
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)

    assert len(lifecycle_calls) >= 1
    assert "started" in lifecycle_calls[0]


def test_run_forever_sends_a_stop_message_on_keyboard_interrupt(monkeypatch):
    lifecycle_calls = []
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: lifecycle_calls.append(text))
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)  # must not raise -- KeyboardInterrupt is caught

    assert len(lifecycle_calls) == 2
    assert "started" in lifecycle_calls[0]
    assert "stopped" in lifecycle_calls[1]


def test_run_forever_returns_cleanly_on_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt must be swallowed after the stop message, not
    propagated -- a Ctrl+C exit should look clean, not dump a traceback."""
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: None)
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)  # must return, not raise
