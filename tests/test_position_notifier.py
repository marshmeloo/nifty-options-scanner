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
    assert "Total open P&L: ₹+1,320" in msg


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
    assert "Total open P&L: ₹+450" in msg


def test_condor_position_included_when_open():
    state = _flat_state()
    state["condor_position"] = _condor_position()
    msg = pn.build_snapshot_message(state)
    assert "Condor" in msg
    assert "24300CE/24000PE" in msg
    assert "24500CE/23800PE" in msg
    assert "₹+850" in msg


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

    assert "Total open P&L: ₹-397" in msg


def test_total_sums_every_category_across_both_indices():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade(pnl_inr=1000)]
    state["condor_position"] = _condor_position(mtm=-200)
    state["price_action_trades"] = [_momentum_trade(strike=24500.0, pnl_inr=50)]
    state["banknifty"]["directional_spread_position"] = _spread_position(mtm=300)

    msg = pn.build_snapshot_message(state)

    assert "Total open P&L: ₹+1,150" in msg


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
    monkeypatch.setattr(pn, "_send_premarket_summary", lambda: None)
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: False)  # market closed
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)

    assert len(lifecycle_calls) >= 1
    assert "started" in lifecycle_calls[0]


def test_run_forever_sends_a_stop_message_on_keyboard_interrupt(monkeypatch):
    lifecycle_calls = []
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: lifecycle_calls.append(text))
    monkeypatch.setattr(pn, "_send_premarket_summary", lambda: None)
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)  # must not raise -- KeyboardInterrupt is caught

    assert len(lifecycle_calls) == 2
    assert "started" in lifecycle_calls[0]
    assert "stopped" in lifecycle_calls[1]


def test_run_forever_returns_cleanly_on_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt must be swallowed after the stop message, not
    propagated -- a Ctrl+C exit should look clean, not dump a traceback."""
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: None)
    monkeypatch.setattr(pn, "_send_premarket_summary", lambda: None)
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)  # must return, not raise


def test_run_forever_sends_premarket_summary_after_start_message(monkeypatch):
    order = []
    monkeypatch.setattr(pn, "_send_lifecycle_message", lambda text: order.append(("lifecycle", text)))
    monkeypatch.setattr(pn, "_send_premarket_summary", lambda: order.append(("premarket",)))
    monkeypatch.setattr(pn, "check_once", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    pn.run_forever(interval_seconds=1)

    assert order[0] == ("lifecycle", order[0][1]) and "started" in order[0][1]
    assert order[1] == ("premarket",)


# --- icon helpers --------------------------------------------------------------

def test_pnl_icon_positive_negative_zero_and_none():
    assert pn._pnl_icon(100) == "\U0001f7e2"
    assert pn._pnl_icon(-100) == "\U0001f534"
    assert pn._pnl_icon(0) == "⚪"
    assert pn._pnl_icon(None) == "⚪"


def test_direction_icon_positive_negative_zero_and_none():
    assert pn._direction_icon(100) == "\U0001f4c8"
    assert pn._direction_icon(-100) == "\U0001f4c9"
    assert pn._direction_icon(0) == "➖"
    assert pn._direction_icon(None) == "➖"


def test_bias_icon_matches_on_substring_not_exact_string():
    assert pn._bias_icon("bullish") == "\U0001f4c8"
    assert pn._bias_icon("leaning bullish (2/3 signals)") == "\U0001f4c8"
    assert pn._bias_icon("bearish") == "\U0001f4c9"
    assert pn._bias_icon("leaning bearish (3/4 signals) -- caveat: ...") == "\U0001f4c9"
    assert pn._bias_icon("neutral/range") == "➖"
    assert pn._bias_icon("mixed / neutral") == "➖"
    assert pn._bias_icon(None) == "➖"
    assert pn._bias_icon("") == "➖"


# --- premarket summary -----------------------------------------------------------

def _full_premarket_brief():
    return {
        "bias": "leaning bullish (2/3 signals)",
        "expiry": {"expiry": "2026-08-18", "days_to_expiry": 4, "is_expiry_day": False},
        "previous_session": {"close": 24395.85, "close_position_pct": 70.3},
        "banknifty": {"divergence": {"read": "confirming", "detail": "..."}},
        "smart_money": {"lean": "bearish"},
        "chain_context": {"pcr": 0.83, "max_pain_strike": 24400.0},
        "event_today": None,
        "news": {"risk": {"level": "normal", "categories_hit": []}},
    }


def test_premarket_message_none_when_brief_empty():
    assert pn.build_premarket_message({}) is None
    assert pn.build_premarket_message(None) is None


def test_premarket_message_includes_bias_and_expiry():
    msg = pn.build_premarket_message(_full_premarket_brief())
    assert "leaning bullish (2/3 signals)" in msg
    assert "\U0001f4c8" in msg  # bullish icon
    assert "2026-08-18" in msg
    assert "4 day(s) away" in msg


def test_premarket_message_flags_expiry_day():
    brief = _full_premarket_brief()
    brief["expiry"] = {"expiry": "2026-08-16", "days_to_expiry": 0, "is_expiry_day": True}
    msg = pn.build_premarket_message(brief)
    assert "TODAY IS EXPIRY DAY" in msg


def test_premarket_message_shows_no_flags_when_nothing_flagged():
    msg = pn.build_premarket_message(_full_premarket_brief())
    assert "No event/news risk flagged today" in msg
    assert "⚠️" not in msg


def test_premarket_message_flags_event_and_elevated_news():
    brief = _full_premarket_brief()
    brief["event_today"] = "RBI Policy"
    brief["news"] = {"risk": {"level": "elevated", "categories_hit": ["rbi_policy"]}}
    msg = pn.build_premarket_message(brief)
    assert "Event flagged: RBI Policy" in msg
    assert "News risk: elevated (rbi_policy)" in msg
    assert "No event/news risk flagged today" not in msg


def test_premarket_message_omits_expiry_section_on_error():
    brief = _full_premarket_brief()
    brief["expiry"] = {"error": "network down"}
    msg = pn.build_premarket_message(brief)
    assert "Expiry:" not in msg


def test_premarket_message_omits_smart_money_when_unavailable():
    brief = _full_premarket_brief()
    brief["smart_money"] = {"lean": "unavailable", "detail": "no report"}
    msg = pn.build_premarket_message(brief)
    assert "Smart money" not in msg


def test_send_premarket_summary_skips_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pn.premarket, "brief_json_path", lambda: tmp_path / "absent.json")
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: sent.append(a))
    pn._send_premarket_summary()  # must not raise
    assert sent == []


def test_send_premarket_summary_sends_when_file_present(tmp_path, monkeypatch):
    import json as jsonlib
    path = tmp_path / "brief.json"
    path.write_text(jsonlib.dumps(_full_premarket_brief()))
    monkeypatch.setattr(pn.premarket, "brief_json_path", lambda: path)
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda text, **kw: sent.append(text))
    pn._send_premarket_summary()
    assert len(sent) == 1
    assert "leaning bullish" in sent[0]


def test_send_premarket_summary_does_not_raise_on_malformed_json(tmp_path, monkeypatch):
    path = tmp_path / "brief.json"
    path.write_text("not valid json{{{")
    monkeypatch.setattr(pn.premarket, "brief_json_path", lambda: path)
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: sent.append(a))
    pn._send_premarket_summary()  # must not raise
    assert sent == []


# --- bias-shift alerts -----------------------------------------------------------

def test_bias_shift_first_sighting_records_but_does_not_alert():
    """No prior memory of this index yet (fresh install / just restarted)
    must not be treated as a "shift from nothing" -- that would false-alarm
    on every single restart."""
    state = {}
    bias = {"label": "bullish", "score": 1.5, "reasons": ["Trend: uptrend"]}
    result = pn._check_bias_shift("NIFTY", "NIFTY", bias, state)
    assert result is None
    assert state["NIFTY"]["label"] == "bullish"


def test_bias_shift_same_label_no_alert():
    state = {"NIFTY": {"label": "bullish", "since": "10:00"}}
    bias = {"label": "bullish", "score": 1.2, "reasons": []}
    result = pn._check_bias_shift("NIFTY", "NIFTY", bias, state)
    assert result is None


def test_bias_shift_real_change_produces_a_message():
    state = {"NIFTY": {"label": "neutral/range", "since": "10:15"}}
    bias = {"label": "bullish", "score": 1.5, "reasons": ["Trend: uptrend (higher highs)", "ROC +0.62%"]}
    result = pn._check_bias_shift("NIFTY", "NIFTY", bias, state)
    assert result is not None
    assert "NIFTY" in result
    assert "neutral/range" in result and "bullish" in result
    assert "score +1.50" in result
    assert "Trend: uptrend (higher highs)" in result
    assert "since 10:15" in result
    assert state["NIFTY"]["label"] == "bullish"


def test_bias_shift_missing_label_is_a_no_op():
    state = {"NIFTY": {"label": "bullish", "since": "10:00"}}
    result = pn._check_bias_shift("NIFTY", "NIFTY", {}, state)
    assert result is None
    assert state["NIFTY"]["label"] == "bullish"   # unchanged


def test_check_and_alert_bias_shifts_covers_both_indices_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(pn, "BIAS_STATE_PATH", tmp_path / "bias.json")
    state = {
        "latest_cycle": {"market_bias": {"label": "bullish", "score": 1.5, "reasons": []}},
        "banknifty": {"latest_cycle": {"market_bias": {"label": "bearish", "score": -1.2, "reasons": []}}},
    }
    # seed prior state so both count as real shifts
    pn.atomic_write_json(pn.BIAS_STATE_PATH, {
        "NIFTY": {"label": "neutral/range", "since": "10:00"},
        "BANKNIFTY": {"label": "neutral/range", "since": "10:00"},
    })

    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda text, **kw: sent.append(text))

    pn._check_and_alert_bias_shifts(state)

    assert len(sent) == 2
    assert any("NIFTY" in m and "Bank Nifty" not in m for m in sent)
    assert any("Bank Nifty" in m for m in sent)

    saved = pn._load_bias_state()
    assert saved["NIFTY"]["label"] == "bullish"
    assert saved["BANKNIFTY"]["label"] == "bearish"


def test_check_and_alert_bias_shifts_persists_state_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(pn, "BIAS_STATE_PATH", tmp_path / "bias.json")
    state = {"latest_cycle": {"market_bias": {"label": "bullish", "score": 1.0, "reasons": []}}}
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: None)

    pn._check_and_alert_bias_shifts(state)

    assert pn.BIAS_STATE_PATH.exists()
    assert pn._load_bias_state()["NIFTY"]["label"] == "bullish"


def test_check_and_alert_bias_shifts_does_not_raise_when_send_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(pn, "BIAS_STATE_PATH", tmp_path / "bias.json")
    pn.atomic_write_json(pn.BIAS_STATE_PATH, {"NIFTY": {"label": "neutral/range", "since": "10:00"}})
    state = {"latest_cycle": {"market_bias": {"label": "bullish", "score": 1.0, "reasons": []}}}

    def _boom(*a, **kw):
        raise ConnectionError("network down")
    monkeypatch.setattr(pn.telegram_notifier, "send_message", _boom)

    pn._check_and_alert_bias_shifts(state)  # must not raise


def test_check_once_also_checks_bias_shifts(monkeypatch, tmp_path):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: _flat_state())
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(pn, "_check_and_alert_bias_shifts", lambda state: calls.append(state))

    pn.check_once()

    assert len(calls) == 1


# --- radar (top-scored candidate) -------------------------------------------------

def _candidate(strike=24400.0, option_type="CE", raw_score=4.8, adjusted_score=4.2,
              conviction_bar=5.0, final_decision="NOT_SELECTED"):
    return {"strike": strike, "option_type": option_type, "raw_score": raw_score,
            "adjusted_score": adjusted_score, "conviction_bar": conviction_bar,
            "final_decision": final_decision}


def test_radar_line_none_when_no_candidates():
    assert pn._radar_line("NIFTY", []) is None


def test_radar_line_uses_the_first_candidate_only():
    candidates = [_candidate(strike=24400.0), _candidate(strike=24000.0, raw_score=3.0)]
    line = pn._radar_line("NIFTY", candidates)
    assert "24400CE" in line
    assert "24000" not in line


def test_radar_line_shows_raw_and_adjusted_when_they_differ():
    line = pn._radar_line("NIFTY", [_candidate(raw_score=4.8, adjusted_score=4.2)])
    assert "4.8 → 4.2" in line


def test_radar_line_shows_just_one_score_when_unadjusted():
    line = pn._radar_line("NIFTY", [_candidate(raw_score=5.0, adjusted_score=5.0)])
    assert "5.0 → 5.0" not in line
    assert "score 5.0" in line


def test_radar_line_includes_bar_and_decision():
    line = pn._radar_line("NIFTY", [_candidate(conviction_bar=5.0, final_decision="REJECTED_RISK")])
    assert "bar 5.0" in line
    assert "REJECTED_RISK" in line


def test_radar_section_empty_when_neither_index_has_candidates():
    assert pn._radar_section({}) == []


def test_radar_section_includes_both_indices_independently():
    state = {
        "latest_cycle": {"candidates": [_candidate(strike=24400.0)]},
        "banknifty": {"latest_cycle": {"candidates": [_candidate(strike=51300.0, option_type="PE")]}},
    }
    lines = pn._radar_section(state)
    assert "On the radar" in lines[0]
    joined = "\n".join(lines)
    assert "NIFTY: 24400CE" in joined
    assert "Bank Nifty: 51300PE" in joined


def test_radar_section_only_nifty_when_banknifty_has_no_candidates():
    state = {"latest_cycle": {"candidates": [_candidate()]}}
    lines = pn._radar_section(state)
    joined = "\n".join(lines)
    assert "NIFTY:" in joined
    assert "Bank Nifty:" not in joined


# --- build_full_message ------------------------------------------------------------

def test_full_message_none_when_flat_and_no_candidates():
    assert pn.build_full_message(_flat_state()) is None


def test_full_message_sends_with_only_radar_when_flat(monkeypatch):
    """The behavior change from build_snapshot_message() alone: nothing
    open, but a candidate exists this cycle -> still sends."""
    state = _flat_state()
    state["latest_cycle"] = {"candidates": [_candidate()]}
    msg = pn.build_full_message(state)
    assert msg is not None
    assert "On the radar" in msg
    assert "no open positions right now" in msg


def test_full_message_includes_both_positions_and_radar(monkeypatch):
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    state["latest_cycle"] = {"candidates": [_candidate()]}
    msg = pn.build_full_message(state)
    assert "24300CE" in msg          # the open position
    assert "On the radar" in msg
    assert "no open positions right now" not in msg
    assert "Total open P&L" in msg


def test_full_message_positions_only_when_no_candidates_this_cycle():
    state = _flat_state()
    state["open_trades"] = [_momentum_trade()]
    msg = pn.build_full_message(state)
    assert "On the radar" not in msg
    assert "Total open P&L" in msg


def test_check_once_sends_even_when_flat_if_a_candidate_exists(monkeypatch):
    """The actual behavior change, exercised through check_once() rather
    than build_full_message() directly."""
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    state = _flat_state()
    state["latest_cycle"] = {"candidates": [_candidate()]}
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: state)
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda text, **kw: sent.append(text))

    pn.check_once()

    assert len(sent) == 1
    assert "On the radar" in sent[0]


def test_check_once_still_silent_when_flat_and_no_candidates(monkeypatch):
    monkeypatch.setattr(pn, "market_is_open", lambda now=None: True)
    monkeypatch.setattr(pn.dashboard_server, "build_state", lambda: _flat_state())
    sent = []
    monkeypatch.setattr(pn.telegram_notifier, "send_message", lambda *a, **kw: sent.append(a))

    pn.check_once()

    assert sent == []
