"""
Alternative scoring weightings, evaluated over recorded history.

WHY THESE VARIANTS
------------------
The per-component forward-return study (component_study.py, 527,528
candidates over 493 days) found the scorer's weights badly mismatched to
its components' actual predictive power:

  - Momentum alignment is by far the strongest signal -- +7.44% lift when
    aligned, -5.65% when against, z beyond 30, and robust across every
    moneyness band. It carries the SMALLEST weight in the system: +/-0.25.

  - IV percentile carries +/-1.0, four times momentum's weight, and is
    wrong for the strikes this scanner actually trades. Pooled, IV-cheap
    underperformed; stratified, the sign flips with moneyness (IV-rich
    beats IV-cheap by +3.2% near the money, loses by -3.2% far OTM),
    because cross-sectional IV percentile largely encodes the volatility
    smile. Candidates cluster near-to-mid money, so the rule is
    backwards where it is applied most.

  - Support levels score +0.5 for "supports this contract" but that tag
    was NEGATIVE in every moneyness band tested (-2.0%, -3.4%).
    Resistance, scored identically, was correctly signed.

These variants test whether correcting that mismatch separates gross
expectancy from zero.

WHY THEY LIVE HERE AND NOT IN config.py
---------------------------------------
A weighting under test must not be one edit away from silently becoming
the weighting that trades. Nothing in this module is read by scanner.py,
config.py or any live path -- variants reach the backtest only through
shadow.Policy.rescore, which live never sets.

AN HONEST LIMITATION
--------------------
Each variant adjusts the scanner's FINAL score by adding or removing a
component's nominal weight. That is an approximation: scanner.py caps its
structure score (STRUCTURE_SCORE_CAP) and scales level weights by a
magnitude factor, so removing a nominal 0.5 does not always undo exactly
what was added. The variants are therefore a directional test of "does
re-weighting toward the predictive component help", not an exact
reconstruction of a rewritten scanner. If one wins clearly, the change
belongs in scanner.py properly, re-measured there before it ever trades.
"""

import config


def _has(setup, needle: str) -> bool:
    return any(needle in r for r in setup.reasons)


def baseline(setup) -> float:
    """The scanner exactly as it runs today -- the control."""
    return setup.score


def momentum_weighted(setup) -> float:
    """
    Raise momentum alignment from +/-0.25 to +/-1.75, leaving everything
    else untouched. Isolates the single change the evidence most
    directly supports.
    """
    score = setup.score
    if _has(setup, "Momentum aligned"):
        score += 1.5
    if _has(setup, "Momentum against"):
        score -= 1.5
    return score


def iv_neutral(setup) -> float:
    """
    Strip the IV component out entirely rather than flipping it. The
    stratified result says the rule is wrong near the money and right far
    OTM, so a sign flip would just be wrong in the other half; removing
    it is the honest test of whether it contributes anything net.
    """
    score = setup.score
    if _has(setup, "IV cheap"):
        score -= config.IV_CHEAP_SCORE
    if _has(setup, "IV rich"):
        score -= config.IV_RICH_SCORE
    return score


def support_fixed(setup) -> float:
    """
    Remove the support-level "supports this contract" credit, which was
    negative in every band tested. Resistance is left alone -- it measured
    correctly signed, and changing it would confound the test.
    """
    score = setup.score
    if _has(setup, "Support level at this strike") and _has(setup, "supports this contract"):
        score -= config.LEVEL_SCORE_EACH
    return score


def combined(setup) -> float:
    """All three corrections together."""
    score = setup.score
    if _has(setup, "Momentum aligned"):
        score += 1.5
    if _has(setup, "Momentum against"):
        score -= 1.5
    if _has(setup, "IV cheap"):
        score -= config.IV_CHEAP_SCORE
    if _has(setup, "IV rich"):
        score -= config.IV_RICH_SCORE
    if _has(setup, "Support level at this strike") and _has(setup, "supports this contract"):
        score -= config.LEVEL_SCORE_EACH
    return score


def momentum_only(setup) -> float:
    """
    Momentum alignment alone, ignoring every other component.

    ADOPTED LIVE 2026-08-02 as config.SCORING_MODE == "momentum_only" --
    this is no longer just a reference point being tested against the
    others. Reuses the SAME config constants scanner.py's live override
    reads, so backtest and live cannot drift into scoring this
    differently from each other.
    """
    if _has(setup, "Momentum aligned"):
        return config.MOMENTUM_ONLY_ALIGNED_SCORE
    if _has(setup, "Momentum against"):
        return config.MOMENTUM_ONLY_AGAINST_SCORE
    return config.MOMENTUM_ONLY_NEUTRAL_SCORE


ALL = {
    "baseline": baseline,
    "momentum_weighted": momentum_weighted,
    "iv_neutral": iv_neutral,
    "support_fixed": support_fixed,
    "combined": combined,
    "momentum_only": momentum_only,
}
