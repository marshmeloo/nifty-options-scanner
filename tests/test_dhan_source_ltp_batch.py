"""
Tests for dhan_source.get_ltp_batch() -- the lightweight /marketfeed/ltp
wrapper added 2026-08-18 for the fast stop/target check, so it can stop
re-fetching a full option chain just to check a handful of open strikes.

Run: python -m pytest tests/test_dhan_source_ltp_batch.py -q
"""

import pytest

import dhan_source as ds


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ds.requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def no_env_headers(monkeypatch):
    """_headers() requires real env vars; every test here goes through a
    faked requests.post, so the actual header values don't matter."""
    monkeypatch.setattr(ds, "_headers", lambda: {"fake": "headers"})


@pytest.fixture(autouse=True)
def isolated_rate_limiter(monkeypatch):
    """Point the LTP rate limiter at throwaway files so tests never touch
    real shared state, matching the convention in
    tests/test_dhan_rate_limiter.py."""
    import dhan_rate_limiter as drl
    monkeypatch.setattr(drl, "LTP_LOCK_PATH", drl.STATE_DIR / "test_ltp.lock")
    monkeypatch.setattr(drl, "LTP_STATE_PATH", drl.STATE_DIR / "test_ltp.json")
    yield
    for p in (drl.LTP_LOCK_PATH, drl.LTP_STATE_PATH):
        if p.exists():
            p.unlink()


def test_empty_list_returns_empty_dict_without_a_request(monkeypatch):
    calls = []
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: calls.append(1))
    assert ds.get_ltp_batch([]) == {}
    assert calls == []


def test_parses_response_into_int_keyed_price_dict(monkeypatch):
    fake_payload = {
        "data": {"NSE_FNO": {"49081": {"last_price": 368.15}, "49082": {"last_price": 12.5}}},
        "status": "success",
    }
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: _FakeResponse(fake_payload))

    result = ds.get_ltp_batch([49081, 49082])

    assert result == {49081: 368.15, 49082: 12.5}
    assert all(isinstance(k, int) for k in result)


def test_sends_ids_grouped_under_the_exchange_segment(monkeypatch):
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen["url"] = url
        seen["json"] = json
        return _FakeResponse({"data": {"NSE_FNO": {}}, "status": "success"})

    monkeypatch.setattr(ds.requests, "post", fake_post)
    ds.get_ltp_batch([111, 222, 333])

    assert seen["url"] == f"{ds.DHAN_BASE_URL}/marketfeed/ltp"
    assert seen["json"] == {"NSE_FNO": [111, 222, 333]}


def test_uses_the_ltp_rate_limiter_not_the_chain_one(monkeypatch):
    """The whole point of the separate budget (see dhan_rate_limiter's
    module docstring): this call must not queue behind /optionchain's
    slower interval."""
    calls = []
    monkeypatch.setattr(ds.dhan_rate_limiter, "wait_for_ltp_slot", lambda: calls.append("ltp"))
    monkeypatch.setattr(ds.dhan_rate_limiter, "wait_for_slot", lambda *a, **kw: calls.append("chain"))
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: _FakeResponse({"data": {}, "status": "success"}))

    ds.get_ltp_batch([111])

    assert calls == ["ltp"]


def test_a_requested_id_missing_from_the_response_is_simply_absent(monkeypatch):
    """Mirrors update_open_trades()'s existing graceful degradation for a
    strike missing from the chain: no KeyError, no invented price."""
    fake_payload = {"data": {"NSE_FNO": {"111": {"last_price": 42.0}}}, "status": "success"}
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: _FakeResponse(fake_payload))

    result = ds.get_ltp_batch([111, 999])

    assert result == {111: 42.0}
    assert 999 not in result


def test_a_malformed_entry_does_not_lose_the_rest_of_the_batch(monkeypatch):
    fake_payload = {
        "data": {"NSE_FNO": {"111": {"last_price": 42.0}, "222": {"no_price_field": True}}},
        "status": "success",
    }
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: _FakeResponse(fake_payload))

    result = ds.get_ltp_batch([111, 222])

    assert result == {111: 42.0}


def test_raises_on_http_error_matching_other_dhan_calls(monkeypatch):
    """Same failure behaviour as _fetch_raw_chain and friends -- an HTTP
    error propagates rather than being silently swallowed into an empty
    result, so a caller can distinguish "no data for this contract" from
    "the request itself failed"."""
    monkeypatch.setattr(ds.requests, "post", lambda *a, **kw: _FakeResponse({}, status_code=500))

    with pytest.raises(ds.requests.HTTPError):
        ds.get_ltp_batch([111])
