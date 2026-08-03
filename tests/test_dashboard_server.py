"""
Tests for dashboard_server.py's error handling.

Reported live 2026-08-04: a full traceback printed to the terminal every
time a browser tab closed or refreshed mid-poll (ConnectionAbortedError /
WinError 10053). That's normal traffic for a page polling every few
seconds, not a server fault -- the client just isn't there to receive
the bytes anymore.

Run: python -m pytest tests/ -q
"""

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
