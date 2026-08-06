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


def _is_liquid_enough(q) -> bool:
    """
    Can this contract realistically be traded at anything near its quoted
    price? Checks open interest, today's volume, and bid-ask spread.

    Each check is skipped when its config value is None, or when the data
    isn't available (spread needs a book, which the CSV and TradingView
    tiers don't provide) -- a missing input must not silently reject the
    whole chain and leave the scanner finding nothing.
    """
    min_oi = getattr(config, "MIN_OI_TO_TRADE", None)
    if min_oi and (q.oi or 0) < min_oi:
        return False

    min_vol = getattr(config, "MIN_VOLUME_TO_TRADE", None)
    if min_vol and (q.volume or 0) < min_vol:
        return False

    max_spread = getattr(config, "MAX_SPREAD_PCT", None)
    if max_spread is not None:
        spread = getattr(q, "spread_pct", None)
        if spread is not None and spread > max_spread:
            return False
    return True


def _level_direction(kind: str) -> str:
    """"bullish" / "bearish" / "neutral" reading of a PriceLevel kind."""
    if kind.endswith("_bullish"):
        return "bullish"
    if kind.endswith("_bearish"):
        return "bearish"
    # Support is a floor (bullish); resistance is a ceiling (bearish).
    if kind == "support":
        return "bullish"
    if kind == "resistance":
        return "bearish"
    return "neutral"


def _score_levels(nearby, option_type: str) -> tuple:
    """
    Turn the structural levels near a strike into ONE netted, capped,
    direction-aware score contribution. Returns (score, reasons).

    Two things this fixes, both seen in the 2026-07-28 24050 PE trades:

    1. DIRECTION. The old code added a flat positive score for EVERY
       nearby level regardless of which way it pointed -- so a *bullish*
       FVG raised the conviction on a PE (a bearish instrument). A level
       that argues against the contract should count against it.

    2. CONTRADICTION. Overlapping bull and bear levels each scored
       positive, so a chopping market that printed both a bullish and a
       bearish FVG across the same price zone read as DOUBLE confluence
       when it actually means the opposite -- no one is in control there.
       Netting them means genuine one-sided structure still scores, while
       two-sided noise cancels toward zero.

    The net is then capped by MAX_LEVEL_SCORE_CONTRIBUTION so structure
    can support a thesis but never dominate the score on count alone.
    """
    favours = "bullish" if option_type == "CE" else "bearish"
    opposes = "bearish" if option_type == "CE" else "bullish"

    raw = 0.0
    reasons = []
    for lvl in nearby:
        weight = config.SWEEP_SCORE_EACH if "sweep" in lvl.kind else config.LEVEL_SCORE_EACH
        direction = _level_direction(lvl.kind)
        label = _KIND_LABELS.get(lvl.kind, lvl.kind)

        if direction == favours:
            raw += weight
            note = "supports this contract"
        elif direction == opposes:
            raw -= weight
            note = "argues AGAINST this contract"
        else:
            note = "neutral"

        reasons.append(f"{label} at this strike ({lvl.low:.1f}-{lvl.high:.1f}) -- {note}")

    cap = config.MAX_LEVEL_SCORE_CONTRIBUTION
    capped = max(-cap, min(cap, raw))
    if capped != raw:
        reasons.append(
            f"Structure score capped at {capped:+.2f} (raw {raw:+.2f} from "
            f"{len(nearby)} levels) -- confluence count alone shouldn't dominate conviction"
        )
    return round(capped, 2), reasons


def _tiebreak_value(quote, snapshot, chain_index: int) -> float:
    """
    Secondary sort key for candidates whose SCORES ARE EQUAL. Lower sorts
    first (i.e. gets picked). See config.TIEBREAK_MODE for why this is an
    explicit setting rather than whatever chain order happens to produce.

    Never affects ranking between candidates with different scores --
    it's strictly a tie-break, so a genuinely stronger signal always
    still wins on score alone.
    """
    mode = getattr(config, "TIEBREAK_MODE", "chain_order")

    if mode == "nearest_atm":
        return abs(quote.strike - snapshot.spot)

    if mode == "highest_oi_change":
        # Negated so the LARGEST buildup sorts first. Uses the intraday
        # figure when available for the same reason scanner scoring does
        # (the daily one only grows through the session and saturates);
        # falls back to the daily figure in the opening minutes.
        oi_change = quote.oi_change_pct_intraday
        if oi_change is None:
            oi_change = quote.oi_change_pct
        return -abs(oi_change or 0.0)

    return chain_index


def scan(snapshot, price_levels=None, context=None) -> list:
    """
    price_levels: optional list of PriceLevel from price_action.analyze(),
    checked per-strike (OB/FVG/S-R/sweep/breakout/pullback confluence).

    context: optional MarketContext from price_action.build_context(),
    applied once per setup since trend/momentum/volume are chain-wide
    reads, not strike-specific.
    """
    setups = []
    # (tiebreak_value) parallel to `setups`, so the sort below can break
    # score ties deliberately instead of leaving it to chain order.
    tiebreaks = []

    for chain_index, q in enumerate(snapshot.chain):
        # Premium filter lives HERE, not in the data-source chain builder:
        # this only gates whether a strike is worth flagging as a NEW
        # candidate. It must not remove strikes from the snapshot itself,
        # since trade_tracker.update_open_trades() needs to find an
        # already-open trade's quote even once its premium has moved well
        # outside this band -- which is normal as a position runs toward
        # its target. See dhan_source.py's chain-building comment for the
        # bug this used to cause when the filter was applied too early.
        if getattr(config, "PREMIUM_MIN", None) is not None and q.ltp < config.PREMIUM_MIN:
            continue
        if getattr(config, "PREMIUM_MAX", None) is not None and q.ltp > config.PREMIUM_MAX:
            continue
        # Liquidity screen, same placement and for the same reason as the
        # premium filter above -- candidate selection only, never the
        # chain itself. A tradeable premium is not a tradeable contract:
        # without this a strike showing a plausible price on a handful of
        # lots gets planned, sized and journalled at a fill that could
        # not have happened.
        if not _is_liquid_enough(q):
            continue

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
            # Score the magnitude of the move the classification was
            # actually made on. The daily figure is measured against
            # YESTERDAY's close, so it only grows through the session and
            # saturates the 3.0 cap almost immediately (anything past a
            # 45% day-cumulative change) -- which made the multiplier a
            # near-constant rather than a measure of conviction.
            if q.oi_change_pct_intraday is not None:
                magnitude = min(abs(q.oi_change_pct_intraday) / config.OI_INTRADAY_BUILDUP_PCT, 3.0)
                reasons.append(
                    f"{label}: OI {q.oi_change_pct_intraday:+.1f}%, premium "
                    f"{q.price_change_pct_intraday:+.1f}% over the last "
                    f"{q.buildup_window_minutes:g} min (OI {q.oi_change_pct:+.1f}% vs prev close)"
                )
            else:
                magnitude = min(abs(q.oi_change_pct) / config.OI_BUILDUP_PCT, 3.0)
                # price_change_pct is normally present whenever a buildup
                # was classified (the classifier requires it), but it is
                # an Optional field and a source could legally populate
                # buildup_type without it. Formatting None here would
                # raise, and scan() runs inside main_live's per-cycle
                # try/except -- so a display concern would silently cost
                # an entire decision cycle. Not worth the risk.
                premium_note = (
                    f"{q.price_change_pct:+.1f}%" if q.price_change_pct is not None else "n/a"
                )
                reasons.append(
                    f"{label}: OI {q.oi_change_pct:+.1f}%, premium {premium_note} "
                    f"(vs previous close -- not enough intraday history yet)"
                )
            score += weight * magnitude
        elif abs(q.oi_change_pct) >= config.OI_BUILDUP_PCT:
            # OI moved enough to flag but no price baseline yet to classify
            # direction (typically the first cycle of the session for this
            # contract). Kept visible but deliberately NOT scored either way.
            direction = "buildup" if q.oi_change_pct > 0 else "unwinding"
            reasons.append(f"OI {direction}: {q.oi_change_pct:+.1f}% (unclassified, no price baseline yet)")

        # IV percentile. Asymmetric on purpose: this scanner only buys
        # premium, so rich IV is a cost, not a signal. See config's
        # IV_CHEAP_SCORE / IV_RICH_SCORE for the full reasoning.
        if q.iv_percentile >= config.IV_PERCENTILE_HIGH:
            reasons.append(
                f"IV rich: {q.iv_percentile:.0f}th percentile "
                f"(costly to BUY -- vega works against a long-premium position)"
            )
            score += config.IV_RICH_SCORE
        elif q.iv_percentile <= config.IV_PERCENTILE_LOW:
            reasons.append(f"IV cheap: {q.iv_percentile:.0f}th percentile")
            score += config.IV_CHEAP_SCORE

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
            level_score, level_reasons = _score_levels(nearby, q.option_type)
            reasons.extend(level_reasons)
            score += level_score

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
            final_score = score
            if config.SCORING_MODE == "momentum_only":
                # See config.SCORING_MODE's docstring for the evidence.
                # `reasons` is left untouched -- apply_learned_adjustment
                # and the decision log still need the real component
                # breakdown, this only changes what RANKS and CLEARS the
                # conviction bar.
                if any(r.startswith("Momentum aligned") for r in reasons):
                    final_score = config.MOMENTUM_ONLY_ALIGNED_SCORE
                elif any(r.startswith("Momentum against") for r in reasons):
                    final_score = config.MOMENTUM_ONLY_AGAINST_SCORE
                else:
                    final_score = config.MOMENTUM_ONLY_NEUTRAL_SCORE

            setups.append(
                Setup(
                    symbol=q.symbol,
                    strike=q.strike,
                    option_type=q.option_type,
                    expiry=q.expiry,
                    reasons=reasons,
                    score=round(final_score, 2),
                )
            )
            tiebreaks.append(_tiebreak_value(q, snapshot, chain_index))

    # Strongest signals first; ties broken by config.TIEBREAK_MODE rather
    # than by whatever order the chain happened to arrive in.
    order = sorted(range(len(setups)), key=lambda i: (-setups[i].score, tiebreaks[i]))
    return [setups[i] for i in order]


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


def bias_conflict(option_type: str, bias_label: str, bias_score: float) -> bool:
    """
    Does this contract bet against a STRONG top-down bias?

    A CE is a bullish bet, a PE a bearish one. Only acts when the bias is
    strong enough to be worth respecting (BIAS_STRONG_THRESHOLD) --
    "neutral/range" gates nothing, since in a range neither side is
    fighting the tape.
    """
    if bias_label is None or bias_score is None:
        return False
    if abs(bias_score) < getattr(config, "BIAS_STRONG_THRESHOLD", 1.5):
        return False
    if bias_label == "bullish" and option_type == "PE":
        return True
    if bias_label == "bearish" and option_type == "CE":
        return True
    return False


def apply_bias_gate(setup, bias_label: str, bias_score: float) -> tuple:
    """
    Returns (blocked, score_penalty, note).

    Called at decision time rather than inside scan(), so the raw score
    stays a pure read of the contract's own evidence and the bias
    adjustment is visible as a separate, auditable step -- the same
    reasoning behind keeping the learned tag adjustment out of the raw
    score.
    """
    mode = getattr(config, "BIAS_GATING_MODE", "off")
    if mode == "off" or not bias_conflict(setup.option_type, bias_label, bias_score):
        return False, 0.0, None

    side = "bullish" if setup.option_type == "CE" else "bearish"
    note = (
        f"{setup.option_type} is a {side} bet against a {bias_label} market bias "
        f"(score {bias_score:+.2f})"
    )
    if mode == "block":
        return True, 0.0, note + " -- blocked by BIAS_GATING_MODE=block"
    penalty = getattr(config, "BIAS_CONFLICT_PENALTY", 1.0)
    return False, penalty, note + f" -- score reduced by {penalty}"


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
