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


# --------------------------------------------------------------------------
# _contract_label: multi-leg strategies had no readable identifier in /pnl
# (added 2026-08-17 -- spread/condor rows rendered a blank "-" contract)
# --------------------------------------------------------------------------

def test_contract_label_single_leg_uses_strike_and_type():
    t = {"strike": 24200.0, "option_type": "PE"}
    assert ds._contract_label(t) == "24200 PE"


def test_contract_label_single_leg_trims_trailing_space_without_type():
    assert ds._contract_label({"strike": 24200.0}) == "24200"


def test_contract_label_directional_spread_bull_put():
    t = {"plan": {"direction": "PE", "short_strike": 24600.0, "hedge_strike": 24500.0}}
    assert ds._contract_label(t) == "Bull put 24600/24500"


def test_contract_label_directional_spread_bear_call():
    t = {"plan": {"direction": "CE", "short_strike": 24800.0, "hedge_strike": 24900.0}}
    assert ds._contract_label(t) == "Bear call 24800/24900"


def test_contract_label_condor_names_both_shorts_and_hedges():
    t = {"plan": {"short_ce_strike": 24500.0, "short_pe_strike": 23850.0,
                  "hedge_ce_strike": 24800.0, "hedge_pe_strike": 23550.0}}
    label = ds._contract_label(t)
    assert "Condor" in label
    assert "24500CE" in label and "23850PE" in label
    assert "24800" in label and "23550" in label


def test_contract_label_falls_back_when_nothing_identifiable():
    assert ds._contract_label({}) == "—"
    assert ds._contract_label({"plan": {}}) == "—"


def test_contract_label_prefers_single_leg_fields_when_both_present():
    """A single-leg journal entry that happens to carry a plan must still
    report its own strike, not a spread description."""
    t = {"strike": 24200.0, "option_type": "CE", "plan": {"short_strike": 999.0}}
    assert ds._contract_label(t) == "24200 CE"


def test_pnl_loader_falls_back_to_plan_lots_for_multi_leg(tmp_path, monkeypatch):
    """Spread/condor entries keep `lots` inside `plan`, not at top level --
    the table showed "-" for every one of them."""
    journal = tmp_path / "directional_spread_journal.jsonl"
    journal.write_text(json.dumps({
        "closed_at": "2026-08-17T14:00:00", "pnl_inr": 1196.0, "status": "CLOSED",
        "plan": {"direction": "PE", "short_strike": 24600.0, "hedge_strike": 24500.0, "lots": 3},
    }) + "\n")
    monkeypatch.setattr(ds, "PNL_JOURNALS", [(journal, "NIFTY", "Directional Spread")])

    trades = ds._load_all_pnl_trades()

    assert len(trades) == 1
    assert trades[0]["lots"] == 3
    assert trades[0]["contract"] == "Bull put 24600/24500"


# --------------------------------------------------------------------------
# _sentinel_version: the dashboard displayed a hardcoded "Sentinel v1.1-dev"
# heading for BOTH indices and kept showing it after Sentinel was promoted to
# v1.2-dev on 2026-08-27 -- the same stale-literal bug that made the live log
# banner misreport its version. Anything naming a version must derive it.
# --------------------------------------------------------------------------

def test_sentinel_version_reads_the_journals_latest_entry(tmp_path):
    import dashboard_server as ds
    j = tmp_path / "trade_journal_sentinel.jsonl"
    j.write_text(
        json.dumps({"strategy_version": "1.1-dev", "strike": 1}) + "\n"
        + json.dumps({"strategy_version": "1.2-dev", "strike": 2}) + "\n",
        encoding="utf-8")
    assert ds._sentinel_version(j) == "1.2-dev"


def test_sentinel_version_is_none_when_journal_missing(tmp_path):
    """Better a bare 'Sentinel' than an invented version number."""
    import dashboard_server as ds
    assert ds._sentinel_version(tmp_path / "nope.jsonl") is None


def test_sentinel_version_skips_unparseable_and_versionless_lines(tmp_path):
    import dashboard_server as ds
    j = tmp_path / "j.jsonl"
    j.write_text(
        json.dumps({"strategy_version": "1.2-dev"}) + "\n"
        + json.dumps({"no_version_here": True}) + "\n"
        + "{ this is not json\n",
        encoding="utf-8")
    assert ds._sentinel_version(j) == "1.2-dev"


def test_sentinel_version_never_falls_back_to_anchors_config(tmp_path):
    """The dashboard imports the SHARED config unpatched, so
    config.STRATEGY_VERSION is ANCHOR's here. Reading it would silently
    label Sentinel's panel with Anchor's version."""
    import dashboard_server as ds
    import config
    assert ds._sentinel_version(tmp_path / "absent.jsonl") != config.STRATEGY_VERSION


def test_sentinel_block_exposes_the_version(tmp_path):
    import dashboard_server as ds
    j = tmp_path / "j.jsonl"
    j.write_text(json.dumps({"strategy_version": "1.2-dev"}) + "\n", encoding="utf-8")
    block = ds._sentinel_block(tmp_path / "state.json", j, 30)
    assert block["strategy_version"] == "1.2-dev"


def test_dashboard_html_has_no_hardcoded_sentinel_version():
    """Guards the regression directly: no literal version in the heading."""
    from pathlib import Path as _P
    html = (_P(__file__).parent.parent / "dashboard" / "live_dashboard.html").read_text(encoding="utf-8")
    # Only the LIVE Sentinel panels. The "One Trade/Day v0.1-research"
    # headings also mention Sentinel, but that is a derived research view,
    # not a live process whose version drifts -- see _derive_onetrade_block.
    headings = [l for l in html.splitlines()
                if "<h2>" in l and "candidate strategy, its own live process" in l]
    assert len(headings) == 2, f"expected 2 live Sentinel panels, found {len(headings)}"
    for h in headings:
        assert "v1.1-dev" not in h and "v1.2-dev" not in h, f"hardcoded version in: {h.strip()}"
        assert "sentinel-version-label" in h, f"heading not wired to the derived label: {h.strip()}"


# --------------------------------------------------------------------------
# _capital_committed: single-leg vs credit-spread shapes.
#
# Credit spreads and condors COLLECT premium and post margin -- there is no
# "entry" price to multiply, so entry x lots is meaningless for them, not
# merely imprecise. They showed no capital at all until 2026-09-01, which
# left August's ONLY profitable strategy (Directional Spread: Rs 4,579 on
# Rs 34,089 of committed risk) with no denominator on the dashboard.
# --------------------------------------------------------------------------

def test_capital_committed_single_leg_uses_premium_paid():
    import dashboard_server as ds
    assert ds._capital_committed({"entry": 100.0, "lots": 1}, 65) == 6500.0
    assert ds._capital_committed({"entry": 100.0, "lots": 2}, 30) == 6000.0


def test_capital_committed_defaults_to_one_lot():
    import dashboard_server as ds
    assert ds._capital_committed({"entry": 50.0}, 65) == 3250.0


def test_capital_committed_credit_spread_uses_max_loss():
    """A real August directional-spread plan: 24600/24500, max loss 4108."""
    import dashboard_server as ds
    t = {"plan": {"short_strike": 24600.0, "hedge_strike": 24500.0,
                  "net_credit_inr": 2392.0, "max_loss_inr": 4108.0, "lots": 1}}
    assert ds._capital_committed(t, 65) == 4108.0


def test_capital_committed_never_multiplies_a_spread_by_lot_size():
    """The bug this guards: treating max_loss as a per-unit price."""
    import dashboard_server as ds
    t = {"plan": {"max_loss_inr": 4108.0}}
    assert ds._capital_committed(t, 65) == 4108.0
    assert ds._capital_committed(t, 30) == 4108.0   # lot size must not matter


def test_capital_committed_unknown_shape_is_none_not_zero():
    """None renders as a dash; 0 would read as 'no capital used', a lie."""
    import dashboard_server as ds
    assert ds._capital_committed({"something_else": 1}, 65) is None


def test_pnl_rows_carry_capital_for_both_shapes(tmp_path, monkeypatch):
    import dashboard_server as ds
    single = tmp_path / "single.jsonl"
    single.write_text(json.dumps({
        "opened_at": "2026-08-03T10:00:00", "closed_at": "2026-08-03T11:00:00",
        "entry": 100.0, "lots": 1, "pnl_inr": 500, "strike": 24000, "option_type": "CE"}) + "\n",
        encoding="utf-8")
    spread = tmp_path / "spread.jsonl"
    spread.write_text(json.dumps({
        "opened_at": "2026-08-03T11:56:27", "closed_at": "2026-08-03T15:00:00",
        "pnl_inr": -2122, "plan": {"short_strike": 24600.0, "hedge_strike": 24500.0,
                                   "direction": "PE", "max_loss_inr": 4108.0, "lots": 1}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(ds, "PNL_JOURNALS", [
        (single, "NIFTY", "Momentum (Anchor)"),
        (spread, "NIFTY", "Directional Spread")])
    rows = {r["strategy"]: r for r in ds._load_all_pnl_trades()}
    assert rows["Momentum (Anchor)"]["capital_deployed"] == 6500.0
    assert rows["Directional Spread"]["capital_deployed"] == 4108.0


# --------------------------------------------------------------------------
# _peak_capital: capital committed AT ONCE, never the sum.
#
# Summing every trade's commitment invents a base that never existed and
# flatters risk. August's 211 trades summed to Rs 20,96,762 against a
# Rs 5,00,000 book; the real peak was Rs 4,30,860 on 2026-08-27 -- 86% of
# the allocation, against a MAX_TOTAL_EXPOSURE_PCT of 20% that does not
# measure deployment at all.
# --------------------------------------------------------------------------

def test_peak_capital_is_concurrency_not_sum():
    """Two trades that never overlap: peak is the larger, not the total."""
    import dashboard_server as ds
    closed = [
        {"capital_deployed": 1000, "opened_at": "2026-08-31T09:20", "closed_at": "2026-08-31T10:00"},
        {"capital_deployed": 3000, "opened_at": "2026-08-31T10:30", "closed_at": "2026-08-31T11:00"},
    ]
    assert ds._peak_capital([], closed) == 3000


def test_peak_capital_adds_overlapping_positions():
    import dashboard_server as ds
    closed = [
        {"capital_deployed": 1000, "opened_at": "2026-08-31T09:20", "closed_at": "2026-08-31T11:00"},
        {"capital_deployed": 3000, "opened_at": "2026-08-31T10:00", "closed_at": "2026-08-31T10:30"},
    ]
    assert ds._peak_capital([], closed) == 4000


def test_peak_capital_counts_still_open_positions():
    """An open position is capital committed RIGHT NOW."""
    import dashboard_server as ds
    assert ds._peak_capital(
        [{"capital_deployed": 2500, "opened_at": "2026-08-31T09:20"}], []) == 2500


def test_peak_capital_empty_is_zero():
    import dashboard_server as ds
    assert ds._peak_capital([], []) == 0.0
