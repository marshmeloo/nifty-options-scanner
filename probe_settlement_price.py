"""
One-off diagnostic: does Dhan's option-chain response expose a real,
exchange-published settlement/closing price for a strike, distinct from
`last_price`? Open question from the 2026-08-04 incident (see
BACKLOG.md's "Verify whether Dhan exposes a real settlement price"
entry) -- condor/directional-spread expiry settlement currently uses
"whichever LTP happened to land closest to 15:40" as a proxy, because
this was never checked directly.

Now that CAS (SEBI's Closing Auction Session, live since 2026-08-03)
is confirmed to be the mechanism behind the 15:15 cash-market freeze
and the ~15:40 options close, there's a specific thing to look for: a
field that behaves differently inside the 15:15-15:40 auction window --
e.g. stays null/zero until the auction settles around 15:30-15:35, then
populates with a value distinct from whatever last_price is doing.
`last_price` itself is expected to keep moving through this window (it
did on 2026-08-04); a genuine settlement field should NOT.

Usage:
    python3 probe_settlement_price.py                 # one-shot sample, both indices
    python3 probe_settlement_price.py --watch          # resample every 60s until 15:45
    python3 probe_settlement_price.py --watch --interval 30

Requires DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID set (same as any live run).
The option-chain endpoint only responds during market hours -- outside
that window this will fail fast with an HTTP error, not hang.

Read-only. Never places or modifies anything.
"""

import argparse
import json
import time
from datetime import datetime, time as dtime
from pathlib import Path

import dhan_source as ds

OUT_DIR = Path(__file__).parent / "logs"
OUT_DIR.mkdir(exist_ok=True)
OUT_PATH = OUT_DIR / f"settlement_price_probe_{datetime.now().strftime('%Y%m%d')}.jsonl"

# Known price-ish field name so far; anything else in a side's dict is
# a candidate worth a human look, especially if it doesn't just track
# last_price.
KNOWN_PRICE_KEY = "last_price"

INDICES = [
    ("NIFTY", ds.NIFTY_UNDERLYING_SCRIP, ds.NIFTY_UNDERLYING_SEG),
    ("BANKNIFTY", ds.BANKNIFTY_UNDERLYING_SCRIP, ds.BANKNIFTY_UNDERLYING_SEG),
]


def _sample_one(label: str, scrip: int, seg: str) -> dict:
    expiry = ds.get_nearest_expiry(scrip, seg)
    raw = ds._fetch_raw_chain(expiry, scrip, seg)

    top_level_keys = sorted(raw.keys())
    oc = raw.get("oc", {})
    strikes = sorted(oc.keys(), key=float) if oc else []

    sample_strike = None
    ce_keys, pe_keys = [], []
    if strikes:
        mid = strikes[len(strikes) // 2]
        sample_strike = mid
        entry = oc[mid]
        ce_keys = sorted(entry.get("ce", {}).keys())
        pe_keys = sorted(entry.get("pe", {}).keys())

    return {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "index": label,
        "expiry": expiry,
        "underlying_last_price": raw.get("last_price"),
        "top_level_keys": top_level_keys,
        "sample_strike": sample_strike,
        "ce_keys": ce_keys,
        "pe_keys": pe_keys,
        "ce_full": oc[sample_strike]["ce"] if sample_strike else None,
        "pe_full": oc[sample_strike]["pe"] if sample_strike else None,
    }


def _print_summary(rec: dict):
    extra_ce = [k for k in rec["ce_keys"] if k != KNOWN_PRICE_KEY]
    print(f"[{rec['captured_at']}] {rec['index']}  expiry={rec['expiry']}  "
          f"underlying_last_price={rec['underlying_last_price']}")
    print(f"  strike {rec['sample_strike']} CE fields: {rec['ce_keys']}")
    print(f"  strike {rec['sample_strike']} PE fields: {rec['pe_keys']}")
    if extra_ce:
        print(f"  >>> fields beyond '{KNOWN_PRICE_KEY}' -- inspect these: {extra_ce}")
        print(f"      CE full record: {json.dumps(rec['ce_full'], indent=2)}")


def run_once():
    for label, scrip, seg in INDICES:
        try:
            rec = _sample_one(label, scrip, seg)
        except Exception as e:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] {label}: FAILED -- {e}")
            continue
        _print_summary(rec)
        with open(OUT_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    print(f"\nAppended to {OUT_PATH}")


def run_watch(interval_seconds: int, stop_at: dtime):
    print(f"Watching every {interval_seconds}s until {stop_at.strftime('%H:%M')} -- "
          f"the window to actually watch is ~15:10 to 15:45 (CAS auction + options close).")
    while datetime.now().time() < stop_at:
        run_once()
        time.sleep(interval_seconds)
    print("Stop time reached.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Resample repeatedly instead of once")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between samples in --watch mode")
    parser.add_argument("--stop-at", default="15:45", help="HH:MM to stop watching (default 15:45)")
    args = parser.parse_args()

    if args.watch:
        hh, mm = (int(x) for x in args.stop_at.split(":"))
        run_watch(args.interval, dtime(hh, mm))
    else:
        run_once()
