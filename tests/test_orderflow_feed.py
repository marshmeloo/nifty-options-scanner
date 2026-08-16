"""
orderflow_feed.py's select_contracts() underlying-parameterization (added
2026-08-16, for Bank Nifty order-flow support) -- defaults reproduce the
original NIFTY-only behaviour; underlying="BANKNIFTY" selects Bank
Nifty's own chain via dhan_source/instrument_master.

Run: python -m pytest tests/test_orderflow_feed.py -q
"""
import dhan_source
import instrument_master
import orderflow_feed as of
from models import MarketSnapshot, OptionQuote


def _quote(strike, opt_type, symbol="NIFTY", expiry="2026-08-28"):
    return OptionQuote(symbol=symbol, expiry=expiry, strike=strike, option_type=opt_type,
                       ltp=100.0, oi=1000, oi_change_pct=0.0, volume=500, iv=15.0, iv_percentile=50.0)


def _snapshot(spot, chain, symbol="NIFTY"):
    return MarketSnapshot(symbol=symbol, spot=spot, vwap=spot, pcr=1.0, chain=chain, source="dhan")


def test_select_contracts_defaults_to_nifty(monkeypatch):
    captured = {}

    def fake_get_nifty_snapshot(expiry=None, must_include_strikes=None, symbol="NIFTY",
                                 underlying_scrip=None, underlying_seg=None):
        captured["symbol"] = symbol
        captured["underlying_scrip"] = underlying_scrip
        captured["underlying_seg"] = underlying_seg
        return _snapshot(24000.0, [_quote(24000.0, "CE")])

    monkeypatch.setattr(of.dhan_source, "get_nifty_snapshot", fake_get_nifty_snapshot)
    monkeypatch.setattr(of.dhan_source, "get_nearest_expiry", lambda *a, **kw: "2026-08-28")
    monkeypatch.setattr(of.instrument_master, "build_index",
                        lambda underlying="NIFTY": {(24000.0, "2026-08-28", "CE"): 111})

    contracts, spot = of.select_contracts()

    assert captured["symbol"] == "NIFTY"
    assert captured["underlying_scrip"] == dhan_source.NIFTY_UNDERLYING_SCRIP
    assert captured["underlying_seg"] == dhan_source.NIFTY_UNDERLYING_SEG
    assert spot == 24000.0
    assert contracts == [{"security_id": 111, "strike": 24000.0, "option_type": "CE", "expiry": "2026-08-28"}]


def test_select_contracts_banknifty_uses_banknifty_scrip_and_index(monkeypatch):
    captured = {}

    def fake_get_nifty_snapshot(expiry=None, must_include_strikes=None, symbol="NIFTY",
                                 underlying_scrip=None, underlying_seg=None):
        captured["symbol"] = symbol
        captured["underlying_scrip"] = underlying_scrip
        captured["underlying_seg"] = underlying_seg
        return _snapshot(51000.0, [_quote(51000.0, "CE", symbol="BANKNIFTY")], symbol="BANKNIFTY")

    build_index_calls = []
    monkeypatch.setattr(of.dhan_source, "get_nifty_snapshot", fake_get_nifty_snapshot)
    monkeypatch.setattr(of.dhan_source, "get_nearest_expiry", lambda *a, **kw: "2026-08-27")
    monkeypatch.setattr(of.instrument_master, "build_index",
                        lambda underlying="NIFTY": (build_index_calls.append(underlying)
                                                     or {(51000.0, "2026-08-27", "CE"): 999}))

    contracts, spot = of.select_contracts(
        underlying="BANKNIFTY",
        underlying_scrip=dhan_source.BANKNIFTY_UNDERLYING_SCRIP,
        underlying_seg=dhan_source.BANKNIFTY_UNDERLYING_SEG,
    )

    assert captured["symbol"] == "BANKNIFTY"
    assert captured["underlying_scrip"] == dhan_source.BANKNIFTY_UNDERLYING_SCRIP
    assert build_index_calls == ["BANKNIFTY"]
    assert spot == 51000.0
    assert contracts[0]["security_id"] == 999


def test_select_contracts_filters_by_strike_range(monkeypatch):
    chain = [_quote(23800.0, "CE"), _quote(24000.0, "CE"), _quote(24500.0, "CE")]
    monkeypatch.setattr(of.dhan_source, "get_nifty_snapshot",
                        lambda **kw: _snapshot(24000.0, chain))
    monkeypatch.setattr(of.dhan_source, "get_nearest_expiry", lambda *a, **kw: "2026-08-28")
    monkeypatch.setattr(of.instrument_master, "build_index",
                        lambda underlying="NIFTY": {
                            (23800.0, "2026-08-28", "CE"): 1,
                            (24000.0, "2026-08-28", "CE"): 2,
                            (24500.0, "2026-08-28", "CE"): 3,
                        })

    contracts, _ = of.select_contracts(strike_range_points=300)

    strikes = sorted(c["strike"] for c in contracts)
    assert strikes == [23800.0, 24000.0]  # 24500 is 500pts out, excluded


def test_select_contracts_skips_strikes_missing_from_instrument_master(monkeypatch):
    chain = [_quote(24000.0, "CE"), _quote(24050.0, "CE")]
    monkeypatch.setattr(of.dhan_source, "get_nifty_snapshot",
                        lambda **kw: _snapshot(24000.0, chain))
    monkeypatch.setattr(of.dhan_source, "get_nearest_expiry", lambda *a, **kw: "2026-08-28")
    # Only 24000 is in the "master" -- 24050 is missing (stale master).
    monkeypatch.setattr(of.instrument_master, "build_index",
                        lambda underlying="NIFTY": {(24000.0, "2026-08-28", "CE"): 1})

    contracts, _ = of.select_contracts()

    assert len(contracts) == 1
    assert contracts[0]["strike"] == 24000.0
