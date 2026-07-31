"""
Gate: does historical_source.py's reconstruction actually match what we
recorded live on the same day?

WHY THIS EXISTS
---------------
Every backtest conclusion rests entirely on the reconstructed data being
the same market the live system saw. If it isn't -- wrong strike
alignment, bar timestamps off by one interval, a different price
convention -- the backtest still produces confident-looking win rates and
equity curves, just for a market that never existed. That failure is
silent by construction: nothing errors, the numbers simply mean nothing.

There is exactly one way to catch it, and it's available only for the
handful of days we have BOTH sources for: reconstruct a day we also
recorded live, and compare them field by field. This must pass before any
strategy result built on historical data is trusted.

WHAT IS AND ISN'T EXPECTED TO MATCH
-----------------------------------
Expected to match closely:
  - spot at a matched timestamp (same underlying, same instant)
  - per-strike LTP, OI and IV at a matched timestamp

Expected to differ, legitimately:
  - Cadence. Live records every ~30s; historical is 5-minute bars. Each
    historical bar is matched to the NEAREST live cycle, and the residual
    time offset is reported -- a systematic offset near one full interval
    means the bar timestamp convention (start vs end of bar) is wrong,
    which is precisely the kind of silent misalignment this exists to
    catch.
  - LTP vs bar close. Live LTP is the last trade at that instant;
    historical is the 5-minute bar's close. Small differences are
    expected and healthy; large ones are not.
  - Strike coverage. Live carries +/-STRIKE_RANGE_POINTS (800 by
    default); historical caps at ATM+/-10 (+/-500). Only the strikes
    present in BOTH are compared.
  - bid/ask and Greeks are absent from historical entirely and are not
    compared (see historical_source's docstring).
"""

import argparse
import logging
import statistics
from collections import defaultdict

import historical_source
import snapshot_recorder

log = logging.getLogger("nifty_scanner")

# A matched pair further apart in time than this is not the same market
# instant and comparing them says nothing. Half a 5-minute bar.
MAX_MATCH_SECONDS = 150

# Thresholds for the pass/fail verdict. Deliberately loose on price (LTP
# vs bar close genuinely differ) and tight on spot (same underlying at
# the same instant has no excuse to differ).
MAX_MEDIAN_SPOT_DIFF = 5.0        # points
MAX_MEDIAN_LTP_DIFF_PCT = 2.0     # percent
MAX_MEDIAN_OFFSET_SECONDS = 60.0  # systematic timestamp skew


def _match_cycles(historical, live):
    """
    Pair each historical snapshot with the nearest live cycle in time.

    Live cycles are consumed in order and never reused, so two historical
    bars can't both match the same live cycle and inflate agreement.
    """
    live = sorted(live, key=lambda s: s.timestamp)
    pairs = []
    i = 0
    for h in sorted(historical, key=lambda s: s.timestamp):
        best = None
        while i < len(live):
            gap = abs((live[i].timestamp - h.timestamp).total_seconds())
            if best is not None and gap > best[0]:
                break
            best = (gap, i)
            i += 1
        if best is None:
            continue
        i = best[1] + 1
        if best[0] <= MAX_MATCH_SECONDS:
            pairs.append((h, live[best[1]], (live[best[1]].timestamp - h.timestamp).total_seconds()))
    return pairs


def compare(day: str, interval: str = "5") -> dict:
    """Reconstruct `day` historically and diff it against the live recording."""
    live = [snap for snap, _candles, _meta in snapshot_recorder.load_day(day)]
    if not live:
        raise RuntimeError(f"No live recording on disk for {day} -- nothing to validate against.")

    hist = historical_source.reconstruct_range(day, day, interval=interval)
    if not hist:
        raise RuntimeError(f"Historical reconstruction returned nothing for {day}.")

    pairs = _match_cycles(hist, live)
    if not pairs:
        raise RuntimeError(
            f"No historical bar landed within {MAX_MATCH_SECONDS}s of a live cycle on {day} -- "
            "the two sources do not overlap in time at all."
        )

    spot_diffs, offsets = [], []
    ltp_diff_pcts, oi_diff_pcts, iv_diffs = [], [], []
    compared_strikes = 0

    for h, l, offset in pairs:
        spot_diffs.append(abs(h.spot - l.spot))
        offsets.append(offset)

        live_by_key = {(q.strike, q.option_type): q for q in l.chain}
        for hq in h.chain:
            lq = live_by_key.get((hq.strike, hq.option_type))
            if lq is None:
                continue
            compared_strikes += 1
            if lq.ltp:
                ltp_diff_pcts.append(abs(hq.ltp - lq.ltp) / lq.ltp * 100)
            if lq.oi:
                oi_diff_pcts.append(abs(hq.oi - lq.oi) / lq.oi * 100)
            if lq.iv and hq.iv:
                iv_diffs.append(abs(hq.iv - lq.iv))

    def _med(xs):
        return round(statistics.median(xs), 3) if xs else None

    result = {
        "day": day,
        "live_cycles": len(live),
        "historical_cycles": len(hist),
        "matched_pairs": len(pairs),
        "compared_quotes": compared_strikes,
        "median_offset_seconds": _med(offsets),
        "median_spot_diff": _med(spot_diffs),
        "max_spot_diff": round(max(spot_diffs), 2) if spot_diffs else None,
        "median_ltp_diff_pct": _med(ltp_diff_pcts),
        "median_oi_diff_pct": _med(oi_diff_pcts),
        "median_iv_diff": _med(iv_diffs),
    }

    failures = []
    if result["median_spot_diff"] is not None and result["median_spot_diff"] > MAX_MEDIAN_SPOT_DIFF:
        failures.append(f"spot differs by {result['median_spot_diff']}pts (limit {MAX_MEDIAN_SPOT_DIFF})")
    if result["median_ltp_diff_pct"] is not None and result["median_ltp_diff_pct"] > MAX_MEDIAN_LTP_DIFF_PCT:
        failures.append(f"LTP differs by {result['median_ltp_diff_pct']}% (limit {MAX_MEDIAN_LTP_DIFF_PCT}%)")
    if result["median_offset_seconds"] is not None and abs(result["median_offset_seconds"]) > MAX_MEDIAN_OFFSET_SECONDS:
        failures.append(
            f"systematic {result['median_offset_seconds']}s timestamp skew "
            f"(limit {MAX_MEDIAN_OFFSET_SECONDS}s) -- suspect a bar start/end convention mismatch")
    if not compared_strikes:
        failures.append("no strike appeared in both sources -- strike alignment is wrong")

    result["failures"] = failures
    result["passed"] = not failures
    return result


def describe(result: dict) -> str:
    lines = [
        f"Historical vs live: {result['day']}",
        f"  cycles           live {result['live_cycles']}, historical {result['historical_cycles']}",
        f"  matched pairs    {result['matched_pairs']}  ({result['compared_quotes']} quotes compared)",
        f"  timestamp skew   median {result['median_offset_seconds']}s",
        f"  spot             median {result['median_spot_diff']}pts, max {result['max_spot_diff']}pts",
        f"  LTP              median {result['median_ltp_diff_pct']}%",
        f"  OI               median {result['median_oi_diff_pct']}%",
        f"  IV               median {result['median_iv_diff']} vol pts",
        "",
    ]
    if result["passed"]:
        lines.append("  PASS -- reconstruction matches the live recording.")
    else:
        lines.append("  FAIL -- do NOT trust backtests built on this data:")
        lines.extend(f"    - {f}" for f in result["failures"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Validate historical reconstruction against a live recording.")
    parser.add_argument("--day", required=True, help="YYYY-MM-DD (must have a live recording on disk)")
    parser.add_argument("--interval", default="5", choices=["1", "5", "15", "25", "60"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(describe(compare(args.day, interval=args.interval)))


if __name__ == "__main__":
    main()
