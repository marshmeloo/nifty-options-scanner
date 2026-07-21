"""
Scanner: turns a MarketSnapshot into a list of flagged Setups.
Pure rule-based logic for now, deliberately simple and inspectable.
Swap in a model later if you want, but keep the reasons list, it's
what makes the output auditable instead of a black box.
"""

import config
from models import Setup
from price_action import levels_near_price

_KIND_LABELS = {
    "ob_bullish": "Bullish order block",
    "ob_bearish": "Bearish order block",
    "fvg_bullish": "Bullish FVG",
    "fvg_bearish": "Bearish FVG",
    "support": "Support level",
    "resistance": "Resistance level",
    "sweep_bullish": "Bullish liquidity sweep",
    "sweep_bearish": "Bearish liquidity sweep",
    "breakout_bullish": "Bullish breakout",
    "breakout_bearish": "Bearish breakout",
    "pullback_bullish": "Bullish pullback (retest)",
    "pullback_bearish": "Bearish pullback (retest)",
}


def scan(snapshot, price_levels=None, context=None) -> list:
    """
    price_levels: optional list of PriceLevel from price_action.analyze(),
    checked per-strike (OB/FVG/S-R/sweep/breakout/pullback confluence).

    context: optional MarketContext from price_action.build_context(),
    applied once per setup since trend/momentum/volume are chain-wide
    reads, not strike-specific.
    """
    setups = []

    for q in snapshot.chain:
        reasons = []
        score = 0.0

        # OI + price buildup classification (see dhan_source._classify_buildup).
        # Raw OI% alone can't tell you if buyers or writers are behind a
        # move — the premium's own direction is what disambiguates it.
        # This replaces a version that scored ANY large OI change as
        # positive, which meant a strike getting hammered by writers
        # (premium falling while OI surges — a bearish "short buildup")
        # was being scored identically to genuine buyer accumulation.
        buildup_labels = {
            "long_buildup": ("Long buildup (bullish for this contract)", config.LONG_BUILDUP_SCORE),
            "short_covering": ("Short covering (bullish for this contract)", config.SHORT_COVERING_SCORE),
            "short_buildup": ("Short buildup (bearish for this contract — writers piling in)", config.SHORT_BUILDUP_SCORE),
            "long_unwinding": ("Long unwinding (bearish for this contract — longs exiting)", config.LONG_UNWINDING_SCORE),
        }
        if q.buildup_type and q.buildup_type in buildup_labels:
            label, weight = buildup_labels[q.buildup_type]
            magnitude = min(abs(q.oi_change_pct) / config.OI_BUILDUP_PCT, 3.0)
            reasons.append(
                f"{label}: OI {q.oi_change_pct:+.1f}%, premium {q.price_change_pct:+.1f}%"
            )
            score += weight * magnitude
        elif abs(q.oi_change_pct) >= config.OI_BUILDUP_PCT:
            # OI moved enough to flag but no price baseline yet to classify
            # direction (typically the first cycle of the session for this
            # contract). Kept visible but deliberately NOT scored either way.
            direction = "buildup" if q.oi_change_pct > 0 else "unwinding"
            reasons.append(f"OI {direction}: {q.oi_change_pct:+.1f}% (unclassified, no price baseline yet)")

        # IV percentile
        if q.iv_percentile >= config.IV_PERCENTILE_HIGH:
            reasons.append(f"IV rich: {q.iv_percentile:.0f}th percentile")
            score += 1.0
        elif q.iv_percentile <= config.IV_PERCENTILE_LOW:
            reasons.append(f"IV cheap: {q.iv_percentile:.0f}th percentile")
            score += 1.0

        # PCR bias (applies to whole chain, but noted per-candidate for context)
        if snapshot.pcr >= config.PCR_BULLISH_ABOVE:
            reasons.append(f"Chain PCR bullish: {snapshot.pcr:.2f}")
        elif snapshot.pcr <= config.PCR_BEARISH_BELOW:
            reasons.append(f"Chain PCR bearish: {snapshot.pcr:.2f}")

        # Price vs VWAP
        vwap_dev_pct = ((snapshot.spot - snapshot.vwap) / snapshot.vwap) * 100
        if abs(vwap_dev_pct) >= config.VWAP_DEVIATION_PCT:
            direction = "above" if vwap_dev_pct > 0 else "below"
            reasons.append(f"Spot {direction} VWAP by {abs(vwap_dev_pct):.2f}%")
            score += 0.5

        # Price-action structure confluence (OB / FVG / S-R / sweeps)
        # Checked against THIS strike, not the underlying spot, so only
        # strikes actually sitting near a zone get flagged.
        if price_levels:
            nearby = levels_near_price(price_levels, q.strike)
            for lvl in nearby:
                label = _KIND_LABELS.get(lvl.kind, lvl.kind)
                reasons.append(f"{label} at this strike ({lvl.low:.1f}-{lvl.high:.1f})")
                score += 0.75 if "sweep" in lvl.kind else 0.5

        # Trend, momentum, and volume context (chain-wide, applied once per setup)
        if context:
            is_bullish_setup = q.option_type == "CE"

            # Trend continuation vs counter-trend
            if context.trend == "uptrend":
                if is_bullish_setup:
                    reasons.append(f"Trend continuation: CE with uptrend ({context.trend_note})")
                    score += 0.5
                else:
                    reasons.append(f"Counter-trend: PE against uptrend, caution")
                    score -= 0.5
            elif context.trend == "downtrend":
                if not is_bullish_setup:
                    reasons.append(f"Trend continuation: PE with downtrend ({context.trend_note})")
                    score += 0.5
                else:
                    reasons.append(f"Counter-trend: CE against downtrend, caution")
                    score -= 0.5

            # Momentum: RSI extremes flagged as reversal risk for the direction they oppose
            if context.rsi is not None:
                if context.rsi_state == "overbought" and is_bullish_setup:
                    reasons.append(f"RSI overbought ({context.rsi}): CE momentum may be exhausted")
                    score -= 0.25
                elif context.rsi_state == "oversold" and not is_bullish_setup:
                    reasons.append(f"RSI oversold ({context.rsi}): PE momentum may be exhausted")
                    score -= 0.25
                elif context.rsi_state == "oversold" and is_bullish_setup:
                    reasons.append(f"RSI oversold ({context.rsi}): possible bounce setup for CE")
                    score += 0.25
                elif context.rsi_state == "overbought" and not is_bullish_setup:
                    reasons.append(f"RSI overbought ({context.rsi}): possible pullback setup for PE")
                    score += 0.25

            # Momentum: ROC direction alignment
            if context.roc_pct is not None and abs(context.roc_pct) >= config.ROC_SIGNIFICANT_PCT:
                roc_bullish = context.roc_pct > 0
                if roc_bullish == is_bullish_setup:
                    reasons.append(f"Momentum aligned: {context.roc_pct:+.2f}% ROC supports this direction")
                    score += 0.25
                else:
                    reasons.append(f"Momentum against: {context.roc_pct:+.2f}% ROC opposes this direction")
                    score -= 0.25

            # Volume confirmation
            if context.volume_spike:
                reasons.append(f"Volume spike confirms move ({context.volume_ratio}x average)")
                score += 0.5

        if reasons:
            setups.append(
                Setup(
                    symbol=q.symbol,
                    strike=q.strike,
                    option_type=q.option_type,
                    expiry=q.expiry,
                    reasons=reasons,
                    score=round(score, 2),
                )
            )

    # Strongest signals first
    setups.sort(key=lambda s: s.score, reverse=True)
    return setups


def compute_market_bias(snapshot, context=None) -> tuple:
    """
    One composite top-down read (bullish/bearish/neutral) combining trend,
    momentum, and PCR. This is what was missing before: everything else in
    this scanner is bottom-up, per-option scoring with no sense of an
    overall market lean, which is exactly why CE and PE at the same strike
    could both get APPROVED with nothing telling you which one the day
    actually favors.

    Returns (label, score, reasons). label is "bullish" / "bearish" / "neutral".
    """
    score = 0.0
    reasons = []

    if context:
        if context.trend == "uptrend":
            score += 1.0
            reasons.append(f"Trend: {context.trend} ({context.trend_note})")
        elif context.trend == "downtrend":
            score -= 1.0
            reasons.append(f"Trend: {context.trend} ({context.trend_note})")

        if context.rsi is not None:
            if context.rsi >= 55:
                score += 0.5
            elif context.rsi <= 45:
                score -= 0.5

        if context.roc_pct is not None and abs(context.roc_pct) >= config.ROC_SIGNIFICANT_PCT:
            score += 0.5 if context.roc_pct > 0 else -0.5
            reasons.append(f"ROC {context.roc_pct:+.2f}%")

    if snapshot.pcr >= config.PCR_BULLISH_ABOVE:
        score += 0.5
        reasons.append(f"PCR {snapshot.pcr:.2f} (bullish)")
    elif snapshot.pcr <= config.PCR_BEARISH_BELOW:
        score -= 0.5
        reasons.append(f"PCR {snapshot.pcr:.2f} (bearish)")

    if score >= 1.0:
        label = "bullish"
    elif score <= -1.0:
        label = "bearish"
    else:
        label = "neutral/range"

    return label, round(score, 2), reasons


def tag_bias_conflicts(results) -> None:
    """
    results: list of (Setup, TradePlan, RiskVerdict) tuples for one cycle,
    already decided. Mutates verdict.reasons IN PLACE to flag strikes where
    both CE and PE were independently APPROVED — the exact confusion this
    was built to catch. Doesn't change the decision itself, just makes the
    ambiguity visible instead of silent.
    """
    approved_sides_by_strike = {}
    for setup, _plan, verdict in results:
        if verdict.decision == "APPROVED":
            approved_sides_by_strike.setdefault(setup.strike, set()).add(setup.option_type)

    for setup, _plan, verdict in results:
        if verdict.decision != "APPROVED":
            continue
        opposite = "PE" if setup.option_type == "CE" else "CE"
        if opposite in approved_sides_by_strike.get(setup.strike, set()):
            verdict.reasons.append(
                f"Two-sided: both CE and PE approved at {setup.strike} — "
                f"treat as a high-interest level, not a directional call on its own"
            )
