"""
Price-action / Smart-Money-Concepts-style structure detection, operating
on a plain list of Candle objects (any timeframe you feed it).

Covers:
  - Fair Value Gaps (FVG): 3-candle imbalance where price left a gap
  - Order Blocks (OB): the last opposite-direction candle before a strong
    directional move, treated as a zone of institutional interest
  - Support / Resistance: clusters of swing highs/lows touched multiple times
  - Liquidity sweeps: a wick pokes through a prior swing level then closes
    back inside it (a stop-hunt pattern)

This is standard retail-SMC style pattern-matching, not a proprietary edge.
Treat it as one more layer of context alongside the option-chain scanner,
not a standalone signal.
"""

import config
from models import PriceLevel, MarketContext


def _pct(a, b):
    return abs(a - b) / b * 100 if b else 0.0


def find_swing_points(candles, lookback=None):
    """
    Returns (swing_highs, swing_lows) as lists of (index, price) tuples.
    A swing high/low is a local extremum over `lookback` candles each side.
    """
    lookback = lookback or config.SWING_LOOKBACK
    swing_highs, swing_lows = [], []

    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback : i + lookback + 1]
        if candles[i].high == max(c.high for c in window):
            swing_highs.append((i, candles[i].high))
        if candles[i].low == min(c.low for c in window):
            swing_lows.append((i, candles[i].low))

    return swing_highs, swing_lows


def detect_fair_value_gaps(candles) -> list:
    """3-candle imbalance: gap between candle[i-1] and candle[i+1]."""
    levels = []
    for i in range(1, len(candles) - 1):
        prev_c, next_c = candles[i - 1], candles[i + 1]

        if prev_c.high < next_c.low:  # bullish FVG (gap up)
            levels.append(
                PriceLevel(
                    kind="fvg_bullish",
                    low=prev_c.high,
                    high=next_c.low,
                    note=f"Bullish FVG between candles at {prev_c.timestamp} and {next_c.timestamp}",
                    strength=_pct(next_c.low, prev_c.high),
                )
            )
        elif prev_c.low > next_c.high:  # bearish FVG (gap down)
            levels.append(
                PriceLevel(
                    kind="fvg_bearish",
                    low=next_c.high,
                    high=prev_c.low,
                    note=f"Bearish FVG between candles at {prev_c.timestamp} and {next_c.timestamp}",
                    strength=_pct(prev_c.low, next_c.high),
                )
            )
    return levels


def detect_order_blocks(candles) -> list:
    """
    Last opposite-direction candle before a move that qualifies as
    significant (>= OB_MIN_MOVE_PCT away from that candle's close).
    """
    levels = []
    for i in range(len(candles) - 1):
        c = candles[i]
        move_pct = _pct(candles[i + 1].close, c.close)
        if move_pct < config.OB_MIN_MOVE_PCT:
            continue

        is_down_candle = c.close < c.open
        is_up_candle = c.close > c.open
        move_up = candles[i + 1].close > c.close

        if is_down_candle and move_up:
            levels.append(
                PriceLevel(
                    kind="ob_bullish",
                    low=c.low,
                    high=c.open,
                    note=f"Bullish OB at {c.timestamp}, preceded a {move_pct:.2f}% up move",
                    strength=move_pct,
                )
            )
        elif is_up_candle and not move_up:
            levels.append(
                PriceLevel(
                    kind="ob_bearish",
                    low=c.open,
                    high=c.high,
                    note=f"Bearish OB at {c.timestamp}, preceded a {move_pct:.2f}% down move",
                    strength=move_pct,
                )
            )
    return levels


def detect_support_resistance(candles) -> list:
    """Cluster swing highs into resistance, swing lows into support."""
    swing_highs, swing_lows = find_swing_points(candles)
    levels = []

    for label, kind, points in (
        ("resistance", "resistance", swing_highs),
        ("support", "support", swing_lows),
    ):
        prices = sorted(p for _, p in points)
        clusters = []
        for p in prices:
            placed = False
            for cluster in clusters:
                if _pct(p, cluster["mean"]) <= config.SR_CLUSTER_TOLERANCE_PCT:
                    cluster["prices"].append(p)
                    cluster["mean"] = sum(cluster["prices"]) / len(cluster["prices"])
                    placed = True
                    break
            if not placed:
                clusters.append({"prices": [p], "mean": p})

        for cluster in clusters:
            touches = len(cluster["prices"])
            if touches >= config.SR_MIN_TOUCHES:
                levels.append(
                    PriceLevel(
                        kind=kind,
                        low=min(cluster["prices"]),
                        high=max(cluster["prices"]),
                        note=f"{label.capitalize()} level touched {touches}x around {cluster['mean']:.1f}",
                        strength=float(touches),
                    )
                )
    return levels


def detect_liquidity_sweeps(candles) -> list:
    """
    A candle wicks beyond a prior swing high/low by at least
    SWEEP_WICK_MIN_PCT, then closes back inside it. Classic stop-hunt.
    """
    swing_highs, swing_lows = find_swing_points(candles)
    levels = []

    for i, c in enumerate(candles):
        prior_highs = [p for idx, p in swing_highs if idx < i]
        prior_lows = [p for idx, p in swing_lows if idx < i]

        if prior_highs:
            level = max(prior_highs)
            wick_pct = _pct(c.high, level)
            if c.high > level and c.close < level and wick_pct >= config.SWEEP_WICK_MIN_PCT:
                levels.append(
                    PriceLevel(
                        kind="sweep_bearish",
                        low=level,
                        high=c.high,
                        note=f"Swept resistance {level:.1f} then closed back below at {c.timestamp}",
                        strength=wick_pct,
                    )
                )

        if prior_lows:
            level = min(prior_lows)
            wick_pct = _pct(c.low, level)
            if c.low < level and c.close > level and wick_pct >= config.SWEEP_WICK_MIN_PCT:
                levels.append(
                    PriceLevel(
                        kind="sweep_bullish",
                        low=c.low,
                        high=level,
                        note=f"Swept support {level:.1f} then closed back above at {c.timestamp}",
                        strength=wick_pct,
                    )
                )
    return levels


def classify_trend(candles, lookback=None) -> tuple:
    """
    Classifies trend from the sequence of recent swing highs/lows:
    higher-highs + higher-lows = uptrend, lower-highs + lower-lows =
    downtrend, anything mixed = range. Returns (direction, note).
    """
    n = lookback or config.TREND_SWING_LOOKBACK
    swing_highs, swing_lows = find_swing_points(candles)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "range", "Not enough swing points yet to read a trend"

    recent_highs = [p for _, p in swing_highs[-n:]]
    recent_lows = [p for _, p in swing_lows[-n:]]

    highs_rising = all(recent_highs[i] < recent_highs[i + 1] for i in range(len(recent_highs) - 1))
    lows_rising = all(recent_lows[i] < recent_lows[i + 1] for i in range(len(recent_lows) - 1))
    highs_falling = all(recent_highs[i] > recent_highs[i + 1] for i in range(len(recent_highs) - 1))
    lows_falling = all(recent_lows[i] > recent_lows[i + 1] for i in range(len(recent_lows) - 1))

    if highs_rising and lows_rising:
        return "uptrend", f"Higher highs and higher lows over last {len(recent_highs)} swings"
    if highs_falling and lows_falling:
        return "downtrend", f"Lower highs and lower lows over last {len(recent_highs)} swings"
    return "range", "Swing highs/lows not consistently rising or falling"


def compute_rsi(candles, period=None) -> float:
    """Wilder's RSI on candle closes. Returns None if not enough data."""
    period = period or config.RSI_PERIOD
    closes = [c.close for c in candles]
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def compute_roc(candles, period=None) -> float:
    """Rate of change (%) over `period` candles. None if not enough data."""
    period = period or config.ROC_PERIOD
    if len(candles) < period + 1:
        return None
    old_close = candles[-period - 1].close
    new_close = candles[-1].close
    if old_close == 0:
        return None
    return round((new_close - old_close) / old_close * 100, 2)


def compute_volume_context(candles, period=None) -> tuple:
    """Returns (avg_volume, latest_volume, ratio, is_spike)."""
    period = period or config.VOLUME_MA_PERIOD
    if len(candles) < period + 1:
        return None, None, None, False

    recent = candles[-period - 1 : -1]  # window excluding the latest candle
    avg_volume = sum(c.volume for c in recent) / len(recent)
    latest_volume = candles[-1].volume
    ratio = round(latest_volume / avg_volume, 2) if avg_volume else None
    is_spike = ratio is not None and ratio >= config.VOLUME_SPIKE_MULTIPLE
    return avg_volume, latest_volume, ratio, is_spike


def build_context(candles) -> MarketContext:
    """One combined chain-wide read: trend + momentum + volume."""
    trend, trend_note = classify_trend(candles)
    rsi = compute_rsi(candles)
    roc = compute_roc(candles)
    _, _, vol_ratio, vol_spike = compute_volume_context(candles)

    rsi_state = "neutral"
    if rsi is not None:
        if rsi >= config.RSI_OVERBOUGHT:
            rsi_state = "overbought"
        elif rsi <= config.RSI_OVERSOLD:
            rsi_state = "oversold"

    return MarketContext(
        trend=trend,
        trend_note=trend_note,
        rsi=rsi,
        rsi_state=rsi_state,
        roc_pct=roc,
        volume_ratio=vol_ratio,
        volume_spike=vol_spike,
    )


def detect_breakouts(candles, sr_levels) -> list:
    """
    A candle closes beyond a support/resistance level by at least
    BREAKOUT_CONFIRM_PCT. Returns PriceLevel entries anchored at the
    broken level itself (useful for confluence with strikes near it).
    """
    levels = []
    for lvl in sr_levels:
        for c in candles:
            if lvl.kind == "resistance" and c.close > lvl.high:
                move_pct = _pct(c.close, lvl.high)
                if move_pct >= config.BREAKOUT_CONFIRM_PCT:
                    levels.append(
                        PriceLevel(
                            kind="breakout_bullish",
                            low=lvl.low,
                            high=lvl.high,
                            note=f"Broke above resistance {lvl.high:.1f} at {c.timestamp}",
                            strength=move_pct,
                        )
                    )
                    break  # one breakout flag per level is enough
            elif lvl.kind == "support" and c.close < lvl.low:
                move_pct = _pct(c.close, lvl.low)
                if move_pct >= config.BREAKOUT_CONFIRM_PCT:
                    levels.append(
                        PriceLevel(
                            kind="breakout_bearish",
                            low=lvl.low,
                            high=lvl.high,
                            note=f"Broke below support {lvl.low:.1f} at {c.timestamp}",
                            strength=move_pct,
                        )
                    )
                    break
    return levels


def detect_pullbacks(candles, breakouts) -> list:
    """
    After a breakout, price returns close to the broken level (now
    flipped support/resistance) without closing back through it.
    Classic "retest" entry zone.
    """
    levels = []
    for bo in breakouts:
        mid = (bo.low + bo.high) / 2
        for c in candles:
            near = _pct(c.close, mid) <= config.PULLBACK_PROXIMITY_PCT

            if bo.kind == "breakout_bullish" and near and c.close > bo.high:
                levels.append(
                    PriceLevel(
                        kind="pullback_bullish",
                        low=bo.low,
                        high=bo.high,
                        note=f"Pullback retest of broken resistance {bo.high:.1f} at {c.timestamp}",
                        strength=bo.strength,
                    )
                )
                break
            if bo.kind == "breakout_bearish" and near and c.close < bo.low:
                levels.append(
                    PriceLevel(
                        kind="pullback_bearish",
                        low=bo.low,
                        high=bo.high,
                        note=f"Pullback retest of broken support {bo.low:.1f} at {c.timestamp}",
                        strength=bo.strength,
                    )
                )
                break
    return levels


def analyze(candles) -> list:
    """Run all structure detectors and return one combined list of PriceLevel signals."""
    if len(candles) < (2 * config.SWING_LOOKBACK + 1):
        return []  # not enough candles to detect swings reliably

    levels = []
    levels += detect_fair_value_gaps(candles)
    levels += detect_order_blocks(candles)
    sr_levels = detect_support_resistance(candles)
    levels += sr_levels
    levels += detect_liquidity_sweeps(candles)

    breakouts = detect_breakouts(candles, sr_levels)
    levels += breakouts
    levels += detect_pullbacks(candles, breakouts)

    return levels


def analyze_with_context(candles) -> tuple:
    """Returns (levels, MarketContext) — the full structure + trend/momentum/volume read."""
    levels = analyze(candles)
    context = build_context(candles)
    return levels, context


def levels_near_price(levels, price, tolerance_pct=None) -> list:
    """Filter to levels whose zone is within tolerance_pct of a given price."""
    tolerance_pct = tolerance_pct or config.PRICE_LEVEL_PROXIMITY_PCT
    nearby = []
    for lvl in levels:
        if lvl.low - (lvl.low * tolerance_pct / 100) <= price <= lvl.high + (lvl.high * tolerance_pct / 100):
            nearby.append(lvl)
    return nearby
