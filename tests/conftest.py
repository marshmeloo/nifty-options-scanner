"""
Shared test fixtures.

WHY THE SCORING_MODE DEFAULT IS FORCED HERE
--------------------------------------------
config.SCORING_MODE defaults to "momentum_only" live (adopted 2026-08-02,
see config.py's docstring on that setting). Under it, scanner.scan()
overrides every candidate's final score down to one of three constants
based on momentum alignment alone -- by design, since that is the whole
point of the mode.

Most of this suite's existing tests predate that switch and assert on the
underlying WEIGHTED score (IV, OI buildup, support/resistance levels,
RSI, etc.) -- that computation is still very much alive in scanner.py,
just no longer what final_score is set to by default. Without forcing
"legacy" here, every one of those tests would collapse to comparing
3.0 == 3.0 (the momentum_only neutral score) regardless of what the
component logic under test actually did, which would silently stop
testing anything.

Tests specifically about SCORING_MODE itself set it explicitly within
the test (monkeypatch layering makes that override this default for the
duration of that test only).
"""

import pytest

import config


@pytest.fixture(autouse=True)
def _default_scoring_mode(monkeypatch):
    monkeypatch.setattr(config, "SCORING_MODE", "legacy")


@pytest.fixture(autouse=True)
def _isolate_staged_orders(tmp_path, monkeypatch):
    """
    Point trade_staging at a throwaway file for EVERY test.

    Without this, tests that exercise a tracker's staging path wrote into
    the REAL state/staged_orders.json. Measured 2026-08-31: one run of the
    suite appended 3 records (485 -> 488), and production had accumulated
    99 PENDING notices -- 64 of them `condor_*`, from a strategy removed
    from live automation on 2026-08-26 and therefore incapable of
    producing them. The live dashboard's "Notices" panel was showing that
    backlog as though it were real trading activity.

    Deliberately AUTOUSE rather than a fixture each test opts into. Three
    test files already monkeypatched STAGED_ORDERS_PATH themselves and
    two others still leaked, which is exactly how an opt-in list fails --
    it only covers what someone remembered. Same reasoning as the
    full-module config restore in tests/test_opposite_direction_gate.py.
    Tests that want to assert on staged output still monkeypatch the path
    to their own tmp file; this just guarantees the default is never the
    real one.
    """
    import trade_staging as staging
    monkeypatch.setattr(staging, "STAGED_ORDERS_PATH", tmp_path / "staged_orders.json")
