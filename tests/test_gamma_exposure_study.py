"""
Tests for the pure-logic pieces of gamma_exposure_study.py:
_is_momentum_aligned, analyse(), and describe(). The row-building loop
(candidate_rows_with_gamma) mirrors component_study.candidate_rows(),
which itself has no unit test coverage in this suite -- that module is
validated by running it against real recorded history, not synthetic
fixtures wiring together scan()/price_action/oi_analytics/gamma_exposure
all at once. Same approach here; the real validation is the backtest run
itself (see BACKLOG.md / README for the recorded result).

Run: python -m pytest tests/test_gamma_exposure_study.py -q
"""

from research import gamma_exposure_study as study


# --------------------------------------------------------------------------
# _is_momentum_aligned
# --------------------------------------------------------------------------

def test_momentum_aligned_reason_detected():
    reasons = ["Momentum aligned: +1.23% ROC supports this direction"]
    assert study._is_momentum_aligned(reasons) is True


def test_momentum_against_reason_not_aligned():
    reasons = ["Momentum against: -0.80% ROC opposes this direction"]
    assert study._is_momentum_aligned(reasons) is False


def test_no_momentum_reason_not_aligned():
    reasons = ["Long buildup (bullish for this contract)"]
    assert study._is_momentum_aligned(reasons) is False


def test_empty_reasons_not_aligned():
    assert study._is_momentum_aligned([]) is False


# --------------------------------------------------------------------------
# analyse()
# --------------------------------------------------------------------------

def _row(ret, aligned=True, regime="SHORT_GAMMA"):
    return {"score": 6.0, "ret": ret, "momentum_aligned": aligned, "gamma_regime": regime, "net_gex": -1.0}


def test_analyse_empty_rows():
    result = study.analyse([])
    assert result["n_momentum_aligned"] == 0


def test_analyse_ignores_non_aligned_candidates():
    rows = [_row(5.0, aligned=False, regime="SHORT_GAMMA")] * 50
    result = study.analyse(rows)
    assert result["n_momentum_aligned"] == 0


def test_analyse_reports_no_comparison_below_the_sample_floor():
    """Same 30-per-side discipline as MIN_TRADES_FOR_ANY_ADJUSTMENT --
    below it, no comparison is reported at all, just the counts."""
    rows = [_row(5.0, regime="SHORT_GAMMA")] * 10 + [_row(3.0, regime="LONG_GAMMA")] * 40
    result = study.analyse(rows)
    assert result["n_short_gamma"] == 10
    assert result["n_long_gamma"] == 40
    assert "lift_short_minus_long_pct" not in result
    assert "need >=30 each" in result["note"]


def test_analyse_computes_lift_and_z_once_both_sides_clear_the_floor():
    short_rows = [_row(2.0, regime="SHORT_GAMMA") for _ in range(40)]
    long_rows = [_row(0.5, regime="LONG_GAMMA") for _ in range(40)]
    result = study.analyse(short_rows + long_rows)

    assert result["n_short_gamma"] == 40
    assert result["n_long_gamma"] == 40
    assert result["mean_ret_short_gamma_pct"] == 2.0
    assert result["mean_ret_long_gamma_pct"] == 0.5
    assert result["lift_short_minus_long_pct"] == 1.5
    # Zero within-group variance here (every row identical) makes the
    # standard error zero -- z is None in that degenerate case rather
    # than a division by zero.
    assert result["z"] is None


def test_analyse_z_is_a_real_number_with_natural_variance():
    import random
    random.seed(0)
    short_rows = [_row(random.gauss(2.0, 1.0), regime="SHORT_GAMMA") for _ in range(200)]
    long_rows = [_row(random.gauss(0.0, 1.0), regime="LONG_GAMMA") for _ in range(200)]
    result = study.analyse(short_rows + long_rows)
    assert result["z"] is not None
    assert result["z"] > 0   # short_gamma group has the higher mean by construction


def test_analyse_ignores_rows_with_no_computed_regime():
    """A cycle where gamma_exposure raised (caught in
    candidate_rows_with_gamma, regime=None) must not be silently counted
    into either bucket."""
    rows = [_row(5.0, regime=None)] * 50 + [_row(3.0, regime="SHORT_GAMMA")] * 30 + [_row(1.0, regime="LONG_GAMMA")] * 30
    result = study.analyse(rows)
    assert result["n_momentum_aligned"] == 60   # the 50 None-regime rows are excluded entirely


# --------------------------------------------------------------------------
# describe()
# --------------------------------------------------------------------------

def test_describe_handles_empty_summary():
    text = study.describe({"n_momentum_aligned": 0, "note": "no data"}, horizon_minutes=30)
    assert "no data" in text


def test_describe_handles_below_floor_summary():
    summary = {"n_momentum_aligned": 50, "n_short_gamma": 10, "n_long_gamma": 40, "note": "too few, need >=30 each"}
    text = study.describe(summary, horizon_minutes=30)
    assert "too few" in text


def test_describe_reports_hypothesis_direction_when_short_gamma_wins():
    summary = {
        "n_momentum_aligned": 100, "n_short_gamma": 50, "n_long_gamma": 50,
        "mean_ret_short_gamma_pct": 2.0, "mean_ret_long_gamma_pct": 0.5,
        "lift_short_minus_long_pct": 1.5, "z": 2.5,
    }
    text = study.describe(summary, horizon_minutes=30)
    assert "supports the hypothesis" in text
    assert "significant" in text


def test_describe_reports_hypothesis_direction_when_long_gamma_wins():
    summary = {
        "n_momentum_aligned": 100, "n_short_gamma": 50, "n_long_gamma": 50,
        "mean_ret_short_gamma_pct": 0.5, "mean_ret_long_gamma_pct": 2.0,
        "lift_short_minus_long_pct": -1.5, "z": -2.5,
    }
    text = study.describe(summary, horizon_minutes=30)
    assert "OPPOSES the hypothesis" in text
