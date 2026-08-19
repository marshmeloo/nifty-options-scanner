"""
Records REAL bid/ask spreads for the F&O stock universe during market
hours. RESEARCH ONLY.

WHY THIS EXISTS
---------------
The stocks-in-play sweep produced one live-looking cell (pullback on
high-RVOL decliners) whose viability rests entirely on one unmeasured
number: its worst-year break-even slippage of 10.6 bps. Everything up
to now has judged that against PLAUSIBLE spread ranges, because the
historical backfill holds OHLCV only -- Dhan publishes no historical
bid/ask. Judging a strategy against a guessed cost is precisely how the
published US version survived peer scrutiny until an independent
replication measured the spread and found the entire edge inside it.

So this measures the number instead of arguing about it.

WHY IT HAS TO RUN LIVE, AND WHY TIMING IS THE WHOLE POINT
-----------------------------------------------------------
Two traps, both of which this project has already been bitten by once
(see spread_study.py, which refuses to report a trading-cost figure
from non-regular-session samples):

  - **Closed-market quotes are worthless.** Probed at 05:16 with the
    market shut, the depth book comes back ONE-SIDED -- a real price on
    one side and a literal zero on the other. Naively computing
    (ask-bid) there produces enormous fake spreads, or worse, a
    plausible-looking small one if only one level is missing.
  - **Spread is not constant through the day.** The strategy under test
    enters at ~09:30, minutes after the open, when spreads are at their
    widest. Sampling at 13:00 and calling it "the spread" would
    understate the strategy's real cost precisely where it matters.

Hence: sample the WHOLE universe once a minute for the whole session,
and let the analysis slice by time of day rather than assuming one
number.

COST: 208 instruments fit in a single /marketfeed/quote request (the
endpoint takes up to 1000), so one sample = one API call. At 60s
spacing that is ~375 calls per session against a 1 req/sec budget --
negligible, and it shares the limiter with everything else.

Run:  python -m research.stock_spread_recorder          # waits for the open, records the session
      python -m research.stock_spread_recorder --once   # single sample now (for testing)
      python -m research.stock_spread_recorder --describe
"""

import argparse
import gzip
import json
import os
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

import requests

import dhan_rate_limiter
import dhan_source
from research import stock_data

OUT_DIR = Path(__file__).parent.parent / "logs" / "stock_spreads"
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SAMPLE_SECONDS = 60


def path_for(day: str = None) -> Path:
    day = day or date.today().isoformat()
    return OUT_DIR / f"{day.replace('-', '')}.jsonl.gz"


def market_is_open(now: datetime = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def fetch_quotes(security_ids: list) -> dict:
    """{security_id: {bid, ask, bid_qty, ask_qty, ltp}} for a usable book.

    A contract whose top level is missing on EITHER side is DROPPED, not
    recorded with a zero. A zero is not a price, and letting one through
    would silently produce either an absurd spread or a fake tight one
    depending on which side is missing.
    """
    dhan_rate_limiter.wait_for_ltp_slot()
    resp = requests.post(
        f"{dhan_source.DHAN_BASE_URL}/marketfeed/quote",
        headers=dhan_source._headers(),
        json={"NSE_EQ": list(security_ids)},
        timeout=20,
    )
    resp.raise_for_status()
    seg = (resp.json().get("data") or {}).get("NSE_EQ") or {}

    out = {}
    for sid, q in seg.items():
        depth = q.get("depth") or {}
        buy = (depth.get("buy") or [{}])[0]
        sell = (depth.get("sell") or [{}])[0]
        bid, ask = buy.get("price") or 0, sell.get("price") or 0
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        out[int(sid)] = {
            "b": bid, "a": ask,
            "bq": buy.get("quantity") or 0, "aq": sell.get("quantity") or 0,
            "ltp": q.get("last_price"), "vol": q.get("volume"),
        }
    return out


def spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000 if mid > 0 else None


def append_sample(quotes: dict, ts: datetime = None):
    ts = ts or datetime.now()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    row = {"t": ts.strftime("%H:%M:%S"), "q": quotes}
    with gzip.open(path_for(), "at", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def record_session(sample_seconds: int = SAMPLE_SECONDS):
    ids = [u["security_id"] for u in stock_data.universe()]
    print(f"recording spreads for {len(ids)} symbols every {sample_seconds}s", flush=True)

    while not market_is_open():
        now = datetime.now()
        if now.weekday() >= 5:
            print("weekend -- nothing to record"); return
        if now.time() > MARKET_CLOSE:
            print("session already over for today"); return
        time.sleep(30)

    print(f"market open at {datetime.now():%H:%M:%S} -- sampling", flush=True)
    n = 0
    while market_is_open():
        try:
            q = fetch_quotes(ids)
            append_sample(q)
            n += 1
            if n % 15 == 0:
                print(f"  {datetime.now():%H:%M}  {n} samples, {len(q)} live books", flush=True)
        except Exception as e:
            print(f"  {datetime.now():%H:%M} sample failed: {type(e).__name__}", flush=True)
        time.sleep(sample_seconds)
    print(f"session ended, {n} samples -> {path_for()}", flush=True)


def load(day: str = None) -> list:
    p = path_for(day)
    if not p.exists():
        return []
    out = []
    with gzip.open(p, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def describe(day: str = None) -> str:
    rows = load(day)
    if not rows:
        return f"no spread recording for {day or date.today().isoformat()}"
    import statistics
    all_bps = []
    for r in rows:
        for sid, q in r["q"].items():
            s = spread_bps(q["b"], q["a"])
            if s is not None:
                all_bps.append(s)
    return "\n".join([
        f"{len(rows)} samples, {rows[0]['t']} .. {rows[-1]['t']}",
        f"{len(all_bps):,} quote observations",
        f"spread bps: median {statistics.median(all_bps):.2f}  "
        f"p25 {sorted(all_bps)[len(all_bps)//4]:.2f}  p75 {sorted(all_bps)[3*len(all_bps)//4]:.2f}",
    ])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true")
    p.add_argument("--describe", action="store_true")
    p.add_argument("--day", default=None)
    p.add_argument("--interval", type=int, default=SAMPLE_SECONDS)
    args = p.parse_args()

    if args.describe:
        print(describe(args.day))
    elif args.once:
        ids = [u["security_id"] for u in stock_data.universe()]
        q = fetch_quotes(ids)
        print(f"{len(q)} usable books of {len(ids)} requested"
              f"{'  (market closed -- expect few/none)' if not market_is_open() else ''}")
        for sid, v in list(q.items())[:5]:
            print(f"  {sid}: bid={v['b']} ask={v['a']} spread={spread_bps(v['b'], v['a']):.2f} bps")
    else:
        record_session(args.interval)
