"""
orderflow_feed_banknifty.py -- the Bank Nifty counterpart to
orderflow_feed.py, added 2026-08-16 to close the coverage gap where
Bank Nifty's book_imbalance/total_quantity_imbalance always read None
(no feed ever subscribed to a Bank Nifty strike). Verifies the wiring
(underlying scrip/seg, own state path, own recording directory) without
ever opening a real WebSocket.

Run: python -m pytest tests/test_orderflow_feed_banknifty.py -q
"""
import dhan_source
import orderflow_feed as of
import orderflow_feed_banknifty as ofbn
import orderflow_recorder


def test_state_path_distinct_from_nifty():
    assert ofbn.STATE_PATH != of.STATE_PATH
    assert ofbn.STATE_PATH.name == "orderflow_banknifty.json"


def test_default_strike_range_is_positive_and_wider_than_nifty_default():
    # NIFTY's own automation invocation uses 300; Bank Nifty's wider
    # strike spacing warrants a larger default, not a smaller one.
    assert ofbn.DEFAULT_STRIKE_RANGE_POINTS > 300


def test_main_wires_banknifty_underlying_and_own_state_path(monkeypatch):
    captured = {}

    def fake_select_contracts(expiry, strike_range, underlying=None,
                              underlying_scrip=None, underlying_seg=None):
        captured["underlying"] = underlying
        captured["underlying_scrip"] = underlying_scrip
        captured["underlying_seg"] = underlying_seg
        captured["strike_range"] = strike_range
        return ([{"security_id": 1, "strike": 51000.0, "option_type": "CE", "expiry": "2026-08-27"}], 51000.0)

    class FakeFeed:
        def __init__(self, contracts, state_path=None, record_spreads=True, reference_spot=None):
            captured["state_path"] = state_path
            captured["contracts"] = contracts
            self.packets_seen = 0
            self.samples_recorded = 0
        def run_forever(self):
            captured["ran"] = True
        def stop(self):
            pass

    monkeypatch.setattr(ofbn, "select_contracts", fake_select_contracts)
    monkeypatch.setattr(ofbn, "OrderFlowFeed", FakeFeed)
    monkeypatch.setattr("sys.argv", ["orderflow_feed_banknifty.py"])

    ofbn.main()

    assert captured["underlying"] == "BANKNIFTY"
    assert captured["underlying_scrip"] == dhan_source.BANKNIFTY_UNDERLYING_SCRIP
    assert captured["underlying_seg"] == dhan_source.BANKNIFTY_UNDERLYING_SEG
    assert captured["strike_range"] == ofbn.DEFAULT_STRIKE_RANGE_POINTS
    assert captured["state_path"] == ofbn.STATE_PATH
    assert captured["ran"] is True


def test_main_overrides_recorder_dir_to_banknifty_own(monkeypatch):
    # main() assigns orderflow_recorder.RECORD_DIR directly (matching real
    # production use -- this process owns that module for its lifetime),
    # not via monkeypatch, so it wouldn't auto-revert on its own. Seed
    # monkeypatch with the pre-call value so its normal teardown still
    # restores it after this test, keeping other tests/files that read
    # RECORD_DIR's default unaffected regardless of run order.
    monkeypatch.setattr(orderflow_recorder, "RECORD_DIR", orderflow_recorder.RECORD_DIR)

    def fake_select_contracts(*a, **kw):
        return ([{"security_id": 1, "strike": 51000.0, "option_type": "CE", "expiry": "2026-08-27"}], 51000.0)

    class FakeFeed:
        def __init__(self, *a, **kw):
            self.packets_seen = 0
            self.samples_recorded = 0
        def run_forever(self):
            pass
        def stop(self):
            pass

    monkeypatch.setattr(ofbn, "select_contracts", fake_select_contracts)
    monkeypatch.setattr(ofbn, "OrderFlowFeed", FakeFeed)
    monkeypatch.setattr("sys.argv", ["orderflow_feed_banknifty.py"])

    ofbn.main()

    assert orderflow_recorder.RECORD_DIR.name == "orderflow_banknifty"
    assert orderflow_recorder.RECORD_DIR != of.STATE_DIR.parent / "logs" / "orderflow"


def test_main_handles_no_contracts_gracefully(monkeypatch, capsys):
    monkeypatch.setattr(ofbn, "select_contracts", lambda *a, **kw: ([], 51000.0))
    monkeypatch.setattr("sys.argv", ["orderflow_feed_banknifty.py"])

    ofbn.main()  # must not raise

    assert "No contracts selected" in capsys.readouterr().out
