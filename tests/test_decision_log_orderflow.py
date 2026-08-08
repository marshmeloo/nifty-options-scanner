"""
Pins decision_log.py's new book_imbalance/total_quantity_imbalance
fields: recorded on every candidate record, and -- critically -- never
influencing final_decision. Not a full test of _candidate_record's
existing decision logic (untested before this change); scoped to what
this change actually touches.

Run: python -m pytest tests/test_decision_log_orderflow.py -q
"""
import decision_log as dl
import trade_tracker as tt
import scanner
from models import Setup, RiskVerdict


def _setup(strike=24000.0, option_type="CE"):
    return Setup(symbol="NIFTY", strike=strike, option_type=option_type,
                expiry="2026-08-08", reasons=["test reason"], score=5.0)


def _verdict(decision="REJECTED"):
    return RiskVerdict(decision=decision, reasons=["too risky"], checks={})


def _quiet_mocks(monkeypatch):
    monkeypatch.setattr(tt, "apply_learned_adjustment", lambda score, reasons: (score, []))
    monkeypatch.setattr(tt, "expiry_day_rules", lambda expiry, now: (0.0, None))
    monkeypatch.setattr(tt, "is_repeat_of_stopped_plan", lambda *a, **kw: False)
    monkeypatch.setattr(scanner, "apply_bias_gate", lambda setup, label, score: (False, 0.0, None))


def test_candidate_record_includes_book_imbalance_when_available(monkeypatch):
    _quiet_mocks(monkeypatch)
    monkeypatch.setattr(dl.orderflow, "book_imbalance", lambda strike, opt_type: 0.42)
    monkeypatch.setattr(dl.orderflow, "total_quantity_imbalance", lambda strike, opt_type: -0.1)

    state = {"trades": [], "opened_today": 0}
    record = dl._candidate_record(_setup(), None, _verdict(), state, opened_trade=None)

    assert record["book_imbalance"] == 0.42
    assert record["total_quantity_imbalance"] == -0.1


def test_candidate_record_book_imbalance_none_when_feed_unavailable(monkeypatch):
    _quiet_mocks(monkeypatch)
    monkeypatch.setattr(dl.orderflow, "book_imbalance", lambda strike, opt_type: None)
    monkeypatch.setattr(dl.orderflow, "total_quantity_imbalance", lambda strike, opt_type: None)

    state = {"trades": [], "opened_today": 0}
    record = dl._candidate_record(_setup(), None, _verdict(), state, opened_trade=None)

    assert record["book_imbalance"] is None
    assert record["total_quantity_imbalance"] is None


def test_book_imbalance_never_affects_final_decision(monkeypatch):
    """The whole point: this is recorded data, not a gate. A strongly
    NEGATIVE (contradicting) imbalance must not change final_decision
    from what the existing risk/score logic already decided."""
    _quiet_mocks(monkeypatch)
    state = {"trades": [], "opened_today": 0}

    monkeypatch.setattr(dl.orderflow, "book_imbalance", lambda strike, opt_type: -0.95)
    monkeypatch.setattr(dl.orderflow, "total_quantity_imbalance", lambda strike, opt_type: -0.95)
    record_bad_imbalance = dl._candidate_record(_setup(), None, _verdict("APPROVED"), state, opened_trade=None)

    monkeypatch.setattr(dl.orderflow, "book_imbalance", lambda strike, opt_type: 0.95)
    monkeypatch.setattr(dl.orderflow, "total_quantity_imbalance", lambda strike, opt_type: 0.95)
    record_good_imbalance = dl._candidate_record(_setup(), None, _verdict("APPROVED"), state, opened_trade=None)

    assert record_bad_imbalance["final_decision"] == record_good_imbalance["final_decision"]
