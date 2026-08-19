"""
What correlated-cluster-cap band does BANK NIFTY actually want?

WHY THIS EXISTS
---------------
The live cluster cap (`config.CLUSTER_CAP_ADJACENCY_POINTS = 200`,
`_WINDOW_MINUTES = 30`, Sentinel-only) was backtested against NIFTY's
history ONLY -- main_live_banknifty_sentinel.py says so in its own
docstring -- and then applied unchanged to Bank Nifty. Two reasons that
is unlikely to be right:

  - **Proportion.** 200pt is 0.83% of NIFTY at ~24,000 but only 0.35% of
    Bank Nifty at ~57,000. The proportionally equivalent Bank Nifty band
    is closer to 475pt.
  - **Strike spacing.** NIFTY strikes are 50pt apart, Bank Nifty's are
    100pt. A 200pt band therefore reaches 4 strikes either side on
    NIFTY but only 2 on Bank Nifty -- so it can only ever thin a Bank
    Nifty cluster, never collapse it.

Live evidence that this matters, three separate sessions: 2026-08-10
(8 CE trades, 8 strikes, Rs -5,153), 2026-08-17 (9-strike Bank Nifty
cluster), and 2026-08-19 (5 Bank Nifty CE trades opened inside 115
seconds, Rs -11,436 -- two of them 300pt apart, straight through the
200pt band).

WHAT IS MEASURED, AND WHY NET PROFIT ALONE IS THE WRONG ANSWER
---------------------------------------------------------------
A cluster cap can only ever REMOVE trades. Against a backtest as
profitable as Bank Nifty momentum (+Rs 3M over 1,244 days) almost any
cap will cut net profit, so ranking cells by net Rs would mechanically
pick "no cap" every time and answer nothing.

The cap exists to cut DRAWDOWN -- the failure mode is many correlated
positions moving against you at once. So the real question is the
trade-off, and the bar is the one NIFTY's own Sentinel was adopted on:
it gave up 14% of profit for a 62% smaller max drawdown. A Bank Nifty
band is interesting only if it lands somewhere comparable.

Reported per cell: trades, net Rs, max drawdown, profit retained vs the
uncapped control, and drawdown reduction vs that control.

METHOD
------
  - Bank Nifty's own recorded history (logs/snapshots_banknifty,
    1,244 days) via a patched loader -- shadow.py loads NIFTY's
    directory by default.
  - Bank Nifty's LIVE config (lot 30, premium 300-800, strike range
    2000), copied from main_live_banknifty.py so the backtest scores the
    same universe the live process does.
  - 5 independent ~1-year periods, the same split BACKLOG's 2026-08-13
    Bank Nifty momentum validation used, so a band has to survive regime
    changes rather than ride one good year.
  - Window held at 30min (the live value) while the BAND is swept --
    sweeping both at once multiplies the comparison count for an axis
    that already has a documented answer.

Run: python sweep_banknifty_cluster_cap.py
"""

import argparse
import json
from pathlib import Path

import config
import shadow
import snapshot_recorder

BN_SNAPSHOT_DIR = Path(__file__).parent / "logs" / "snapshots_banknifty"

# Copied from main_live_banknifty.py's own config block.
BN_CONFIG = {
    "NIFTY_LOT_SIZE": 30,
    "PREMIUM_MIN": 300.0,
    "PREMIUM_MAX": 800.0,
    "STRIKE_RANGE_POINTS": 2000,
}

# None = uncapped control (today's real Anchor behaviour). 200 = the
# NIFTY-derived value currently applied to Bank Nifty. The rest span up
# to and past the ~475pt proportional equivalent.
BAND_GRID = [None, 200.0, 300.0, 400.0, 500.0, 600.0, 800.0]
CLUSTER_WINDOW_MINUTES = 30.0

PERIODS = [
    ("Y1 2021-08..2022-07", "2021-08-01", "2022-07-31"),
    ("Y2 2022-08..2023-07", "2022-08-01", "2023-07-31"),
    ("Y3 2023-08..2024-07", "2023-08-01", "2024-07-31"),
    ("Y4 2024-08..2025-07", "2024-08-01", "2025-07-31"),
    ("Y5 2025-08..2026-08", "2025-08-01", "2026-12-31"),
]


def patch_for_banknifty():
    """Point shadow.py at Bank Nifty's snapshots and config."""
    original = snapshot_recorder.load_day

    def patched(day, snapshot_dir=None, symbol="NIFTY"):
        return original(day, snapshot_dir=snapshot_dir or BN_SNAPSHOT_DIR, symbol="BANKNIFTY")

    snapshot_recorder.load_day = patched
    for k, v in BN_CONFIG.items():
        setattr(config, k, v)
    return original


def max_drawdown(trades: list) -> float:
    """
    Peak-to-trough of the cumulative net-P&L curve, trades ordered by
    close time. Returns a POSITIVE rupee figure (0.0 if never underwater
    relative to a prior peak).
    """
    closed = sorted([t for t in trades if t.net_inr is not None and t.closed_at],
                    key=lambda t: t.closed_at)
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for t in closed:
        equity += t.net_inr
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def run_cell(days: list, band) -> dict:
    policy = shadow.Policy(
        name=f"band={band}",
        strike_adjacency_band_points=band,
        cluster_window_minutes=CLUSTER_WINDOW_MINUTES if band is not None else None,
        use_learned_adjustment=False,   # journal postdates this history
    )
    trades = []
    for day in days:
        try:
            trades.extend(shadow.run_policy(day, policy))
        except Exception:
            pass
    usable = [t for t in trades if t.net_inr is not None]
    return {
        "n": len(usable),
        "net_inr": round(sum(t.net_inr for t in usable)),
        "max_dd_inr": round(max_drawdown(usable)),
        "win_pct": round(100 * sum(1 for t in usable if t.net_inr > 0) / len(usable), 1) if usable else None,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/sweep_banknifty_cluster_cap.json")
    args = p.parse_args()

    patch_for_banknifty()
    all_days = snapshot_recorder.available_days(snapshot_dir=BN_SNAPSHOT_DIR)
    print(f"{len(all_days)} Bank Nifty days: {all_days[0]} .. {all_days[-1]}")
    print(f"config: lot={config.NIFTY_LOT_SIZE} premium={config.PREMIUM_MIN}-{config.PREMIUM_MAX} "
          f"window={CLUSTER_WINDOW_MINUTES:g}min\n", flush=True)

    results = {}
    for band in BAND_GRID:
        label = "none" if band is None else f"{band:g}"
        per_period = {}
        for pname, start, end in PERIODS:
            days = [d for d in all_days if start <= d <= end]
            per_period[pname] = run_cell(days, band) if days else {"n": 0, "net_inr": 0, "max_dd_inr": 0}
        total_n = sum(r["n"] for r in per_period.values())
        total_net = sum(r["net_inr"] for r in per_period.values())
        worst_dd = max(r["max_dd_inr"] for r in per_period.values())
        pos = sum(1 for r in per_period.values() if r["net_inr"] > 0)
        results[label] = {
            "band": band, "total_n": total_n, "total_net_inr": total_net,
            "worst_period_dd_inr": worst_dd, "periods_positive": pos,
            "per_period": per_period,
        }
        print(f"  band={label:<5} n={total_n:>6,}  net=Rs {total_net:>+11,}  "
              f"worst-period DD=Rs {worst_dd:>10,}  {pos}/5 periods positive", flush=True)

    control = results["none"]
    base_net, base_dd = control["total_net_inr"], control["worst_period_dd_inr"]
    print(f"\n{'band':<8} {'trades':>7} {'net Rs':>12} {'profit kept':>12} "
          f"{'worst DD':>12} {'DD cut':>8} {'pos':>5}")
    for label, r in results.items():
        kept = (r["total_net_inr"] / base_net * 100) if base_net else float("nan")
        ddcut = (1 - r["worst_period_dd_inr"] / base_dd) * 100 if base_dd else float("nan")
        print(f"{label:<8} {r['total_n']:>7,} {r['total_net_inr']:>+12,} {kept:>11.1f}% "
              f"{r['worst_period_dd_inr']:>12,} {ddcut:>7.1f}% {r['periods_positive']:>4}/5")

    with open(args.out, "w") as f:
        json.dump({"window_minutes": CLUSTER_WINDOW_MINUTES, "bn_config": BN_CONFIG,
                   "periods": [p[0] for p in PERIODS], "results": results}, f, indent=2)
    print(f"\nwritten to {args.out}")
    print("\nBar for interest: NIFTY's Sentinel was adopted giving up ~14% of profit "
          "for a ~62% smaller max drawdown. A band is only worth adopting if the "
          "trade-off is comparable AND it holds across periods.")


if __name__ == "__main__":
    main()
