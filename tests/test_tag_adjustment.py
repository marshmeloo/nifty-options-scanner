"""
Tests for the rewritten learned tag adjustment.

The previous design penalised a tag -0.5 whenever its win rate sat below
an ABSOLUTE 0.4, needing only 3 samples. Measured against real sessions
it was wrong three ways at once, and these tests pin each fix:

  1. n=3 produced confident penalties on evidence with a 6%-79%
     confidence interval.
  2. The journal's base win rate was 22%, so EVERY tag fell below 0.4
     and every tag was penalised -- including tags performing above the
     system's own average.
  3. Penalties stacked across heavily-correlated tags, so the total grew
     with the raw score (corr +0.385): more confirming signals meant a
     bigger handicap, a progressive tax on conviction.

Run: python -m pytest tests/ -q
"""

import pytest

import config
import trade_tracker as tt


@pytest.fixture(autouse=True)
def _adjustment_enabled(monkeypatch):
    """
    This file's whole purpose is pinning the adjustment MECHANISM, so it
    runs as if enabled regardless of the live default -- see
    test_adjustment_is_disabled_by_default below for the coverage of
    that default itself. config.LEARNED_TAG_ADJUSTMENT_ENABLED went to
    False 2026-08-18 (see config.py's comment): even with
    MIN_TRADES_FOR_ANY_ADJUSTMENT/MIN_TAG_SAMPLES_FOR_ADJUSTMENT both
    satisfied on the live journal, that was judged too small a sample to
    trust for per-tag differentiation this early.
    """
    monkeypatch.setattr(config, "LEARNED_TAG_ADJUSTMENT_ENABLED", True)


def test_adjustment_is_disabled_by_default(monkeypatch):
    """The live default, unset by the fixture above."""
    monkeypatch.setattr(config, "LEARNED_TAG_ADJUSTMENT_ENABLED", False)
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: (_ for _ in ()).throw(
        AssertionError("disabled path must not even read the journal")))
    score, notes = tt.apply_learned_adjustment(5.5, ["Bullish FVG at this strike (1-2)"])
    assert score == 5.5
    assert any("disabled" in n for n in notes)


def _journal(entries):
    """entries: list of (outcome, [tags])"""
    return [{"outcome": o, "reason_tags": t} for o, t in entries]


def _set_journal(monkeypatch, entries):
    monkeypatch.setattr(tt, "_load_recent_journal", lambda limit=None: _journal(entries))


# --------------------------------------------------------------------------
# Guard 1: no adjustment at all on a thin overall sample
# --------------------------------------------------------------------------

def test_no_adjustment_below_minimum_total_trades(monkeypatch):
    """
    The user's actual objection: penalising on 1W/2L. With single-digit
    totals there is nothing to learn, so nothing should move.
    """
    _set_journal(monkeypatch, [("WIN", ["fvg"]), ("LOSS", ["fvg"]), ("LOSS", ["fvg"])])
    score, notes = tt.apply_learned_adjustment(5.5, ["Bullish FVG at this strike (1-2)"])
    assert score == 5.5
    assert any("need" in n and "before tag win rates mean anything" in n for n in notes)


def test_adjustment_switches_on_once_enough_trades_exist(monkeypatch):
    """
    A tag genuinely BELOW the base rate, with real sample weight behind
    it, should still reduce the score -- the rewrite makes small samples
    safe, it doesn't disable learning altogether.
    """
    entries = ([("WIN", ["fvg"])] * 5 + [("LOSS", ["fvg"])] * 45   # fvg at 10%
               + [("WIN", [])] * 30)                                # lifts base well above 10%
    _set_journal(monkeypatch, entries)
    assert len(entries) >= config.MIN_TRADES_FOR_ANY_ADJUSTMENT
    base, _ = tt.base_win_rate()
    assert base > 0.10, "fixture must put the tag below the base rate"

    score, notes = tt.apply_learned_adjustment(5.5, ["Bullish FVG at this strike (1-2)"])
    assert score < 5.5, "a genuinely poor tag with real weight should still reduce the score"
    assert any("fvg" in n for n in notes)


# --------------------------------------------------------------------------
# Guard 2: measured against the base rate, not an absolute threshold
# --------------------------------------------------------------------------

def test_base_win_rate_computed_from_journal(monkeypatch):
    _set_journal(monkeypatch, [("WIN", []), ("LOSS", []), ("LOSS", []), ("LOSS", [])])
    base, n = tt.base_win_rate()
    assert base == pytest.approx(0.25)
    assert n == 4


def test_base_win_rate_ignores_non_decided_outcomes(monkeypatch):
    _set_journal(monkeypatch, [
        ("WIN", []), ("LOSS", []),
        ("EOD_CLOSE", []), ("RECOVERED_INTERRUPTED_SESSION", []),
    ])
    base, n = tt.base_win_rate()
    assert n == 2, "only WIN/LOSS are decided outcomes"
    assert base == pytest.approx(0.5)


def test_tag_at_the_base_rate_is_not_penalised(monkeypatch):
    """
    The core bug: on the real journal every tag sat at or above the 22%
    base rate, yet all were penalised for being under an absolute 0.4.
    A tag performing exactly at average must not move the score.
    """
    # Base rate 20%; the tag also runs at exactly 20%.
    entries = [("WIN", ["fvg"])] * 10 + [("LOSS", ["fvg"])] * 40
    _set_journal(monkeypatch, entries)
    base, _ = tt.base_win_rate()
    assert base == pytest.approx(0.20)

    score, _notes = tt.apply_learned_adjustment(5.0, ["Bullish FVG at this strike (1-2)"])
    assert score == pytest.approx(5.0, abs=0.01), "a tag at the base rate should be neutral"


def test_tag_above_base_rate_is_rewarded_even_if_below_50pct(monkeypatch):
    """
    A 30% tag in a 20%-base-rate system is GOOD, even though 30% would
    look 'weak' against any absolute threshold.
    """
    entries = ([("WIN", ["fvg"])] * 15 + [("LOSS", ["fvg"])] * 35      # fvg 30%
               + [("LOSS", [])] * 50)                                   # drags base down
    _set_journal(monkeypatch, entries)
    base, _ = tt.base_win_rate()
    assert base < 0.30

    score, notes = tt.apply_learned_adjustment(5.0, ["Bullish FVG at this strike (1-2)"])
    assert score > 5.0, "above-average tag should help, not hurt"
    assert any("above average" in n for n in notes)


# --------------------------------------------------------------------------
# Guard 3: shrinkage, and the cap on stacked correlated tags
# --------------------------------------------------------------------------

def test_shrinkage_pulls_small_samples_to_the_base_rate():
    base = 0.40
    # A 1W/2L tag observed at 33% barely moves off base after shrinking.
    assert tt.shrunk_tag_rate(1, 2, base) == pytest.approx(base, abs=0.02)
    # A 40W/10L tag observed at 80% earns real separation.
    assert tt.shrunk_tag_rate(40, 10, base) > base + 0.2


def test_shrinkage_is_monotonic_in_sample_size():
    """Same observed rate, more evidence -> further from the base rate."""
    base = 0.40
    small = tt.shrunk_tag_rate(8, 2, base)     # 80%, n=10
    large = tt.shrunk_tag_rate(80, 20, base)   # 80%, n=100
    assert large > small > base


def test_total_adjustment_is_capped(monkeypatch):
    """
    Tags co-occur heavily (fvg + long_buildup + support on one setup),
    so uncapped summing double-counts one market condition -- which is
    what made the old penalty scale with the raw score.
    """
    tags = ["fvg", "long_buildup", "support", "resistance", "iv_cheap"]
    entries = [("LOSS", tags)] * 60 + [("WIN", [])] * 40   # every tag far below a 40% base
    _set_journal(monkeypatch, entries)

    reasons = ["Bullish FVG at this strike (1-2)", "Long buildup (bullish for this contract)",
               "Support level at this strike (1-2)", "Resistance level at this strike (1-2)",
               "IV cheap: 12th percentile"]
    score, notes = tt.apply_learned_adjustment(5.0, reasons)

    assert score >= 5.0 - config.MAX_TOTAL_TAG_ADJUSTMENT - 0.001
    assert any("capped" in n for n in notes)


def test_penalty_does_not_grow_without_bound_with_more_tags(monkeypatch):
    """
    Directly pins the perverse scaling: a candidate with FIVE weak tags
    must not be penalised ~5x one with a single weak tag.
    """
    tags = ["fvg", "long_buildup", "support", "resistance", "iv_cheap"]
    entries = [("LOSS", tags)] * 60 + [("WIN", [])] * 40
    _set_journal(monkeypatch, entries)

    one, _ = tt.apply_learned_adjustment(5.0, ["Bullish FVG at this strike (1-2)"])
    five, _ = tt.apply_learned_adjustment(5.0, [
        "Bullish FVG at this strike (1-2)", "Long buildup (bullish for this contract)",
        "Support level at this strike (1-2)", "Resistance level at this strike (1-2)",
        "IV cheap: 12th percentile",
    ])
    penalty_one, penalty_five = 5.0 - one, 5.0 - five
    assert penalty_five <= config.MAX_TOTAL_TAG_ADJUSTMENT + 0.001
    assert penalty_five < penalty_one * 5, "five correlated tags must not stack to 5x the penalty"


def test_unknown_tags_are_ignored(monkeypatch):
    entries = [("WIN", ["fvg"])] * 10 + [("LOSS", ["fvg"])] * 40
    _set_journal(monkeypatch, entries)
    score, _ = tt.apply_learned_adjustment(5.0, ["Bullish liquidity sweep at this strike (1-2)"])
    assert score == pytest.approx(5.0), "a tag with no history should be neutral"


def test_empty_journal_is_safe(monkeypatch):
    _set_journal(monkeypatch, [])
    score, notes = tt.apply_learned_adjustment(5.0, ["Bullish FVG at this strike (1-2)"])
    assert score == 5.0
    assert notes


# --------------------------------------------------------------------------
# _reason_tags: the 2026-08-31 substring bug.
#
# "support" matched "supportS this direction" -- a MOMENTUM reason with no
# level involved -- and "supportS this contract", which scanner._score_levels
# appends to every FAVOURABLE level of any kind. Measured on the real
# 2026-08-31 session: all 27 trades carried a "support" tag and not one came
# from a support level. 27 of 27 false positives.
# --------------------------------------------------------------------------

def test_momentum_reason_does_not_produce_a_support_tag():
    """The exact string from the live 2026-08-31 56400 PE entry."""
    from trade_tracker import _reason_tags
    reasons = [
        "Short buildup (bearish for this contract -- writers piling in): "
        "OI +3.2%, premium -1.9% over the last 14 min",
        "Momentum aligned: -0.30% ROC supports this direction",
    ]
    tags = _reason_tags(reasons)
    assert "support" not in tags, "momentum 'supports this direction' is not a support level"
    assert "short_buildup" in tags


def test_favourable_level_note_does_not_produce_a_support_tag():
    """scanner._score_levels appends '-- supports this contract' to EVERY
    favourable level, so a bullish FVG used to tag itself as support too."""
    from trade_tracker import _reason_tags
    tags = _reason_tags(["Bullish FVG at this strike (24248.2-24253.5) -- supports this contract"])
    assert tags == ["fvg"], tags


def test_real_support_level_still_tags():
    from trade_tracker import _reason_tags
    tags = _reason_tags(["Support level at this strike (24000.0-24020.0) -- supports this contract"])
    assert "support" in tags


def test_real_resistance_level_still_tags():
    from trade_tracker import _reason_tags
    tags = _reason_tags(["Resistance level at this strike (24500.0-24520.0) -- supports this contract"])
    assert "resistance" in tags


def test_level_that_argues_against_is_not_tagged():
    """A level pointing the wrong way is evidence AGAINST the trade. Tagging
    it as confluence teaches tag_win_rates the opposite of the truth."""
    from trade_tracker import _reason_tags
    tags = _reason_tags([
        "Resistance level at this strike (24500.0-24520.0) -- argues AGAINST this contract",
        "Long buildup (bullish for this contract): OI +4.1%",
    ])
    assert "resistance" not in tags
    assert "long_buildup" in tags


def test_liquidity_sweep_still_tags():
    from trade_tracker import _reason_tags
    tags = _reason_tags(["Bearish liquidity sweep at this strike (24500.0-24560.0) -- supports this contract"])
    assert "sweep" in tags
    assert "resistance" not in tags, "the word 'resistance' must not leak in from a sweep label"
