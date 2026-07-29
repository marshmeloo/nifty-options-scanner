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
    # OI and premium change over a RECENT INTRADAY window, as opposed to
    # oi_change_pct / price_change_pct above, which are both measured
    # against the previous session's close and therefore only ever grow
    # through the day. `buildup_type` is classified from these when they're
    # available, so "long buildup" means positioning built up in the last
    # few minutes rather than at any point since yesterday. None until
    # enough history has accumulated this session, or on sources that
    # don't track it (NSE/CSV) -- consumers fall back to the daily figures.
    oi_change_pct_intraday: Optional[float] = None
    price_change_pct_intraday: Optional[float] = None
    buildup_window_minutes: Optional[float] = None   # actual span the intraday deltas cover
    # Top of book. LTP is the last TRADED price -- it can be stale, and
    # it sits on whichever side that trade happened to hit. A buyer pays
    # the ask and a seller receives the bid, so marking both legs at LTP
    # builds a systematic optimistic bias into every recorded outcome.
    # None where the source doesn't publish a book (CSV / TradingView),
    # in which case callers fall back to LTP and say so.
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None

    @property
    def has_book(self) -> bool:
        return bool(self.bid and self.ask and self.ask >= self.bid)

    @property
    def buy_price(self) -> float:
        """What it actually costs to OPEN a long position (cross the ask)."""
        return self.ask if self.has_book else self.ltp

    @property
    def sell_price(self) -> float:
        """What is actually received when CLOSING a long position (hit the bid)."""
        return self.bid if self.has_book else self.ltp

    @property
    def spread_pct(self) -> Optional[float]:
        """Bid-ask spread as a % of mid -- the single best liquidity proxy here."""
        if not self.has_book:
            return None
        mid = (self.bid + self.ask) / 2
        return round((self.ask - self.bid) / mid * 100, 2) if mid else None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OIAnalysis:
    """
    Chain-wide "where is smart money positioned" read, computed once per
    snapshot from OI across all strikes. See oi_analytics.py.
    """
    max_pain_strike: float          # strike where option writers collectively lose the least at expiry
    max_pain_distance_pct: float    # how far spot currently sits from max pain, as a % of spot
    call_wall_strike: float         # strike with the single largest CE OI -> acts as resistance
    put_wall_strike: float          # strike with the single largest PE OI -> acts as support
    call_wall_oi: int
    put_wall_oi: int
    net_delta_oi: int               # (today's CE OI added) - (today's PE OI added), signed
    net_delta_oi_bias: str          # "bullish" | "bearish" | "neutral" reading of net_delta_oi
    top_oi_concentration: list = field(default_factory=list)  # list[(strike, ce_oi, pe_oi)], sorted by combined OI desc


@dataclass
class MarketSnapshot:
    """Underlying + full option chain at a point in time."""
    symbol: str
    spot: float
    vwap: float
    pcr: float
    chain: list  # list[OptionQuote]
    timestamp: datetime = field(default_factory=datetime.now)
    oi_analysis: Optional[OIAnalysis] = None
    source: str = "unknown"   # which data source produced this snapshot: "dhan" | "nse" | "csv"


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
    # Index of the candle this level formed at, used to age levels out.
    # Without it, price_action.analyze() returned every level ever detected
    # in the series with no way to tell a fresh zone from a stale one, so
    # scanner.py's per-level score only ever ratcheted upward through a
    # session (measured 0 -> 14 levels on a single strike on 2026-07-28).
    # None means "age unknown" and is treated as always-current.
    formed_at_index: Optional[int] = None


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
    # How the stop distance was arrived at (ATR x delta, or the flat
    # percentage fallback and why). Recorded so a plan's geometry is
    # auditable after the fact rather than an unexplained pair of numbers.
    stop_basis: str = ""
    # Which price the entry was struck at (ask vs LTP fallback), so a
    # journal reader can tell a realistic fill from an optimistic one.
    entry_basis: str = ""


@dataclass
class RiskVerdict:
    decision: str            # "APPROVED", "WATCHLIST", "REJECTED"
    reasons: list             # list[str]
    checks: dict               # individual check name -> pass/fail + detail


@dataclass
class CondorPlan:
    """
    A proposed iron condor: sell CE + sell PE (the credit legs), buy a
    further-OTM CE + PE against each (the protective wings). See
    condor_plan_generator.py for how this gets built and
    config_condor.py for the strike-selection/hedge-distance knobs.
    """
    expiry: str
    short_ce_strike: float
    short_ce_premium: float
    short_pe_strike: float
    short_pe_premium: float
    hedge_ce_strike: float
    hedge_ce_premium: float
    hedge_pe_strike: float
    hedge_pe_premium: float
    lots: int
    net_credit: float          # per share, i.e. per 1 lot-unit -- multiply by lot_size*lots for rupees
    net_credit_inr: float
    max_loss_inr: float        # worst case across both wings, in rupees
    max_profit_inr: float      # = net_credit_inr (full credit retained if spot stays between the short strikes)
    breakeven_upper: float
    breakeven_lower: float


@dataclass
class CondorPosition:
    """Live-tracked state of an OPEN condor -- what condor_tracker.py journals/monitors."""
    plan: CondorPlan
    opened_at: str              # isoformat timestamp
    status: str = "OPEN"        # "OPEN" | "CLOSED" | "EXPIRED"
    closed_at: str = None
    close_reason: str = None    # "expiry_settlement" | "breach_close" | "manual"
    exit_credit_inr: float = None   # what it cost to buy back (or 0 if let expire worthless -- the good outcome)
    pnl_inr: float = None
    pnl_pct_of_max_profit: float = None


@dataclass
class DirectionalSpreadPlan:
    """
    A proposed single-sided credit spread: sell one strike, buy a
    further-OTM strike on the SAME side as protection. Bull put spread
    (direction="PE") when the market bias is bullish -- profits if spot
    stays above the short strike. Bear call spread (direction="CE") when
    bearish -- profits if spot stays below it. Unlike CondorPlan (which
    is market-neutral, both sides), this is a directional bet: it wins
    on being right about which way price goes, not on price staying in
    a range. See directional_spread_scanner.py for how the side is
    chosen and directional_spread_plan_generator.py for how this is
    priced.
    """
    expiry: str
    direction: str              # "PE" (bull put spread) | "CE" (bear call spread)
    bias_label: str             # the market_bias read that produced this trade, for audit
    bias_score: float
    short_strike: float
    short_premium: float
    hedge_strike: float
    hedge_premium: float
    lots: int
    net_credit: float           # per share -- multiply by lot_size*lots for rupees
    net_credit_inr: float
    max_loss_inr: float
    max_profit_inr: float       # = net_credit_inr (full credit retained if spot never breaches the short strike)
    breakeven: float


@dataclass
class DirectionalSpreadPosition:
    """Live-tracked state of an OPEN directional spread."""
    plan: DirectionalSpreadPlan
    opened_at: str
    status: str = "OPEN"        # "OPEN" | "CLOSED" | "EXPIRED"
    closed_at: str = None
    close_reason: str = None    # "profit_target" | "stop_loss" | "expiry_settlement" | "breach_close" | "manual"
    pnl_inr: float = None
    pnl_pct_of_max_profit: float = None
