"""
Does order-flow imbalance -- book_imbalance / total_quantity_imbalance,
recorded on every scanned candidate since 2026-08-07 -- actually predict
the CONTRACT'S OWN forward return? BACKLOG.md ("Order flow: wired for
reliability, book_imbalance recorded but NOT gated on") flagged this as
an open question deliberately left untested until there was real data
to test it against, and said it deserved "the same forward-return
treatment component_study.py applied to the momentum scorer" rather than
an assumption that order flow is informative. This is that treatment.

WHY DECISION_LOG.JSONL, NOT SNAPSHOT_RECORDER
----------------------------------------------
component_study.py replays reconstructed historical chains via
snapshot_recorder + scanner.scan() across 6 years. Book imbalance can't
be reconstructed that way -- it comes from Dhan's live WebSocket depth
feed (orderflow_feed.py), which only exists going FORWARD from when
decision_log.py started recording it on every candidate (2026-08-07).
So this script replays decision_log.jsonl's own live candidate history
instead: every logged candidate already carries book_imbalance,
total_quantity_imbalance, and its own entry price at candidate time
(candidate["plan"]["entry"]). Forward return is computed the same way
component_study.py does it -- find the same (strike, option_type) key
in a later cycle's candidate list and take the price change -- just
sourced from live decision_log rows instead of reconstructed chains.
Same coverage limitation component_study.py has: a candidate only
counts if the scanner still flags that same contract far enough ahead
(near enough to spot, etc.) -- not something new here.

WHAT "PREDICTIVE" MEANS HERE
-----------------------------
Forward return of the OPTION CONTRACT ITSELF (not the underlying) over
a fixed horizon, bucketed by the imbalance reading at candidate time.
book_imbalance/total_quantity_imbalance are in [-1, +1], positive =
more resting buy size than sell size on that contract. If the signal
is informative, a more positive reading should predict a more positive
forward return of that same contract. Deliberately ignores stop/target
path, same reasoning as component_study.py: this measures whether the
SIGNAL carries information, separately from trade construction.

Run: python -m research.order_flow_predictive_study
"""

import argparse
import json
import math
import statistics
from datetime import datetime


def load_cycles(paths: list) -> list:
    cycles = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts, candidates = d.get("timestamp"), d.get("candidates")
                if not ts or not candidates:
                    continue
                cycles.append((datetime.fromisoformat(ts), candidates))
    cycles.sort(key=lambda c: c[0])
    return cycles


def candidate_rows(cycles: list, horizon_minutes: int = 30) -> list:
    """One row per (cycle, candidate) that had an imbalance reading and a
    same-key candidate at least horizon_minutes later, same day."""
    rows = []
    n = len(cycles)
    for i, (ts, candidates) in enumerate(cycles):
        future = None
        for j in range(i + 1, n):
            if (cycles[j][0] - ts).total_seconds() / 60 >= horizon_minutes:
                future = cycles[j]
                break
        if future is None:
            continue
        future_ts, future_candidates = future
        if ts.date() != future_ts.date():
            continue  # don't let a horizon window cross into the next session

        future_prices = {}
        for c in future_candidates:
            entry = (c.get("plan") or {}).get("entry")
            if entry:
                future_prices[(c.get("strike"), c.get("option_type"))] = entry

        for c in candidates:
            bi, tqi = c.get("book_imbalance"), c.get("total_quantity_imbalance")
            if bi is None and tqi is None:
                continue
            now_px = (c.get("plan") or {}).get("entry")
            key = (c.get("strike"), c.get("option_type"))
            later_px = future_prices.get(key)
            if not now_px or not later_px:
                continue
            rows.append({
                "book_imbalance": bi,
                "total_quantity_imbalance": tqi,
                "option_type": c.get("option_type"),
                "ret": (later_px - now_px) / now_px * 100,
            })
    return rows


def _pearson(xs: list, ys: list) -> float:
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def analyse_signal(rows: list, key: str, n_buckets: int = 5) -> dict:
    pts = [(r[key], r["ret"]) for r in rows if r.get(key) is not None]
    if len(pts) < n_buckets * 30:
        return {"n": len(pts), "buckets": [], "note": "too thin to bucket"}
    pts.sort(key=lambda p: p[0])
    n = len(pts)
    size = n // n_buckets

    buckets, bucket_rets = [], []
    for b in range(n_buckets):
        start = b * size
        end = (b + 1) * size if b < n_buckets - 1 else n
        chunk = pts[start:end]
        vals, rets = [v for v, _ in chunk], [r for _, r in chunk]
        bucket_rets.append(rets)
        buckets.append({
            "bucket": b + 1,
            "n": len(chunk),
            "value_range": [round(min(vals), 3), round(max(vals), 3)],
            "mean_ret_pct": round(statistics.mean(rets), 4),
        })

    lo, hi = bucket_rets[0], bucket_rets[-1]
    m_lo, m_hi = statistics.mean(lo), statistics.mean(hi)
    se_lo = statistics.pstdev(lo) / math.sqrt(len(lo)) if len(lo) > 1 else 0
    se_hi = statistics.pstdev(hi) / math.sqrt(len(hi)) if len(hi) > 1 else 0
    sed = math.sqrt(se_lo ** 2 + se_hi ** 2)
    z = (m_hi - m_lo) / sed if sed > 0 else None

    xs, ys = [v for v, _ in pts], [r for _, r in pts]
    r = _pearson(xs, ys)
    t = r * math.sqrt(n - 2) / math.sqrt(1 - r ** 2) if abs(r) < 1 else None

    return {
        "n": n, "buckets": buckets,
        "top_vs_bottom_z": round(z, 2) if z is not None else None,
        "pearson_r": round(r, 4),
        "pearson_t": round(t, 2) if t is not None else None,
    }


def describe(summary: dict, horizon_minutes: int) -> str:
    lines = [f"Order-flow predictive-value study -- {horizon_minutes}min horizon"]
    for key, label in (("book_imbalance", "book_imbalance (top-5 depth)"),
                       ("total_quantity_imbalance", "total_quantity_imbalance (exchange-wide)")):
        r = summary.get(key)
        lines.append("")
        lines.append(f"-- {label} --")
        if not r or not r.get("buckets"):
            lines.append(f"  n={r.get('n', 0) if r else 0}: too thin, no result")
            continue
        lines.append(f"  n={r['n']:,}   Q5-vs-Q1 z={r['top_vs_bottom_z']:+}   "
                     f"pearson r={r['pearson_r']:+.4f} (t={r['pearson_t']:+})")
        lines.append(f"  {'bucket':<8}{'value range':<20}{'n':>7}{'mean ret%':>12}")
        for b in r["buckets"]:
            lines.append(f"  Q{b['bucket']:<7}{str(b['value_range']):<20}"
                         f"{b['n']:>7,}{b['mean_ret_pct']:>+12.4f}")
    lines.append("")
    lines.append("Q1 = most negative (ask-heavy / sell pressure), Q5 = most positive")
    lines.append("(bid-heavy / buy pressure). If the signal is informative, mean")
    lines.append("return should rise monotonically Q1 -> Q5 and Q5-vs-Q1 |z| > ~2.")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", nargs="+", default=["logs/decision_log.jsonl"],
                   help="decision_log.jsonl file(s) to replay")
    p.add_argument("--horizon", type=int, default=30, help="minutes ahead to measure")
    p.add_argument("--out", default="logs/order_flow_predictive_study.json")
    args = p.parse_args()

    cycles = load_cycles(args.log)
    print(f"{len(cycles):,} cycles loaded from {args.log}", flush=True)
    if cycles:
        print(f"  {cycles[0][0]} .. {cycles[-1][0]}", flush=True)

    rows = candidate_rows(cycles, horizon_minutes=args.horizon)
    print(f"{len(rows):,} candidate rows with an imbalance reading and a forward price\n", flush=True)

    summary = {
        "n_rows": len(rows),
        "horizon_minutes": args.horizon,
        "book_imbalance": analyse_signal(rows, "book_imbalance"),
        "total_quantity_imbalance": analyse_signal(rows, "total_quantity_imbalance"),
    }

    print(describe(summary, args.horizon))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
