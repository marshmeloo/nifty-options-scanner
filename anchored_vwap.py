"""
Anchored VWAP: VWAP computed starting from a specific meaningful
reference point, not just "since session open." Institutions often use
anchored VWAP as their own execution benchmark -- e.g. "what's our
average cost since the last major swing low" -- so price reacting at an
anchored VWAP level can reflect where a large participant's average
entry sits, which a plain running session VWAP can't show you.

This is a genuine step up from dhan_source.py's existing VWAP: that one
is an incrementally-updated PROXY (built tick-by-tick from spot as the
session runs, since Dhan doesn't hand us a ready-made VWAP figure), not
a true volume-weighted calculation from the actual candle series. This
module computes the real thing directly from OHLCV bars using the
standard typical-price formula, and does it from multiple anchors, not
just session start.

Anchors used here (all derivable from the intraday candles main_live.py
already fetches, no extra API calls needed):
  - Session open (candles[0]) -- the plain "VWAP since today's open" read
  - Most recent significant swing high (price_action.find_swing_points)
  - Most recent significant swing low

Previous-day high/low or a specific news-event anchor would be other
reasonable anchors to add later; scoped out for now since they need a
day-boundary fetch this module doesn't do on its own.
"""

import price_action


def _typical_price(c) -> float:
    """Standard VWAP price input: (high + low + close) / 3, not just close."""
    return (c.high + c.low + c.close) / 3.0


def compute_vwap(candles: list, anchor_index: int = 0) -> float:
    """
    True volume-weighted VWAP from `anchor_index` through the end of
    `candles` (inclusive). Returns None if there's no volume in the
    range (can't divide by zero, and a VWAP with no volume behind it
    isn't meaningful anyway).
    """
    segment = candles[anchor_index:]
    total_volume = sum(c.volume for c in segment)
    if total_volume <= 0:
        return None
    weighted_sum = sum(_typical_price(c) * c.volume for c in segment)
    return round(weighted_sum / total_volume, 2)


def get_anchor_candidates(candles: list) -> list:
    """
    Returns a list of {"label", "index", "anchor_price", "anchor_time"}
    dicts: session open, plus the most recent swing high and low (if
    any exist yet given how much of the session has printed).
    """
    if not candles:
        return []

    anchors = [{
        "label": "Session Open",
        "index": 0,
        "anchor_price": candles[0].open,
        "anchor_time": candles[0].timestamp,
    }]

    swing_highs, swing_lows = price_action.find_swing_points(candles)

    if swing_highs:
        idx, price = swing_highs[-1]  # most recent
        anchors.append({
            "label": "Recent Swing High",
            "index": idx,
            "anchor_price": price,
            "anchor_time": candles[idx].timestamp,
        })
    if swing_lows:
        idx, price = swing_lows[-1]  # most recent
        anchors.append({
            "label": "Recent Swing Low",
            "index": idx,
            "anchor_price": price,
            "anchor_time": candles[idx].timestamp,
        })

    return anchors


def get_anchored_vwap_context(candles: list) -> dict:
    """
    One-call convenience: computes VWAP from each anchor candidate
    through to the latest candle, and reads current price against each.
    Returns {"anchors": [{"label", "anchor_time", "anchor_price", "vwap",
    "current_price", "distance_pct", "position"}, ...]}. `position` is
    "above" / "below" / "at" the anchored VWAP.
    """
    if not candles:
        return {"error": "No candle data available"}

    current_price = candles[-1].close
    anchor_candidates = get_anchor_candidates(candles)

    results = []
    for anchor in anchor_candidates:
        vwap = compute_vwap(candles, anchor_index=anchor["index"])
        if vwap is None:
            continue
        distance_pct = round((current_price - vwap) / vwap * 100, 2) if vwap else None
        if distance_pct is None:
            position = "unknown"
        elif abs(distance_pct) < 0.02:
            position = "at"
        else:
            position = "above" if distance_pct > 0 else "below"

        results.append({
            "label": anchor["label"],
            "anchor_time": anchor["anchor_time"],
            "anchor_price": anchor["anchor_price"],
            "vwap": vwap,
            "current_price": current_price,
            "distance_pct": distance_pct,
            "position": position,
        })

    if not results:
        return {"error": "No anchor produced a valid VWAP (no volume in range)"}

    return {"anchors": results}
