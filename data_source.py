"""
Data interface. This is the ONLY file you need to rewrite when you
wire in a real feed. Everything downstream consumes a MarketSnapshot,
so it doesn't care whether the data came from a CSV or a live API.

To go live:
  - Dhan API: https://dhanhq.co/docs/ (option chain + historical endpoints)
  - Kite Connect: https://kite.trade/docs/connect/v3/ (quote + instruments)
Both require your API key/secret and, per SEBI's 2025 algo trading
framework, registration if you move beyond decision-support into any
form of auto order placement.
"""

import csv
from datetime import datetime
from models import OptionQuote, MarketSnapshot


def load_snapshot_from_csv(path: str, symbol: str = "NIFTY") -> MarketSnapshot:
    """Load a market snapshot from a CSV file (sample or exported chain)."""
    chain = []
    spot = None
    vwap = None

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if spot is None:
                spot = float(row["spot"])
                vwap = float(row["vwap"])
            chain.append(
                OptionQuote(
                    symbol=symbol,
                    expiry=row["expiry"],
                    strike=float(row["strike"]),
                    option_type=row["option_type"],
                    ltp=float(row["ltp"]),
                    oi=int(row["oi"]),
                    oi_change_pct=float(row["oi_change_pct"]),
                    volume=int(row["volume"]),
                    iv=float(row["iv"]),
                    iv_percentile=float(row["iv_percentile"]),
                )
            )

    total_ce_oi = sum(q.oi for q in chain if q.option_type == "CE")
    total_pe_oi = sum(q.oi for q in chain if q.option_type == "PE")
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else 0.0

    return MarketSnapshot(
        symbol=symbol,
        spot=spot,
        vwap=vwap,
        pcr=pcr,
        chain=chain,
        timestamp=datetime.now(),
    )


def get_live_snapshot(symbol: str = "NIFTY") -> MarketSnapshot:
    """
    Placeholder for the live data path. Replace the body with a real
    Dhan/Kite API call that builds and returns a MarketSnapshot.
    """
    raise NotImplementedError(
        "Wire this up to Dhan or Kite Connect once you're ready to go live. "
        "Use load_snapshot_from_csv() for now with sample or exported data."
    )
