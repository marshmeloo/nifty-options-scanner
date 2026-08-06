"""
Historical futures data for MCX commodities (gold, silver, ...).

WHY THIS IS MUCH SIMPLER THAN historical_source.py'S NIFTY/BANKNIFTY WORK
---------------------------------------------------------------------------
Options have no historical-candle endpoint for an EXPIRED contract at all --
that's the whole reason historical_source.py has to reconstruct a chain
from Dhan's "Expired Options Data" ATM+/-N offset API, and why that
reconstruction has its own documented contamination bug (BACKLOG.md).
Futures don't have that problem: Dhan's /v2/charts/historical, queried
against the CURRENTLY LISTED contract's own security ID with
exchangeSegment=MCX_COMM, already returns a genuine continuous/rolled
series spanning years -- verified directly 2026-08-06 by requesting a
5-year window against Gold's Oct-2026 contract (then security id 483079)
and Silver's Sep-2026 contract (then 471725): both returned 1200+ daily
bars from 2021-09-01 through the request date, with price levels tracing
a smooth, plausible real trajectory (Gold Rs 47,068 -> Rs 145,234/10g;
Silver Rs 63,573 -> Rs 227,584/kg) and no rollover-shaped discontinuities
-- just ordinary volatile trading days (largest single-day moves ~5-12%,
scattered, not periodic). No ATM-offset reconstruction needed here.

exchangeSegment=MCX_COMM confirmed correct by elimination: MCX_FO and
bare MCX both returned 400 ("bad values for parameters") on the same
request; only MCX_COMM got past validation to a real 200 response.

SECURITY ID IS NOT STABLE -- a FUTCOM contract's security id is specific
to ONE expiry and Dhan's instrument master only lists currently-tradeable
expiries (2026-08-06: 145 MCX FUTCOM rows total, covering every listed
commodity's near + far months). _current_security_id() always resolves
the NEAREST currently-listed contract fresh from the instrument master
CSV rather than a hardcoded id, so this keeps working after each
month's rollover without needing manual updates. Which of the 2-3
listed contracts you query does not appear to change the returned
series (see module docstring above) -- nearest expiry is used for no
reason beyond being the first in sorted order.
"""

import csv
import io
import logging
from datetime import date, timedelta, datetime
from pathlib import Path

import requests

import dhan_rate_limiter
from atomic_state import atomic_write_json
from models import Candle

log = logging.getLogger("nifty_scanner")

DHAN_BASE_URL = "https://api.dhan.co/v2"
MCX_SEG = "MCX_COMM"
INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

DATA_DIR = Path(__file__).parent / "logs" / "commodities"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _headers():
    import os
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise EnvironmentError("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN first.")
    return {"Content-Type": "application/json", "access-token": access_token, "client-id": client_id}


def _current_security_id(underlying_symbol: str) -> dict:
    """
    Nearest currently-listed MCX FUTCOM contract for `underlying_symbol`
    (e.g. "GOLD", "SILVER"), fresh from Dhan's public instrument master.
    Returns {"security_id", "expiry", "lot_size"}. Raises if the symbol
    has no listed FUTCOM row -- a typo or a symbol that only trades
    options-on-futures (OPTFUT), not the future itself, would otherwise
    silently look like "no data" rather than "wrong instrument kind".
    """
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=180)
    resp.raise_for_status()
    rows = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        if row.get("EXCH_ID") != "MCX" or row.get("INSTRUMENT") != "FUTCOM":
            continue
        if row.get("UNDERLYING_SYMBOL") != underlying_symbol:
            continue
        expiry = (row.get("SM_EXPIRY_DATE") or "").strip()[:10]
        if not expiry:
            continue
        rows.append({
            "security_id": int(row["SECURITY_ID"]),
            "expiry": expiry,
            "lot_size": float(row.get("LOT_SIZE") or 0) or None,
        })
    if not rows:
        raise ValueError(f"No MCX FUTCOM rows found for UNDERLYING_SYMBOL={underlying_symbol!r} "
                         f"-- check the spelling against the instrument master's own column, "
                         f"or confirm this symbol trades a future at all (some MCX names are "
                         f"options-on-future only)")
    rows.sort(key=lambda r: r["expiry"])
    return rows[0]


def backfill_daily(underlying_symbol: str, days_back: int = 1800) -> list:
    """
    Daily OHLCV candles for `underlying_symbol`'s futures, going
    `days_back` calendar days into the past. Returns a list of Candle,
    oldest first. See module docstring for why a single call against the
    current contract's security id is enough -- no per-expiry stitching
    needed, unlike the options reconstruction.
    """
    contract = _current_security_id(underlying_symbol)
    dhan_rate_limiter.wait_for_slot()
    resp = requests.post(
        f"{DHAN_BASE_URL}/charts/historical",
        headers=_headers(),
        json={
            "securityId": str(contract["security_id"]),
            "exchangeSegment": MCX_SEG,
            "instrument": "FUTCOM",
            "fromDate": (date.today() - timedelta(days=days_back)).isoformat(),
            "toDate": date.today().isoformat(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    candles = []
    for i in range(len(data.get("timestamp", []))):
        candles.append(Candle(
            timestamp=datetime.fromtimestamp(data["timestamp"][i]),
            open=data["open"][i],
            high=data["high"][i],
            low=data["low"][i],
            close=data["close"][i],
            volume=data["volume"][i] if "volume" in data else 0,
        ))
    candles.sort(key=lambda c: c.timestamp)
    return candles


def save_daily(underlying_symbol: str, candles: list) -> Path:
    path = DATA_DIR / f"{underlying_symbol.lower()}_daily.json"
    atomic_write_json(path, {
        "underlying_symbol": underlying_symbol,
        "fetched_at": datetime.now().isoformat(),
        "count": len(candles),
        "candles": [
            {"timestamp": c.timestamp.isoformat(), "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles
        ],
    })
    return path


def load_daily(underlying_symbol: str) -> list:
    import json
    path = DATA_DIR / f"{underlying_symbol.lower()}_daily.json"
    data = json.loads(path.read_text())
    return [
        Candle(timestamp=datetime.fromisoformat(c["timestamp"]), open=c["open"], high=c["high"],
              low=c["low"], close=c["close"], volume=c["volume"])
        for c in data["candles"]
    ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for sym in ("GOLD", "SILVER"):
        candles = backfill_daily(sym)
        path = save_daily(sym, candles)
        print(f"{sym}: {len(candles)} daily bars, "
              f"{candles[0].timestamp.date()} .. {candles[-1].timestamp.date()} -> {path}")
