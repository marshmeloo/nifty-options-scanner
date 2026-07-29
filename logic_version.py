"""
A short, stable fingerprint of the decision logic that produced a result.

WHY THIS EXISTS
---------------
Results from different logic versions must never be silently pooled. A
win rate computed across a config change, or across a bug fix that
altered what "long buildup" means, is measuring nothing -- it's an
average over two different systems.

Every decision-log record and journal entry carries this stamp, so any
later analysis can partition by version and state honestly how large the
comparable sample actually is.

WHAT GOES INTO IT
-----------------
Two independent components:

  - The values of every decision-relevant config constant. Changing a
    threshold changes behaviour without changing a line of code, and is
    exactly the kind of edit that would otherwise go unrecorded.
  - The git commit SHA, which covers changes to the logic itself.

Deliberately EXCLUDED are settings that affect operations but not
decisions -- polling intervals, timeouts, cache durations, dashboard
options. Including those would churn the version on edits that cannot
change a single trade, fragmenting samples for no reason.
"""

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

import config

# Config constants that can change a decision. Keep this list explicit
# rather than "all uppercase attributes": the point is to churn the
# version when behaviour changes and NOT to churn it otherwise, and only
# a curated list can make that distinction.
DECISION_RELEVANT_SETTINGS = (
    # Scoring
    "IV_PERCENTILE_HIGH", "IV_PERCENTILE_LOW", "IV_CHEAP_SCORE", "IV_RICH_SCORE",
    "OI_BUILDUP_PCT", "PCR_BULLISH_ABOVE", "PCR_BEARISH_BELOW", "VWAP_DEVIATION_PCT",
    "LONG_BUILDUP_SCORE", "SHORT_COVERING_SCORE", "SHORT_BUILDUP_SCORE", "LONG_UNWINDING_SCORE",
    "LEVEL_SCORE_EACH", "SWEEP_SCORE_EACH", "MAX_LEVEL_SCORE_CONTRIBUTION",
    "LEVEL_MAX_AGE_CANDLES", "PRICE_LEVEL_PROXIMITY_PCT",
    # Intraday OI freshness
    "OI_INTRADAY_LOOKBACK_MINUTES", "OI_INTRADAY_BUILDUP_PCT", "OI_HISTORY_SAMPLE_SPACING_SECONDS",
    # Structure detection
    "SWING_LOOKBACK", "OB_MIN_MOVE_PCT", "SR_CLUSTER_TOLERANCE_PCT", "SR_MIN_TOUCHES",
    "SWEEP_WICK_MIN_PCT", "BREAKOUT_CONFIRM_PCT", "PULLBACK_PROXIMITY_PCT",
    "CANDLE_INTERVAL_MINUTES", "TREND_SWING_LOOKBACK",
    "RSI_PERIOD", "RSI_OVERBOUGHT", "RSI_OVERSOLD", "ROC_PERIOD", "ROC_SIGNIFICANT_PCT",
    "VOLUME_MA_PERIOD", "VOLUME_SPIKE_MULTIPLE",
    # Universe
    "STRIKE_RANGE_POINTS", "PREMIUM_MIN", "PREMIUM_MAX",
    # Plan geometry
    "DEFAULT_STOP_LOSS_PCT", "DEFAULT_TARGET_RR", "USE_VOLATILITY_BASED_PLAN",
    "ATR_PERIOD", "STOP_ATR_MULTIPLE", "MIN_STOP_PCT", "MAX_STOP_PCT",
    # Risk + selection
    "TOTAL_CAPITAL", "MAX_RISK_PER_TRADE_PCT", "MAX_TOTAL_EXPOSURE_PCT",
    "MAX_DAILY_LOSS_PCT", "MAX_LOTS_PER_TRADE", "NIFTY_LOT_SIZE",
    "MAX_NEW_TRADES_PER_DAY", "MIN_CONVICTION_SCORE_TO_TRACK",
    "JOURNAL_LOOKBACK_FOR_LEARNING", "MIN_TAG_SAMPLES_FOR_ADJUSTMENT",
    "WEAK_TAG_WIN_RATE", "STRONG_TAG_WIN_RATE",
    "REENTRY_PRICE_TOLERANCE_PCT",
    "EXPIRY_DAY_EXTRA_CONVICTION", "EXPIRY_DAY_NO_NEW_TRADES_AFTER",
    "NEWS_RISK_BLOCKS_NEW_TRADES",
)


def config_fingerprint() -> dict:
    """The decision-relevant config values, as actually loaded."""
    return {
        name: getattr(config, name)
        for name in DECISION_RELEVANT_SETTINGS
        if hasattr(config, name)
    }


@lru_cache(maxsize=1)
def git_sha() -> str:
    """Short git SHA, or 'nogit' when unavailable (zip download, no git)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and sha else "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


@lru_cache(maxsize=1)
def compute() -> str:
    """
    Short version string: "<config-hash>-<git-sha>".

    Cached for the life of the process -- config is imported once and
    doesn't change mid-session, and this is called every cycle.
    """
    payload = json.dumps(config_fingerprint(), sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{digest}-{git_sha()}"


def describe() -> str:
    fp = config_fingerprint()
    return (
        f"logic version: {compute()}\n"
        f"  config hash covers {len(fp)} decision-relevant settings\n"
        f"  git: {git_sha()}"
    )


if __name__ == "__main__":
    print(describe())
    print()
    for k, v in sorted(config_fingerprint().items()):
        print(f"  {k} = {v}")
