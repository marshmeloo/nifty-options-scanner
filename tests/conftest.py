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
