"""
Records bid/ask spreads from the live order-flow feed across a session,
so the LTP-fill assumption underlying every backtest in this project can
finally be replaced with a measurement.

WHY THIS EXISTS
---------------
Every backtest number here prices fills at LTP, because historical
reconstruction carries no order book (see historical_source.py). The
momentum cost-sensitivity analysis assumed 0.2-0.6% spreads and showed
that at 0.6% the strategy's edge shrinks materially. That assumption has
never been measured.

The first live capture, taken at 17:16 (well after the 15:30 close), read
0.6-1.6%. That is NOT evidence the assumption was wrong: spreads widen
when the market is closed and nobody is quoting. It IS evidence that a
number was being assumed where one could be measured, and that the answer
might sit at the painful end of the assumed range.

WHY MARKET PHASE IS RECORDED RATHER THAN FILTERED AT CAPTURE TIME
------------------------------------------------------------------
The obvious design is "only record during market hours". Deliberately not
done: the post-close reading is exactly what made the assumption
questionable in the first place, and discarding it at capture time would
have thrown away the observation that prompted this module. Every sample
is tagged with its market phase instead, and the ANALYSIS filters. That
keeps the filtering decision visible and revisable rather than baked
irreversibly into the data.

Sampling, not every packet: a liquid chain pushes hundreds of packets a
second, and spread distribution across a session does not need that
resolution. SAMPLE_INTERVAL_SECONDS between samples keeps a full day at a
few MB gzipped while preserving intraday variation, which is the whole
point (spreads at 09:15 are not spreads at 12:30).

NEVER RAISES. Recording is observability. A full disk or a serialisation
edge case must not take down a feed that strategies may be reading --
same rule as snapshot_recorder.record().
"""

import gzip
import json
import logging
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

log = logging.getLogger("orderflow")

RECORD_DIR = Path(__file__).parent / "logs" / "orderflow"

SAMPLE_INTERVAL_SECONDS = 30.0

# NSE cash/derivatives session. Matches market_regime.py's own constants;
# duplicated rather than imported so this module has no dependency on a
# module that reaches out to Dhan for daily candles.
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# The first and last few minutes behave differently enough to be worth
# separating: the opening auction leaves wide, fast-moving books, and the
# closing minutes thin out. Lumping them into "regular" would bias the
# very number this exists to measure.
OPENING_ENDS = dtime(9, 30)
CLOSING_BEGINS = dtime(15, 15)


def path_for(day) -> Path:
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return RECORD_DIR / f"{day.strftime('%Y%m%d')}.jsonl.gz"


def market_phase(ts: datetime) -> str:
    """
    "pre_open" | "opening" | "regular" | "closing" | "post_close".

    Weekends and holidays are not distinguished -- a holiday simply
    produces a day of pre/post_close samples with no regular ones, which
    is self-evident in the data and needs no calendar lookup.
    """
    t = ts.time()
    if t < MARKET_OPEN:
        return "pre_open"
    if t < OPENING_ENDS:
        return "opening"
    if t < CLOSING_BEGINS:
        return "regular"
    if t <= MARKET_CLOSE:
        return "closing"
    return "post_close"


def _spread_sample(contract: dict, spot: float = None) -> dict:
    """
    One contract's spread at this instant, or None if the book cannot
    price it.

    A zero or crossed book yields None rather than a number: an empty
    side publishes 0.0, and a 0.0 "bid" would produce a 200% spread that
    is an artifact, not a measurement. Those are exactly the samples that
    would drag a mean and make the assumption look worse than it is.
    """
    depth = contract.get("depth") or []
    if not depth:
        return None
    top = depth[0]
    bid = top.get("bid_price") or 0.0
    ask = top.get("ask_price") or 0.0
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None

    mid = (bid + ask) / 2
    sample = {
        "strike": contract.get("strike"),
        "option_type": contract.get("option_type"),
        "bid": round(bid, 2),
        "ask": round(ask, 2),
        "mid": round(mid, 2),
        "spread_pct": round((ask - bid) / mid * 100, 4),
        "bid_qty": top.get("bid_qty"),
        "ask_qty": top.get("ask_qty"),
        "ltp": contract.get("ltp"),
        "volume": contract.get("volume"),
        "oi": contract.get("oi"),
    }
    if spot:
        # Moneyness matters: an ATM contract and a far-OTM one have very
        # different spreads, and the strategies trade specific premium
        # bands. Recorded per sample so the analysis can stratify by it
        # rather than reporting one meaningless chain-wide average.
        sample["distance_from_spot"] = round(abs(contract["strike"] - spot), 1)
    return sample


def record_samples(state: dict, spot: float = None, now: datetime = None) -> int:
    """
    Append one timestamped row covering every contract the feed can
    currently price. Returns how many contracts were recorded (0 on any
    failure -- see the module docstring on never raising).
    """
    try:
        now = now or datetime.now()
        contracts = state.get("contracts") or []
        samples = [s for s in (_spread_sample(c, spot) for c in contracts) if s]
        if not samples:
            return 0

        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": now.isoformat(timespec="seconds"),
            "phase": market_phase(now),
            "spot": spot,
            "subscribed": state.get("subscribed"),
            "receiving": state.get("receiving"),
            "samples": samples,
        }
        with gzip.open(path_for(now.date()), "at", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return len(samples)
    except Exception as e:
        log.info(f"  [orderflow recorder] sample failed, continuing: {e}")
        return 0


def available_days() -> list:
    if not RECORD_DIR.exists():
        return []
    days = []
    for p in RECORD_DIR.glob("*.jsonl.gz"):
        try:
            days.append(datetime.strptime(p.name.split(".")[0], "%Y%m%d").date().isoformat())
        except ValueError:
            continue
    return sorted(days)


def load_day(day):
    """
    Yields each recorded row for `day`.

    A corrupt trailing line is skipped rather than aborting the day: a
    gzip stream cut mid-write by a kill leaves exactly that, and one
    truncated sample should not cost the session -- same handling as
    snapshot_recorder.load_day.
    """
    path = path_for(day)
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def describe() -> str:
    days = available_days()
    if not days:
        return "No order-flow spread recordings yet."
    lines = [f"{len(days)} recorded day(s):"]
    for d in days:
        rows = list(load_day(d))
        samples = sum(len(r.get("samples", [])) for r in rows)
        size_mb = path_for(d).stat().st_size / 1_048_576
        lines.append(f"  {d}: {len(rows):>5} rows, {samples:>7,} samples, {size_mb:.2f} MB gzipped")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
