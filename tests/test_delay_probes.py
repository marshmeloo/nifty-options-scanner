"""
Tests for execution-delay probes (config.DELAY_PROBE_SAMPLES).

These measure what an order-entry delay WOULD have cost: every price
this system acts on is the price at the instant a signal fired, but a
real order is punched in seconds later and fills at whatever the market
is then. Nothing in any backtest models that gap.

The critical property pinned here is the SIGN CONVENTION: `adverse_inr`
must be negative whenever the delay hurt, on BOTH legs -- paying more
when buying, receiving less when selling. Getting that backwards is
exactly how a slippage study concludes delay is free.

Purely observational: nothing here may touch real P&L, the real
journal, or state["trades"].

Run: python -m pytest tests/ -q
"""

from datetime import datetime
from json import loads

import pytest

import config
import trade_tracker as tt
from models import MarketSnapshot, OptionQuote


@pytest.fixture
def journals(tmp_path, monkeypatch):
    real = tmp_path / "trade_journal.jsonl"
    delay = tmp_path / "execution_delay_journal.jsonl"
    monkeypatch.setattr(tt, "JOURNAL_PATH", real)
    monkeypatch.setattr(tt, "DELAY_PROBE_JOURNAL_PATH", delay)
    return real, delay


@pytest.fixture(autouse=True)
def two_samples(monkeypatch):
    monkeypatch.setattr(config, "DELAY_PROBE_SAMPLES", 2)


def _read(path):
    if not path.exists():
        return []
    return [loads(line) for line in path.read_text().strip().split("\n") if line]


def _trade(entry=100.0, stop=70.0, **overrides):
    base = {
        "id": "24050.0_PE_test",
        "strike": 24050.0,
        "option_type": "PE",
        "entry": entry,
        "stop": stop,
        "target": entry + (entry - stop) * config.DEFAULT_TARGET_RR,
        "lots": 1,
        "max_ltp_seen": entry,
        "min_ltp_seen": entry,
        "max_r_seen": 0.0,
        "rr_milestones_hit": {},
        "reason_tags": ["long_buildup"],
    }
    base.update(overrides)
    return base


def _quote(strike=24050.0, option_type="PE", ltp=100.0):
    return OptionQuote(
        symbol="NIFTY", expiry="2026-08-21", strike=strike, option_type=option_type,
        ltp=ltp, oi=50000, oi_change_pct=20.0, volume=5000, iv=12.0, iv_percentile=12.0,
    )


def _snapshot(quote, ts):
    return MarketSnapshot("NIFTY", 24010.0, 24010.0, 1.0, [quote], timestamp=ts)


T0 = datetime(2026, 8, 17, 10, 0, 0)
T1 = datetime(2026, 8, 17, 10, 0, 33)
T2 = datetime(2026, 8, 17, 10, 1, 6)


# --------------------------------------------------------------------------
# Sign convention -- the thing most worth getting right
# --------------------------------------------------------------------------

def test_entry_delay_costs_money_when_price_rises(journals):
    """Buying later at a HIGHER price is worse -> adverse_inr negative."""
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=105.0), T1))
    sample = state["delay_probes"][0]["samples"][0]

    assert sample["delta_vs_signal"] == pytest.approx(5.0)
    assert sample["adverse_inr"] < 0
    assert sample["adverse_inr"] == pytest.approx(-5.0 * 65)


def test_entry_delay_helps_when_price_falls(journals):
    """A delayed BUY that fills cheaper is a genuine gain, not a cost."""
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=97.0), T1))
    sample = state["delay_probes"][0]["samples"][0]

    assert sample["adverse_inr"] > 0
    assert sample["adverse_inr"] == pytest.approx(3.0 * 65)


def test_exit_delay_costs_money_when_price_falls(journals):
    """
    The case that matters most: a stop/breakeven exit fires BECAUSE
    price is moving against the position, so a delayed sell fills
    lower. Must read as a cost.
    """
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "exit", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=94.0), T1))
    sample = state["delay_probes"][0]["samples"][0]

    assert sample["delta_vs_signal"] == pytest.approx(-6.0)
    assert sample["adverse_inr"] < 0
    assert sample["adverse_inr"] == pytest.approx(-6.0 * 65)


def test_exit_delay_helps_when_price_rises(journals):
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "exit", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=102.0), T1))
    assert state["delay_probes"][0]["samples"][0]["adverse_inr"] > 0


def test_adverse_inr_scales_with_lots(journals):
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(lots=3), "entry", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=105.0), T1))
    assert state["delay_probes"][0]["samples"][0]["adverse_inr"] == pytest.approx(-5.0 * 65 * 3)


def test_delay_sec_is_measured_not_assumed(journals):
    """Cycles are ~33s, not exactly 30 -- the real gap must be recorded."""
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(ltp=100.0), T1))
    assert state["delay_probes"][0]["samples"][0]["delay_sec"] == pytest.approx(33.0)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def test_probe_retires_and_journals_after_enough_samples(journals):
    _real, delay_path = journals
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    assert tt.update_delay_probes(state, _snapshot(_quote(ltp=101.0), T1)) == []
    assert len(state["delay_probes"]) == 1      # still collecting after 1 of 2

    completed = tt.update_delay_probes(state, _snapshot(_quote(ltp=102.0), T2))
    assert len(completed) == 1
    assert state["delay_probes"] == []
    entries = _read(delay_path)
    assert len(entries) == 1
    assert entries[0]["complete"] is True
    assert len(entries[0]["samples"]) == 2


def test_probe_survives_a_cycle_with_no_quote(journals):
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    tt.update_delay_probes(state, _snapshot(_quote(strike=24100.0), T1))   # different contract
    assert len(state["delay_probes"]) == 1
    assert state["delay_probes"][0]["samples"] == []


def test_eod_journals_incomplete_probes_flagged(journals):
    """
    A probe seeded near the close never gets its full sample count.
    Dropping those silently would bias the measurement toward
    mid-session conditions only.
    """
    _real, delay_path = journals
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "exit", 100.0, T0)

    closed = tt.force_close_delay_probes(state, _snapshot(_quote(ltp=98.0), T1))

    assert len(closed) == 1
    assert closed[0]["complete"] is False       # only 1 of 2 samples
    assert closed[0]["ended_at_market_close"] is True
    assert state["delay_probes"] == []
    assert _read(delay_path)[0]["complete"] is False


def test_disabling_probes_collects_nothing(journals, monkeypatch):
    monkeypatch.setattr(config, "DELAY_PROBE_SAMPLES", 0)
    state = {"trades": [], "delay_probes": []}
    tt._seed_delay_probe(state, _trade(), "entry", 100.0, T0)

    assert state["delay_probes"] == []
    assert tt.update_delay_probes(state, _snapshot(_quote(), T1)) == []


# --------------------------------------------------------------------------
# Wiring into real trade flow -- and staying out of real P&L
# --------------------------------------------------------------------------

def test_closing_a_trade_seeds_an_exit_probe(journals):
    state = {"trades": [_trade()], "delay_probes": []}
    closed = tt.update_open_trades(state, _snapshot(_quote(ltp=70.0), T0))

    assert closed[0]["outcome"] == "LOSS"
    probes = state["delay_probes"]
    assert len(probes) == 1
    assert probes[0]["leg"] == "exit"
    assert probes[0]["signal_price"] == 70.0


def test_every_outcome_gets_a_probe_not_just_losses(journals):
    """Sampling only adverse exits would overstate the measured cost."""
    state = {"trades": [_trade()], "delay_probes": []}
    closed = tt.update_open_trades(state, _snapshot(_quote(ltp=160.0), T0))

    assert closed[0]["outcome"] == "WIN"
    assert len(state["delay_probes"]) == 1


def test_probes_never_touch_real_pnl_or_the_real_journal(journals):
    real, delay_path = journals
    state = {"trades": [_trade()], "delay_probes": []}
    closed = tt.update_open_trades(state, _snapshot(_quote(ltp=70.0), T0))
    recorded_pnl = closed[0]["pnl_inr"]

    tt.update_delay_probes(state, _snapshot(_quote(ltp=50.0), T1))
    tt.update_delay_probes(state, _snapshot(_quote(ltp=40.0), T2))

    assert closed[0]["pnl_inr"] == recorded_pnl          # unchanged by a worse later price
    assert len(_read(real)) == 1                          # one real close, nothing more
    assert len(_read(delay_path)) == 1                    # probe went to its own file


def test_load_open_trades_defaults_delay_probes(tmp_path, monkeypatch):
    """State written before this feature existed has no delay_probes key."""
    from datetime import date
    path = tmp_path / "open_trades.json"
    path.write_text(f'{{"date": "{date.today().isoformat()}", "trades": [], "opened_today": 0}}')
    monkeypatch.setattr(tt, "OPEN_TRADES_PATH", path)

    assert tt.load_open_trades()["delay_probes"] == []
