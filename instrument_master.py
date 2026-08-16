"""
Maps an option contract (strike, expiry, option type) to Dhan's numeric
security ID.

WHY THIS EXISTS
---------------
Everything in this project until now addressed contracts the way the
/optionchain REST endpoint does -- by strike and expiry, with the chain
response carrying prices directly. Two things need the numeric security
ID instead, and neither can work without this file:

  - The WebSocket live feed (orderflow_feed.py) subscribes per
    instrument by SecurityId. There is no "subscribe to the NIFTY chain"
    call.
  - The lightweight /marketfeed/ltp endpoint (BACKLOG.md's fast
    position-check item) takes a list of security IDs.

Dhan publishes the mapping as a public CSV (no auth, no Data API
subscription needed -- it stays reachable even when the API itself is
returning 401 "Data APIs not Subscribed", which is worth knowing when
diagnosing a dead session).

CACHING
-------
The detailed master is ~32 MB and covers every instrument Dhan lists.
Downloading it per process per run would be wasteful and slow, so it is
cached on disk and only one underlying's option rows are retained -- a
few thousand rows instead of a few hundred thousand. Every function here
takes an `underlying` parameter (default "NIFTY", so existing callers are
unaffected); each underlying gets its own cache file
(instrument_master_<underlying>.json) since NIFTY and Bank Nifty are
fetched by separate processes (orderflow_feed.py / orderflow_feed_banknifty.py)
that have no reason to share one.

The cache is refreshed when it is older than CACHE_MAX_AGE_HOURS.
Deliberately NOT "once per calendar day": new weekly expiries appear in
the master before they are tradeable, and a stale cache silently missing
a contract would look exactly like "that strike has no order flow",
which is the failure mode this project has been bitten by repeatedly.
An explicitly age-bounded cache makes staleness a knowable quantity
rather than an assumption.
"""

import csv
import io
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("nifty_scanner")

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

CACHE_DIR = Path(__file__).parent / "state"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_PATH = CACHE_DIR / "instrument_master_nifty.json"   # kept for back-compat; use _cache_path_for()

CACHE_MAX_AGE_HOURS = 12
DOWNLOAD_TIMEOUT_SECONDS = 180

# What the WebSocket feed and marketfeed endpoints expect as
# ExchangeSegment for NSE derivatives. The master spells the same thing
# as EXCH_ID="NSE" + SEGMENT="D"; this is the API-side name for it. Same
# segment for every NSE index underlying (NIFTY, Bank Nifty, ...).
NSE_FNO_SEGMENT = "NSE_FNO"

UNDERLYING = "NIFTY"
BANKNIFTY = "BANKNIFTY"
INSTRUMENT_KIND = "OPTIDX"   # index options, as opposed to OPTSTK / FUTIDX


def _cache_path_for(underlying: str) -> Path:
    return CACHE_DIR / f"instrument_master_{underlying.lower()}.json"


def _cache_age_hours(underlying: str = UNDERLYING) -> float:
    path = _cache_path_for(underlying)
    if not path.exists():
        return float("inf")
    return (time.time() - path.stat().st_mtime) / 3600


def _parse_options(csv_text: str, underlying: str = UNDERLYING) -> list:
    """
    Keep only `underlying`'s index-option rows, reduced to the fields
    anything here actually needs. Rows with unparseable strike/expiry are
    skipped rather than allowed through as zeros -- a strike of 0.0 would
    silently match nothing forever.
    """
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row.get("UNDERLYING_SYMBOL") != underlying:
            continue
        if row.get("INSTRUMENT") != INSTRUMENT_KIND:
            continue
        option_type = (row.get("OPTION_TYPE") or "").strip()
        if option_type not in ("CE", "PE"):
            continue
        try:
            strike = float(row["STRIKE_PRICE"])
            security_id = int(row["SECURITY_ID"])
        except (KeyError, TypeError, ValueError):
            continue
        expiry = (row.get("SM_EXPIRY_DATE") or "").strip()[:10]
        if not expiry:
            continue
        out.append({
            "security_id": security_id,
            "strike": strike,
            "expiry": expiry,
            "option_type": option_type,
            "lot_size": float(row.get("LOT_SIZE") or 0) or None,
        })
    return out


def refresh(force: bool = False, underlying: str = UNDERLYING) -> list:
    """
    Return `underlying`'s option rows, downloading and re-caching if that
    underlying's cache is missing or older than CACHE_MAX_AGE_HOURS.

    On a download failure with a usable cache present, the STALE cache is
    returned with a warning rather than raising: a live loop losing its
    order-flow subscription list mid-session because Dhan's CDN blipped
    would be a worse outcome than briefly-old security IDs, which barely
    change. With no cache at all there is nothing to fall back to and the
    error propagates.
    """
    cache_path = _cache_path_for(underlying)
    if not force and _cache_age_hours(underlying) <= CACHE_MAX_AGE_HOURS:
        try:
            return json.loads(cache_path.read_text())["contracts"]
        except (json.JSONDecodeError, OSError, KeyError):
            log.info(f"  [instrument master] {underlying} cache unreadable, re-downloading")

    try:
        resp = requests.get(MASTER_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        resp.raise_for_status()
        contracts = _parse_options(resp.text, underlying)
    except Exception as e:
        if cache_path.exists():
            age = _cache_age_hours(underlying)
            log.info(f"  [instrument master] {underlying} download failed ({e}); "
                     f"using stale cache ({age:.1f}h old)")
            return json.loads(cache_path.read_text())["contracts"]
        raise

    if not contracts:
        # A structurally valid CSV that yields nothing means the schema
        # moved (a renamed column, a changed UNDERLYING_SYMBOL spelling),
        # or `underlying` itself doesn't match the master's own spelling.
        # Failing loudly beats caching an empty map that would make every
        # lookup return None and look like "no such contract".
        raise RuntimeError(
            f"Instrument master downloaded but contained no {underlying} option rows -- "
            f"the CSV schema may have changed, or '{underlying}' doesn't match the master's "
            f"UNDERLYING_SYMBOL spelling; check column names against "
            f"instrument_master._parse_options()"
        )

    cache_path.write_text(json.dumps({
        "fetched_at": time.time(),
        "count": len(contracts),
        "contracts": contracts,
    }))
    log.info(f"  [instrument master] cached {len(contracts):,} {underlying} option contracts")
    return contracts


def build_index(contracts: list = None, underlying: str = UNDERLYING) -> dict:
    """{(strike, expiry, option_type): security_id} for O(1) lookup."""
    contracts = contracts if contracts is not None else refresh(underlying=underlying)
    return {
        (c["strike"], c["expiry"], c["option_type"]): c["security_id"]
        for c in contracts
    }


def security_id_for(strike: float, expiry: str, option_type: str,
                    index: dict = None, underlying: str = UNDERLYING) -> int:
    """
    Numeric security ID for one contract, or None if the master has no
    such row.

    Returns None rather than raising: a strike that legitimately isn't
    listed (far outside the traded range, or a just-added expiry not yet
    in the cached master) is a normal condition a caller should handle by
    skipping that contract, not an exception that kills a feed managing
    several hundred subscriptions. `underlying` is ignored when `index`
    is passed explicitly -- it only affects the default refresh() call.
    """
    index = index if index is not None else build_index(underlying=underlying)
    return index.get((float(strike), expiry[:10], option_type))


def describe(underlying: str = UNDERLYING) -> str:
    contracts = refresh(underlying=underlying)
    expiries = sorted({c["expiry"] for c in contracts})
    age = _cache_age_hours(underlying)
    lines = [
        f"{len(contracts):,} {underlying} option contracts across {len(expiries)} expiries",
        f"cache age: {age:.1f}h (refreshes past {CACHE_MAX_AGE_HOURS}h)",
        f"nearest expiries: {', '.join(expiries[:4])}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(describe(sys.argv[1] if len(sys.argv) > 1 else UNDERLYING))
