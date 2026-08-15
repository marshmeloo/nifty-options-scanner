"""
Tests for dashboard_server.py's error handling.

Reported live 2026-08-04: a full traceback printed to the terminal every
time a browser tab closed or refreshed mid-poll (ConnectionAbortedError /
WinError 10053). That's normal traffic for a page polling every few
seconds, not a server fault -- the client just isn't there to receive
the bytes anymore.

Run: python -m pytest tests/ -q
"""

import json
import sys

import pytest

import dashboard_server as ds


def _raise_and_capture_handle_error(exc, monkeypatch):
    server = ds.DashboardServer.__new__(ds.DashboardServer)  # skip __init__, no real socket needed
    called_super = []
    monkeypatch.setattr(ds.socketserver.TCPServer, "handle_error",
                        lambda self, request, addr: called_super.append(True))
    try:
        raise exc
    except type(exc):
        server.handle_error(request=None, client_address=("127.0.0.1", 1))
    return called_super


@pytest.mark.parametrize("exc", [ConnectionAbortedError(), ConnectionResetError(), BrokenPipeError()])
def test_client_disconnect_exceptions_are_swallowed_quietly(exc, monkeypatch):
    called_super = _raise_and_capture_handle_error(exc, monkeypatch)
    assert called_super == [], "a client-abort exception must not fall through to the default traceback printer"


def test_other_exceptions_still_print_the_full_traceback(monkeypatch):
    """
    Swallowing every exception here would hide a real bug in
    build_state() or the request handler behind "oh, it's probably just
    a closed tab" -- only the specific disconnect exceptions are quiet.
    """
    called_super = _raise_and_capture_handle_error(ValueError("a real bug"), monkeypatch)
    assert called_super == [True]


# --------------------------------------------------------------------------
# P&L dashboard: _load_all_pnl_trades() normalises across every
# strategy/index journal, which each write a slightly different shape.
# --------------------------------------------------------------------------

def _write_journal(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_open_trades_are_excluded(tmp_path, monkeypatch):
    """A trade with no closed_at is still open -- this dashboard is
    realised trades only, same convention as a broker's own P&L report."""
    j = tmp_path / "trade_journal.jsonl"
    _write_journal(j, [{"opened_at": "2026-08-01T09:20:00", "pnl_inr": 100}])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "NIFTY", "Momentum")])

    assert ds._load_all_pnl_trades() == []


def test_missing_journal_files_are_skipped_not_errored(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(tmp_path / "does_not_exist.jsonl", "NIFTY", "Momentum")])
    assert ds._load_all_pnl_trades() == []


def test_corrupt_line_does_not_blow_up_the_whole_file(tmp_path, monkeypatch):
    j = tmp_path / "trade_journal.jsonl"
    j.write_text(
        '{"closed_at": "2026-08-01T10:00:00", "pnl_inr": 500}\n'
        'not valid json\n'
        '{"closed_at": "2026-08-02T10:00:00", "pnl_inr": -200}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "NIFTY", "Momentum")])

    trades = ds._load_all_pnl_trades()
    assert len(trades) == 2


def test_charges_modelled_flag_reflects_whether_costs_inr_is_present(tmp_path, monkeypatch):
    """
    Momentum and price-action journal costs_inr; condor and
    directional-spread don't yet. The dashboard must be able to tell
    "zero charges" apart from "not measured", or it silently implies
    condor trades are free to run.
    """
    j = tmp_path / "trade_journal.jsonl"
    _write_journal(j, [
        {"closed_at": "2026-08-01T10:00:00", "pnl_inr": 500, "costs_inr": 40, "pnl_inr_net": 460},
        {"closed_at": "2026-08-02T10:00:00", "pnl_inr": -200},   # no cost model for this strategy
    ])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "NIFTY", "Momentum")])

    trades = {t["closed_at"]: t for t in ds._load_all_pnl_trades()}
    with_costs = trades["2026-08-01T10:00:00"]
    without_costs = trades["2026-08-02T10:00:00"]

    assert with_costs["charges_modelled"] is True
    assert with_costs["charges_inr"] == 40
    assert with_costs["net_pnl_inr"] == 460

    assert without_costs["charges_modelled"] is False
    assert without_costs["charges_inr"] == 0.0
    assert without_costs["net_pnl_inr"] == without_costs["realised_pnl_inr"] == -200


def test_date_is_derived_from_closed_at_not_opened_at(tmp_path, monkeypatch):
    """A trade opened one day and settled the next (e.g. an interrupted-
    session recovery) must land on the day it actually closed, not the
    day it opened -- otherwise a day's P&L wouldn't match what actually
    happened that day."""
    j = tmp_path / "trade_journal.jsonl"
    _write_journal(j, [
        {"opened_at": "2026-08-01T23:50:00", "closed_at": "2026-08-02T00:05:00", "pnl_inr": 100},
    ])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "NIFTY", "Momentum")])

    assert ds._load_all_pnl_trades()[0]["date"] == "2026-08-02"


def test_index_and_strategy_labels_are_tagged_per_journal(tmp_path, monkeypatch):
    nifty = tmp_path / "trade_journal.jsonl"
    bn = tmp_path / "trade_journal_banknifty.jsonl"
    _write_journal(nifty, [{"closed_at": "2026-08-01T10:00:00", "pnl_inr": 100}])
    _write_journal(bn, [{"closed_at": "2026-08-01T10:00:00", "pnl_inr": -50}])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [
        (nifty, "NIFTY", "Momentum"),
        (bn, "Bank Nifty", "Momentum"),
    ])

    trades = ds._load_all_pnl_trades()
    tagged = {(t["index"], t["realised_pnl_inr"]) for t in trades}
    assert tagged == {("NIFTY", 100), ("Bank Nifty", -50)}


def test_entry_with_no_pnl_at_all_is_skipped(tmp_path, monkeypatch):
    """closed_at present but pnl_inr missing/null -- nothing to plot,
    must not crash the aggregation with a None."""
    j = tmp_path / "trade_journal.jsonl"
    _write_journal(j, [{"closed_at": "2026-08-01T10:00:00", "pnl_inr": None}])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "NIFTY", "Momentum")])

    assert ds._load_all_pnl_trades() == []


# --------------------------------------------------------------------------
# Peak favorable excursion: added 2026-08-15 after a real Bank Nifty
# session closed six trades net negative that had each run to ~0.75R in
# profit first -- the closed P&L alone gave no way to see that a trade
# gave back real ground rather than never having been ahead. Three
# journal shapes store this three different ways.
# --------------------------------------------------------------------------

def test_peak_favorable_prefers_momentums_own_field():
    t = {"max_favorable_inr": 1059.0, "max_pnl_inr_seen": 999, "max_ltp_seen": 999, "entry": 1, "lots": 1}
    assert ds._peak_favorable_inr(t, "Bank Nifty") == 1059.0


def test_peak_favorable_falls_back_to_condor_field():
    t = {"max_pnl_inr_seen": 1079.0}
    assert ds._peak_favorable_inr(t, "NIFTY") == 1079.0


def test_peak_favorable_derived_for_price_action_from_max_ltp_seen():
    """price_action_journal.jsonl has no rupee figure at all -- only
    max_ltp_seen, entry, and lots to derive one from."""
    t = {"max_ltp_seen": 96.6, "entry": 59.1, "lots": 1}
    assert ds._peak_favorable_inr(t, "NIFTY") == pytest.approx(round((96.6 - 59.1) * 65, 2))


def test_peak_favorable_derivation_uses_the_right_lot_size_per_index():
    t = {"max_ltp_seen": 110.0, "entry": 100.0, "lots": 1}
    assert ds._peak_favorable_inr(t, "NIFTY") == pytest.approx(10 * 65)
    assert ds._peak_favorable_inr(t, "Bank Nifty") == pytest.approx(10 * 30)


def test_peak_favorable_is_none_not_zero_when_unavailable():
    """A legacy entry from before excursion tracking existed must not
    silently read as 'this trade never moved' -- that's a materially
    different, false claim from 'we don't know'."""
    assert ds._peak_favorable_inr({}, "NIFTY") is None


def test_loaded_trades_carry_peak_fields(tmp_path, monkeypatch):
    j = tmp_path / "trade_journal_banknifty.jsonl"
    _write_journal(j, [{
        "closed_at": "2026-08-14T13:59:11", "pnl_inr": -1531.17,
        "max_favorable_inr": 1059.0, "max_r_seen": 0.75,
    }])
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(j, "Bank Nifty", "Momentum")])

    t = ds._load_all_pnl_trades()[0]
    assert t["peak_favorable_inr"] == 1059.0
    assert t["peak_r"] == 0.75
