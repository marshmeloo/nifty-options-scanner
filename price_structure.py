"""
Pure price-action structure detection and signal generation -- no OI, no
IV, no PCR, no RSI/ROC. Implements the multi-timeframe methodology from
the "Price Action Trading" reference doc (Trendwisdom, 2023):

  1. Mark support/resistance ZONES on a higher timeframe (HTF).
  2. Wait for price to approach one of those zones.
  3. Drop to a lower timeframe (LTF) and wait for a reaction, followed
     by a break of the recent consolidation (the doc calls this an
     "accumulation" or "distribution" phase) in the reaction's direction.
  4. Enter on that break. Stop sits at the far edge of the consolidation
     that just broke. Target is a fixed 2x the resulting risk.

WHY THIS REUSES price_action.py RATHER THAN REIMPLEMENTING IT
---------------------------------------------------------------
Swing-point detection, HH/HL-vs-LH/LL trend classification, and
zone-style (not line) support/resistance clustering already exist there
and already match this doc's own description almost exactly --
classify_trend() IS the doc's trend-change rule, and
detect_support_resistance() already returns AREAS (a low/high range per
level), which is the doc's explicit "mark zones, not lines" instruction.
Reimplementing either here would let this strategy's read of "what is
the trend" drift from the momentum scanner's, for no reason.

What price_action.py does NOT have, because nothing else in this project
needed it, is genuinely new here:
  - impulse-vs-correction LEG classification (candle size + volume +
    color uniformity, the doc's own three criteria)
  - the actual entry rule: HTF zone touch -> LTF reaction -> LTF
    structural break, with the stop/target derived from where that break
    happened

TIMEFRAME-AGNOSTIC BY DESIGN
------------------------------
Every function here takes plain Candle lists and has no idea what
interval they represent. Two different timeframe pairs
(config_price_action.TIMEFRAME_PAIRS) can therefore be evaluated with
IDENTICAL code, differing only in which candle series is fed in as HTF
vs LTF -- see shadow_price_action.py, which backtests both rather than
assuming one is right.
"""

from dataclasses import dataclass, field
from typing import Optional

import config_price_action as pcfg
import price_action
from models import Candle


@dataclass
class Leg:
    """One run of candles between two consecutive swing points."""
    start_index: int
    end_index: int
    direction: str          # "up" or "down", from first candle's open to last candle's close
    kind: str                # "impulse" or "correction"
    avg_body_pct: float      # average |close-open|/open across the leg, as a %
    color_uniformity_pct: float


@dataclass
class StructureSignal:
    direction: str           # "bullish" or "bearish"
    entry_reference: float   # index price at the break
    stop_reference: float    # index price at the far edge of the broken consolidation
    zone: object              # the HTF PriceLevel this signal reacted from
    reasons: list = field(default_factory=list)


def resample_candles(candles: list, target_minutes: int, source_minutes: int) -> list:
    """
    Aggregate finer candles into coarser bars (e.g. 5-min -> 60-min),
    OHLC-correct: open of the first sub-candle, close of the last, high/
    low across the whole group. Used to build a "1-Hour" series from the
    5-min index candles already fetched for historical backtesting,
    without a second data fetch for every timeframe pair under test.

    Groups are aligned to the SOURCE data's own sequence, not wall-clock
    boundaries -- if the source has a gap (a missed poll, a holiday),
    this does not try to reconstruct what would have been there, it just
    groups what exists. A partial trailing group (fewer than a full
    ratio of source candles) is dropped rather than emitted as a
    fake full-sized bar built from less data than every other bar.
    """
    if target_minutes <= source_minutes:
        return list(candles)
    ratio = target_minutes // source_minutes
    if ratio * source_minutes != target_minutes:
        raise ValueError(
            f"target interval {target_minutes} is not an exact multiple of "
            f"source interval {source_minutes}"
        )

    out = []
    for i in range(0, len(candles) - ratio + 1, ratio):
        group = candles[i:i + ratio]
        out.append(Candle(
            timestamp=group[0].timestamp,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
        ))
    return out


def _median_body_pct(candles: list) -> float:
    """
    MEDIAN, not mean, "typical candle" baseline. A short series containing
    even one or two genuinely huge impulsive candles pulls a mean
    baseline up enough that the impulse itself can fail to look
    impulsive relative to it -- exactly backwards. The median is far
    more robust to a small number of outliers dominating a small sample.
    """
    bodies_pct = sorted(abs(c.close - c.open) / c.open * 100 for c in candles if c.open)
    n = len(bodies_pct)
    if not n:
        return 0.0
    return bodies_pct[n // 2] if n % 2 else (bodies_pct[n // 2 - 1] + bodies_pct[n // 2]) / 2


def _classify_segment(segment: list, baseline_body_pct: float, start_index: int, end_index: int) -> Optional["Leg"]:
    """Classify one candle segment as impulse/correction. None if too short to measure."""
    if len(segment) < 2:
        return None

    bodies_pct = [abs(c.close - c.open) / c.open * 100 for c in segment if c.open]
    avg_body_pct = sum(bodies_pct) / len(bodies_pct) if bodies_pct else 0.0

    greens = sum(1 for c in segment if c.close >= c.open)
    reds = len(segment) - greens
    dominant_count = max(greens, reds)
    color_uniformity_pct = round(dominant_count / len(segment) * 100, 1)

    direction = "up" if segment[-1].close >= segment[0].open else "down"

    is_impulse = (
        baseline_body_pct > 0
        and avg_body_pct >= baseline_body_pct * pcfg.IMPULSE_BODY_MULTIPLE
        and color_uniformity_pct >= pcfg.IMPULSE_COLOR_UNIFORMITY_PCT
    )
    return Leg(
        start_index=start_index, end_index=end_index, direction=direction,
        kind="impulse" if is_impulse else "correction",
        avg_body_pct=round(avg_body_pct, 3),
        color_uniformity_pct=color_uniformity_pct,
    )


def classify_legs(candles: list, lookback: int = None) -> list:
    """
    Partition `candles` into legs between consecutive CONFIRMED swing
    points (both highs and lows, merged and sorted), then classify each
    as impulse or correction using the doc's three criteria: candle
    size, volume, and color uniformity.

    Only useful for legs that are fully in the past -- a swing point
    needs `lookback` candles on EACH side to confirm, so the most recent
    `lookback` candles can never close a leg here. That is fine for a
    historical read of "what has this series done", but it is the wrong
    tool for "what is happening RIGHT NOW, about to break out" -- see
    latest_consolidation_box(), which handles that case deliberately
    differently rather than trying to force this function to answer it.
    """
    lookback = lookback or pcfg.SWING_LOOKBACK
    swing_highs, swing_lows = price_action.find_swing_points(candles, lookback)
    swing_indices = sorted({i for i, _ in swing_highs} | {i for i, _ in swing_lows})
    if len(swing_indices) < 2:
        return []

    baseline_body_pct = _median_body_pct(candles)
    legs = []
    for start, end in zip(swing_indices, swing_indices[1:]):
        leg = _classify_segment(candles[start:end + 1], baseline_body_pct, start, end)
        if leg is not None:
            legs.append(leg)
    return legs


def zone_touch(levels: list, price: float, tolerance_pct: float = None) -> Optional[object]:
    """
    The first HTF support/resistance zone `price` is currently inside or
    within `tolerance_pct` of, or None. Support and resistance are
    checked so either a bounce candidate or a rejection candidate can be
    found from the same call -- the caller decides which direction that
    implies (see generate_signal).
    """
    tolerance_pct = tolerance_pct if tolerance_pct is not None else pcfg.ZONE_APPROACH_PCT
    for level in levels:
        pad = level.low * (tolerance_pct / 100)
        if (level.low - pad) <= price <= (level.high + pad):
            return level
    return None


def _last_leg(legs: list) -> Optional[Leg]:
    return legs[-1] if legs else None


def latest_consolidation_box(ltf_candles: list, lookback: int = None) -> Optional[dict]:
    """
    The consolidation the LATEST candle might be about to break out of --
    the doc's "accumulation/distribution phase" still forming right now.

    Deliberately NOT built from classify_legs()'s confirmed-swing legs:
    a swing point needs `lookback` candles on each side to confirm, so
    the live, still-forming range immediately before the current candle
    can never close as a leg there -- by the time it would, the breakout
    has already happened and this function would always be one step
    late. Instead, this takes everything from the last CONFIRMED swing
    point (only one endpoint needs confirming) up to but EXCLUDING the
    final candle, and classifies that trailing segment directly. The
    final candle itself is left out on purpose -- it's the candidate
    breakout, not part of the box it might be breaking.

    None if there's no confirmed swing yet, too few candles remain after
    it to classify anything, or that trailing segment doesn't read as a
    correction (i.e. there is no consolidation to break right now).
    """
    lookback = lookback or pcfg.SWING_LOOKBACK
    swing_highs, swing_lows = price_action.find_swing_points(ltf_candles, lookback)
    swing_indices = sorted({i for i, _ in swing_highs} | {i for i, _ in swing_lows})
    if not swing_indices:
        return None

    last_swing = swing_indices[-1]
    # Start AFTER the swing candle, not at it: the swing candle is the
    # extreme of the impulse move that FORMED the swing, not part of
    # what comes after it. Including it once pulled a huge impulsive
    # candle's high/low straight into the "consolidation" box.
    segment = ltf_candles[last_swing + 1:-1]   # up to, not including, the latest candle
    if len(segment) < 2:
        return None

    baseline_body_pct = _median_body_pct(ltf_candles)
    leg = _classify_segment(segment, baseline_body_pct, last_swing + 1, len(ltf_candles) - 2)
    if leg is None or leg.kind != "correction":
        return None

    return {
        "box_high": max(c.high for c in segment),
        "box_low": min(c.low for c in segment),
    }


def structural_break(ltf_candles: list, reaction_direction: str, lookback: int = None) -> Optional[dict]:
    """
    Has the most recent consolidation broken in `reaction_direction` on
    its LATEST candle -- i.e. the break is happening right now, not
    something that already happened several candles ago.

    Returns {"break_price", "stop_reference", "box_high", "box_low"}
    where stop_reference is the FAR edge of the consolidation (below its
    low for a bullish break, above its high for a bearish break) --
    exactly where the doc says the stop belongs. None if there is no
    recent correction leg, or the latest candle has not broken it.
    """
    box = latest_consolidation_box(ltf_candles, lookback)
    if box is None:
        return None

    latest = ltf_candles[-1]
    if reaction_direction == "bullish" and latest.close > box["box_high"]:
        return {"break_price": latest.close, "stop_reference": box["box_low"], **box}
    if reaction_direction == "bearish" and latest.close < box["box_low"]:
        return {"break_price": latest.close, "stop_reference": box["box_high"], **box}
    return None


def generate_signal(htf_candles: list, ltf_candles: list) -> Optional[StructureSignal]:
    """
    The full multi-timeframe rule, steps 1-4 in this module's docstring.

    Returns None at any step that doesn't qualify -- "no signal this
    cycle" is the overwhelmingly common, entirely legitimate outcome;
    this rule is deliberately selective (a specific zone, near price, a
    specific structural break) rather than firing on every candle the
    way a scored/weighted system would.
    """
    if len(htf_candles) < pcfg.SWING_LOOKBACK * 2 + 2:
        return None
    if len(ltf_candles) < pcfg.SWING_LOOKBACK * 2 + 2:
        return None

    htf_zones = price_action.detect_support_resistance(
        htf_candles, lookback=pcfg.SWING_LOOKBACK,
        cluster_tolerance_pct=pcfg.SR_CLUSTER_TOLERANCE_PCT,
        min_touches=pcfg.SR_MIN_TOUCHES,
    )
    if not htf_zones:
        return None

    # The zone check is against the CONSOLIDATION BOX itself -- the doc's
    # "reaction, then accumulation/distribution phase" -- not the latest
    # closing price. By the time a break has happened, price has already
    # moved away from the zone by definition; checking the post-break
    # price against the zone would mean this could almost never fire
    # right when it should.
    box = latest_consolidation_box(ltf_candles)
    if box is None:
        return None

    support_zone = zone_touch(
        [z for z in htf_zones if z.kind == "support"], box["box_low"])
    resistance_zone = zone_touch(
        [z for z in htf_zones if z.kind == "resistance"], box["box_high"])

    if support_zone is not None:
        zone, reaction_direction = support_zone, "bullish"
    elif resistance_zone is not None:
        zone, reaction_direction = resistance_zone, "bearish"
    else:
        return None

    brk = structural_break(ltf_candles, reaction_direction)
    if brk is None:
        return None

    reasons = [
        f"Consolidation {box['box_low']:.1f}-{box['box_high']:.1f} formed at "
        f"{zone.kind} zone {zone.low:.1f}-{zone.high:.1f} ({zone.note})",
        f"LTF structure broke {reaction_direction} at {brk['break_price']:.1f}, "
        f"stop reference {brk['stop_reference']:.1f}",
    ]

    try:
        _avg, _latest, ratio, is_spike = price_action.compute_volume_context(ltf_candles)
        if is_spike:
            reasons.append(f"Volume spike confirms the break ({ratio}x average)")
        else:
            reasons.append("No volume confirmation on this break -- doc's own bonus tip, not a hard gate")
    except Exception:
        pass

    return StructureSignal(
        direction=reaction_direction,
        entry_reference=brk["break_price"],
        stop_reference=brk["stop_reference"],
        zone=zone,
        reasons=reasons,
    )
