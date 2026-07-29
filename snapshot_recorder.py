"""
Records the raw inputs to every decision cycle, so any future version of
the decision logic can be replayed against identical market history.

WHY THIS EXISTS
---------------
Until this module, data collection and decision logic were welded
together: the only way to gather evidence was to run the live logic, so
every change to that logic invalidated all prior evidence. With ~4 trades
a day and a per-trade R standard deviation around 1.0, distinguishing a
real edge from zero needs on the order of 800-1,100 trades -- roughly
200-280 trading days of UNCHANGED logic. That is not a realistic way to
develop a system, and it means "did that change help?" was, honestly,
unanswerable.

Recording the raw inputs breaks the deadlock. Market history accumulates
regardless of what the code does, and any variant can be replayed over
it offline (see replay.py).

WHAT IS RECORDED, AND WHAT THAT DOES AND DOESN'T BUY YOU
-------------------------------------------------------
Each cycle stores the full option chain as OptionQuote records plus the
candles used for price-action analysis. Because the per-strike raw
observables are all present -- ltp, oi, iv, volume, Greeks -- most
downstream logic can be RECOMPUTED at replay time rather than merely
replayed:

  - IV percentile (cross-sectional, or a future smile-residual version)
    only needs every strike's iv + strike, both recorded.
  - OI buildup classification needs oi/ltp across consecutive cycles,
    which sequential replay reproduces.
  - Price-action structure, scoring, plan geometry, and risk gates are
    all pure functions of the recorded inputs.

What is NOT recoverable is anything requiring fields the broker returned
but this model never carried (e.g. full depth, or bid/ask until
OptionQuote grows those fields). Recording starts capturing a field the
moment the model has it -- so widening the model widens future replay
value, never past.

Storage is ~9 MB/day raw, roughly 1 MB gzipped at a 30s cadence.
"""

import gzip
import json
import logging
from dataclasses import asdict
from datetime import datetime, date
from pathlib import Path

import config
from models import MarketSnapshot, OptionQuote, Candle

log = logging.getLogger("nifty_scanner")

SNAPSHOT_DIR = Path(__file__).parent / "logs" / "snapshots"

# Fields on OptionQuote/Candle that are datetimes and need explicit
# round-tripping (json has no native datetime).
_DT_FIELDS = ("timestamp",)


def path_for(day) -> Path:
    """logs/snapshots/YYYYMMDD.jsonl.gz for a date or ISO date string."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return SNAPSHOT_DIR / f"{day.strftime('%Y%m%d')}.jsonl.gz"


def _encode_dt(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _quote_to_dict(q: OptionQuote) -> dict:
    d = asdict(q)
    for f in _DT_FIELDS:
        if f in d:
            d[f] = _encode_dt(d[f])
    return d


def _candle_to_dict(c: Candle) -> dict:
    d = asdict(c)
    d["timestamp"] = _encode_dt(d["timestamp"])
    return d


def _decode_dt(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def _quote_from_dict(d: dict) -> OptionQuote:
    """
    Tolerant of schema drift in BOTH directions: unknown keys from a
    newer recording are dropped, and fields added to the model after a
    recording was made simply take their defaults. Without this, adding
    one field to OptionQuote would make every previously recorded day
    unreadable -- which would defeat the whole point of recording.
    """
    known = {f for f in OptionQuote.__dataclass_fields__}
    kwargs = {k: v for k, v in d.items() if k in known}
    for f in _DT_FIELDS:
        if f in kwargs:
            kwargs[f] = _decode_dt(kwargs[f])
    return OptionQuote(**kwargs)


def _candle_from_dict(d: dict) -> Candle:
    known = {f for f in Candle.__dataclass_fields__}
    kwargs = {k: v for k, v in d.items() if k in known}
    kwargs["timestamp"] = _decode_dt(kwargs.get("timestamp"))
    return Candle(**kwargs)


def record(snapshot: MarketSnapshot, candles: list = None, logic_version: str = None) -> bool:
    """
    Append one cycle's raw inputs. Returns True if written.

    NEVER raises: recording is observability, and a full disk or a
    serialisation edge case must not take down a loop that may be
    managing open positions. Failures are logged and swallowed.
    """
    if not getattr(config, "RECORD_SNAPSHOTS", True):
        return False
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        record_obj = {
            "timestamp": _encode_dt(snapshot.timestamp),
            "logic_version": logic_version,
            "spot": snapshot.spot,
            "vwap": snapshot.vwap,
            "pcr": snapshot.pcr,
            "source": snapshot.source,
            "chain": [_quote_to_dict(q) for q in snapshot.chain],
            "candles": [_candle_to_dict(c) for c in (candles or [])],
        }
        day = snapshot.timestamp.date() if isinstance(snapshot.timestamp, datetime) else date.today()
        with gzip.open(path_for(day), "at", encoding="utf-8") as f:
            f.write(json.dumps(record_obj, default=str) + "\n")
        return True
    except Exception as e:
        log.info(f"  (snapshot recording failed this cycle, continuing: {e})")
        return False


def available_days() -> list:
    """Recorded days, oldest first, as ISO date strings."""
    if not SNAPSHOT_DIR.exists():
        return []
    days = []
    for p in SNAPSHOT_DIR.glob("*.jsonl.gz"):
        stem = p.name.split(".")[0]
        try:
            days.append(datetime.strptime(stem, "%Y%m%d").date().isoformat())
        except ValueError:
            continue
    return sorted(days)


def load_day(day):
    """
    Yields (MarketSnapshot, candles, meta) per recorded cycle, in order.

    A corrupt trailing line is skipped rather than aborting the day --
    a gzip stream cut mid-write by a kill leaves exactly that, and one
    truncated cycle shouldn't cost you the session.
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
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                snapshot = MarketSnapshot(
                    symbol="NIFTY",
                    spot=rec["spot"],
                    vwap=rec["vwap"],
                    pcr=rec["pcr"],
                    chain=[_quote_from_dict(q) for q in rec.get("chain", [])],
                    timestamp=_decode_dt(rec["timestamp"]),
                    source=rec.get("source", "replay"),
                )
                candles = [_candle_from_dict(c) for c in rec.get("candles", [])]
            except (KeyError, TypeError):
                continue
            yield snapshot, candles, {"logic_version": rec.get("logic_version")}


def describe() -> str:
    """Human summary of what history is on disk, for CLI output."""
    days = available_days()
    if not days:
        return "No recorded snapshots yet."
    lines = [f"{len(days)} recorded day(s):"]
    for d in days:
        p = path_for(d)
        cycles = sum(1 for _ in load_day(d))
        lines.append(f"  {d}: {cycles:>5} cycles, {p.stat().st_size / 1_048_576:.2f} MB gzipped")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
