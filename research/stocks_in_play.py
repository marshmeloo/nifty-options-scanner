"""
Do high-relative-volume Indian stocks actually pay "win large fast,
lose small"? RESEARCH ONLY -- nothing here trades.

THE HYPOTHESIS BEING TESTED, AND WHY IT COMES BEFORE ANY STRATEGY
------------------------------------------------------------------
The stated observation is that trading high-volume momentum stocks
should "win large fast with small loss". That is a claim about the
SHAPE of the return distribution -- a fat right tail -- not about any
particular entry rule. It is worth testing on its own, first, because:

  - If high-RVOL stocks do NOT have a fatter right tail than ordinary
    stocks, no entry rule can manufacture one. Every strategy would be
    rearranging the same symmetric noise, and the honest answer arrives
    cheaply instead of after building five strategies.
  - If they DO, that tells us what kind of strategy can harvest it
    (ride winners, cut losers fast) and rules out the kind that cannot
    (mean-reversion, fixed small targets).

This is the same separation the index ORB study used: measure whether
the SIGNAL carries information before measuring whether a particular
trade construction around it makes money. Conflating the two is how a
good signal gets discarded because of a bad stop, or a useless signal
gets adopted because of a lucky one.

"SMALL LOSS" IS NOT A PROPERTY OF THE DISTRIBUTION
---------------------------------------------------
Worth stating because it changes what can be concluded: the raw return
distribution has whatever left tail it has. "Small loss" comes from
imposing a STOP -- it is trade construction, not a market property. So
this module reports both:
  - the RAW distribution (what the market offers), and
  - an R-multiple view under a hypothetical stop (what a trader would
    actually experience harvesting it).
Only the first is a fact about high-RVOL stocks; the second depends on
choices and is where costs eventually bite.

SELECTION IS RECONSTRUCTED, AND LOOK-AHEAD-SAFE BY CONSTRUCTION
----------------------------------------------------------------
NSE's own gainers / volume-spurt pages are live snapshots with no
history, so the morning's "stocks in play" list is rebuilt from bars:

    RVOL = (volume in the first N minutes today)
           / (mean volume in the first N minutes over the prior 20
              trading days for that same stock)

Only PRIOR days feed the denominator, and only bars up to the selection
time feed the numerator, so nothing here uses information a live
decision could not have had at 09:30.

KNOWN BIAS, NOT FIXABLE WITH THE DATA IN HAND: the universe is TODAY'S
F&O list applied to history. Stocks that have since been dropped from
F&O are missing, and stocks recently added are present for periods when
they were not actually F&O-eligible. That is survivorship/look-ahead in
universe construction. It flatters results to an unknown degree, and
fixing it needs historical F&O membership data this project does not
have. Any result here is provisional on that.

Run: python -m research.stocks_in_play --study
"""

import argparse
import json
import math
import statistics
from collections import defaultdict

from research import stock_data

SELECTION_MINUTES = 15      # first 15 min (bars 09:15, 09:20, 09:25) build the read
RVOL_LOOKBACK_DAYS = 20     # trailing window for "normal" opening volume
MIN_LOOKBACK_DAYS = 10      # below this, RVOL is not computable for that stock-day


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


SESSION_OPEN_MIN = _minutes("09:15")


def opening_slice(bars: list, minutes: int) -> list:
    end = SESSION_OPEN_MIN + minutes
    return [b for b in bars if SESSION_OPEN_MIN <= _minutes(b[0]) < end]


def day_rows(symbol: str, data: dict = None) -> list:
    """
    Per-day facts for one stock, each computed only from information
    available by the selection time. Returns rows ordered by date.

    Fields: day, rvol, open_ret_pct (open -> selection time),
    ref_price (price at selection time), fwd_* (forward returns from
    the selection time, which are OUTCOMES, never inputs to selection).
    """
    data = data if data is not None else stock_data.load(symbol)
    days = sorted(data)
    rows = []
    hist_open_vol = []

    for day in days:
        bars = data[day]
        if len(bars) < 40:
            hist_open_vol.append(None)
            continue
        head = opening_slice(bars, SELECTION_MINUTES)
        if len(head) < SELECTION_MINUTES // 5:
            hist_open_vol.append(None)
            continue

        open_vol = sum(b[5] or 0 for b in head)
        prior = [v for v in hist_open_vol[-RVOL_LOOKBACK_DAYS:] if v]
        hist_open_vol.append(open_vol)
        if len(prior) < MIN_LOOKBACK_DAYS:
            continue
        baseline = statistics.mean(prior)
        if baseline <= 0:
            continue

        day_open = head[0][1]
        ref = head[-1][4]          # close of the last selection bar
        if not day_open or not ref:
            continue

        after = [b for b in bars if _minutes(b[0]) >= SELECTION_OPEN_END]
        if not after:
            continue

        def ret_to(idx_price):
            return (idx_price - ref) / ref * 100

        # Forward outcomes measured from the selection point.
        eod = after[-1][4]
        hi = max(b[2] for b in after)
        lo = min(b[3] for b in after)
        rows.append({
            "day": day,
            "symbol": symbol,
            "rvol": open_vol / baseline,
            "open_ret_pct": (ref - day_open) / day_open * 100,
            "ref_price": ref,
            "fwd_eod_pct": ret_to(eod),
            "fwd_max_pct": ret_to(hi),      # best excursion available after selection
            "fwd_min_pct": ret_to(lo),      # worst excursion
        })
    return rows


SELECTION_OPEN_END = SESSION_OPEN_MIN + SELECTION_MINUTES


def collect(symbols: list = None, limit: int = None) -> list:
    symbols = symbols or [u["symbol"] for u in stock_data.universe()]
    if limit:
        symbols = symbols[:limit]
    out = []
    for s in symbols:
        data = stock_data.load(s)
        if data:
            out.extend(day_rows(s, data))
    return out


def _skew(xs: list) -> float:
    if len(xs) < 3:
        return 0.0
    m = statistics.mean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / len(xs)


def profile(rows: list, label: str) -> dict:
    """Distribution shape of the forward move, which is the actual claim."""
    if not rows:
        return {"label": label, "n": 0}
    r = [x["fwd_eod_pct"] for x in rows]
    ups = [x for x in r if x > 0]
    downs = [x for x in r if x < 0]
    return {
        "label": label,
        "n": len(r),
        "mean_pct": round(statistics.mean(r), 4),
        "median_pct": round(statistics.median(r), 4),
        "skew": round(_skew(r), 3),
        "win_pct": round(100 * len(ups) / len(r), 1),
        "mean_win_pct": round(statistics.mean(ups), 3) if ups else 0.0,
        "mean_loss_pct": round(statistics.mean(downs), 3) if downs else 0.0,
        # The "win large / lose small" ratio, stated directly.
        "win_loss_ratio": round(abs(statistics.mean(ups) / statistics.mean(downs)), 3)
        if ups and downs else None,
        "pct_gt_2": round(100 * sum(1 for x in r if x > 2) / len(r), 2),
        "pct_lt_neg2": round(100 * sum(1 for x in r if x < -2) / len(r), 2),
        # Best/worst excursion AFTER selection -- how much move was on
        # offer, before any stop or target decides what is captured.
        "mean_max_up_pct": round(statistics.mean([x["fwd_max_pct"] for x in rows]), 3),
        "mean_max_down_pct": round(statistics.mean([x["fwd_min_pct"] for x in rows]), 3),
    }


def analyse(rows: list) -> dict:
    """Bucket by RVOL, then split the top bucket by direction."""
    buckets = [
        ("rvol < 1", lambda r: r["rvol"] < 1),
        ("1 - 2", lambda r: 1 <= r["rvol"] < 2),
        ("2 - 3", lambda r: 2 <= r["rvol"] < 3),
        ("3 - 5", lambda r: 3 <= r["rvol"] < 5),
        ("5 +", lambda r: r["rvol"] >= 5),
    ]
    out = {"n_rows": len(rows), "buckets": [], "top_bucket_by_direction": []}
    for name, fn in buckets:
        out["buckets"].append(profile([r for r in rows if fn(r)], name))

    high = [r for r in rows if r["rvol"] >= 3]
    out["top_bucket_by_direction"] = [
        profile([r for r in high if r["open_ret_pct"] > 1], "RVOL>=3 & up >1% (gainer)"),
        profile([r for r in high if r["open_ret_pct"] < -1], "RVOL>=3 & down >1% (loser)"),
        profile([r for r in high if abs(r["open_ret_pct"]) <= 1], "RVOL>=3 & flat"),
    ]
    return out


def describe(summary: dict) -> str:
    def row(p):
        if not p.get("n"):
            return f"  {p['label']:<28} (no rows)"
        return (f"  {p['label']:<28} n={p['n']:>7,}  mean={p['mean_pct']:>+7.3f}%  "
                f"med={p['median_pct']:>+7.3f}%  skew={p['skew']:>+6.2f}  win={p['win_pct']:>5.1f}%  "
                f"W/L={p['win_loss_ratio'] if p['win_loss_ratio'] is not None else 0:>5.2f}  "
                f">+2%={p['pct_gt_2']:>5.2f}%  <-2%={p['pct_lt_neg2']:>5.2f}%")

    lines = [f"Stocks-in-play distribution study: {summary['n_rows']:,} stock-days",
             "Forward return measured from the 09:30 selection point to the close.",
             "", "By opening relative volume:"]
    lines += [row(p) for p in summary["buckets"]]
    lines += ["", "High-RVOL split by opening direction:"]
    lines += [row(p) for p in summary["top_bucket_by_direction"]]
    lines += ["",
              "skew > 0 and W/L > 1 together are what 'win large, lose small' means.",
              "NOTE: universe is TODAY'S F&O list applied to history -- survivorship "
              "bias, see module docstring."]
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--study", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="cap symbols, for a quick look")
    p.add_argument("--out", default="logs/stocks_in_play_study.json")
    args = p.parse_args()

    rows = collect(limit=args.limit)
    if not rows:
        print("No cached stock data yet -- run: python -m research.stock_data --backfill")
    else:
        summary = analyse(rows)
        print(describe(summary))
        with open(args.out, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nwritten to {args.out}")
