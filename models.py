"""
Shared data structures. Keeping these as plain dataclasses so any
data source (sample CSV, Dhan API, Kite Connect) can populate them
the same way.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class OptionQuote:
    """One strike, one option type, one snapshot in time."""
    symbol: str              # e.g. "NIFTY"
    expiry: str               # e.g. "2026-07-24"
    strike: float
    option_type: str          # "CE" or "PE"
    ltp: float
    oi: int
    oi_change_pct: float
    volume: int
    iv: float
    iv_percentile: float
    delta: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    price_change_pct: Optional[float] = None   # premium change vs a comparable baseline
    buildup_type: Optional[str] = None         # "long_buildup" | "short_buildup" | "short_covering" | "long_unwinding" | None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MarketSnapshot:
    """Underlying + full option chain at a point in time."""
    symbol: str
    spot: float
    vwap: float
    pcr: float
    chain: list  # list[OptionQuote]
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class PriceLevel:
    """
    A generic structural price-action signal: order block, fair value gap,
    support/resistance, or liquidity sweep. `kind` distinguishes them.
    """
    kind: str        # "ob_bullish" | "ob_bearish" | "fvg_bullish" | "fvg_bearish"
                      # | "support" | "resistance" | "sweep_bullish" | "sweep_bearish"
    low: float
    high: float
    note: str
    strength: float = 1.0   # e.g. number of touches for S/R, move size for OB/FVG


@dataclass
class MarketContext:
    """
    Chain-wide (not strike-specific) read on trend, momentum, and volume,
    computed once per snapshot from recent candles.
    """
    trend: str            # "uptrend" | "downtrend" | "range"
    trend_note: str
    rsi: float = None
    rsi_state: str = "neutral"   # "overbought" | "oversold" | "neutral"
    roc_pct: float = None
    volume_ratio: float = None   # latest candle volume / rolling average
    volume_spike: bool = False


@dataclass
class Setup:
    """A flagged candidate from the scanner, before it becomes a plan."""
    symbol: str
    strike: float
    option_type: str
    expiry: str
    reasons: list  # list[str], human-readable trigger reasons
    score: float   # simple composite strength score, higher = stronger signal


@dataclass
class TradePlan:
    setup: Setup
    entry: float
    target: float
    stop: float
    invalidation: str      # human-readable invalidation condition
    lots: int
    capital_at_risk: float
    risk_pct_of_capital: float
    risk_level: str         # "Low", "Medium", "High"


@dataclass
class RiskVerdict:
    decision: str            # "APPROVED", "WATCHLIST", "REJECTED"
    reasons: list             # list[str]
    checks: dict               # individual check name -> pass/fail + detail
