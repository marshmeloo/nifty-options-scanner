"""
Volume profile: a distribution of traded volume AT EACH PRICE LEVEL over
a period, as opposed to volume over time (which is all price_action.py's
volume_ratio gives you). This reveals high-volume nodes (HVN -- price
areas where a lot of trading happened, often where big positions were
built or unwound -- "acceptance") and low-volume nodes (LVN -- price
skipped through quickly, often a rejection or an imbalance).

Honesty note on the method: a TRUE volume profile is built from tick-by-
tick trade prints (every executed trade at its exact price). We don't
have that -- Dhan's REST candle endpoint gives OHLCV bars, not a trade
tape. This module uses the standard approximation for when only OHLCV
bars are available: each candle's volume is spread evenly across every
price bin between its low and high. This is coarser than a true
tick-built profile (it assumes volume was uniform across the candle's
range, which usually isn't literally true), but it's the accepted
practice for building a profile from bar data -- resolution is only as
good as your bin size, and a smaller bin size doesn't fix the underlying
approximation, it just makes it more granular.

Key outputs:
  - POC (Point of Control): the single price level with the most volume
    -- the price the market spent the most time/volume agreeing on.
  - Value Area (VAH/VAL): the price range containing ~70% of total
    volume, built outward from the POC. Price outside the value area
    moved through with comparatively little acceptance.
  - HVN / LVN: bins well above / well below average volume.
"""

from collections import defaultdict

import config as cfg
from resilient_source import get_nifty_intraday_candles


def build_volume_profile(candles: list, bin_size: float = None) -> list:
    """
    Returns a sorted list of (price_bin, volume) tuples. Each candle's
    volume is spread evenly across every bin between its low and high
    (inclusive) -- see the module docstring for why this approximation
    is used instead of a true tick-built profile.
    """
    if not candles:
        return []
    bin_size = bin_size or getattr(cfg, "VOLUME_PROFILE_BIN_POINTS", 10)

    volume_by_bin = defaultdict(float)
    for c in candles:
        if c.volume <= 0 or c.high < c.low:
            continue
        low_bin = round(c.low / bin_size) * bin_size
        high_bin = round(c.high / bin_size) * bin_size
        bins_in_range = list(_frange(low_bin, high_bin, bin_size))
        if not bins_in_range:
            bins_in_range = [low_bin]
        vol_per_bin = c.volume / len(bins_in_range)
        for b in bins_in_range:
            volume_by_bin[b] += vol_per_bin

    return sorted(volume_by_bin.items())


def _frange(start, stop, step):
    x = start
    # small epsilon guard against float accumulation skipping the last bin
    while x <= stop + step * 0.001:
        yield round(x, 2)
        x += step


def compute_poc(profile: list):
    """Point of Control: the price bin with the most volume. None if profile is empty."""
    if not profile:
        return None
    return max(profile, key=lambda pv: pv[1])[0]


def compute_value_area(profile: list, target_pct: float = None):
    """
    Value Area: starting from the POC, repeatedly add whichever
    neighboring bin (above or below the current area) has more volume,
    until the accumulated volume reaches target_pct of the total.
    Returns (val, vah, actual_pct_captured). None values if profile is empty.
    """
    if not profile:
        return None, None, 0.0
    target_pct = target_pct or getattr(cfg, "VALUE_AREA_TARGET_PCT", 0.70)

    total_volume = sum(v for _, v in profile)
    if total_volume <= 0:
        return None, None, 0.0

    prices = [p for p, _ in profile]
    volumes = {p: v for p, v in profile}
    poc = compute_poc(profile)
    poc_idx = prices.index(poc)

    lo_idx, hi_idx = poc_idx, poc_idx
    captured = volumes[poc]

    while captured / total_volume < target_pct and (lo_idx > 0 or hi_idx < len(prices) - 1):
        vol_below = volumes[prices[lo_idx - 1]] if lo_idx > 0 else -1
        vol_above = volumes[prices[hi_idx + 1]] if hi_idx < len(prices) - 1 else -1

        if vol_above >= vol_below:
            hi_idx += 1
            captured += vol_above
        else:
            lo_idx -= 1
            captured += vol_below

    return prices[lo_idx], prices[hi_idx], round(captured / total_volume * 100, 1)


def classify_nodes(profile: list, hvn_multiple: float = None, lvn_multiple: float = None):
    """
    HVN = bins with volume >= hvn_multiple x the average bin volume.
    LVN = bins with volume <= lvn_multiple x the average bin volume.
    Returns {"hvn": [prices...], "lvn": [prices...]}.
    """
    if not profile:
        return {"hvn": [], "lvn": []}
    hvn_multiple = hvn_multiple or getattr(cfg, "HVN_MULTIPLE", 1.5)
    lvn_multiple = lvn_multiple or getattr(cfg, "LVN_MULTIPLE", 0.5)

    avg_volume = sum(v for _, v in profile) / len(profile)
    hvn = [p for p, v in profile if v >= avg_volume * hvn_multiple]
    lvn = [p for p, v in profile if v <= avg_volume * lvn_multiple]
    return {"hvn": hvn, "lvn": lvn}


def get_volume_profile_context(candles: list = None) -> dict:
    """
    One-call convenience. If candles aren't passed in, fetches today's
    intraday candles itself -- but callers that already have today's
    candles (e.g. main_live.py, which fetches them for price_action
    anyway) should pass them in to avoid a duplicate API call.
    """
    try:
        if candles is None:
            candles = get_nifty_intraday_candles()
        profile = build_volume_profile(candles)
        if not profile:
            return {"error": "No candle data available to build a profile"}

        poc = compute_poc(profile)
        val, vah, captured_pct = compute_value_area(profile)
        nodes = classify_nodes(profile)

        return {
            "poc": poc,
            "value_area_low": val,
            "value_area_high": vah,
            "value_area_captured_pct": captured_pct,
            "hvn": nodes["hvn"],
            "lvn": nodes["lvn"],
            "bin_count": len(profile),
        }
    except Exception as e:
        return {"error": str(e)}
