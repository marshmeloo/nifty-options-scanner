"""
Does capping entries at ONE per day change the picture for a realistic
Rs1,00,000 retail account? RESEARCH ONLY -- nothing here trades.

WHY THIS IS A DIFFERENT QUESTION FROM THE MAIN BREAKEVEN-ARM STUDY
---------------------------------------------------------------------
breakeven_arm_study.py (and the dashboard built from it) uses the live
system's own Policy defaults -- no cap on trades per day beyond what the
scanner's gates naturally produce, averaging ~9/day for Anchor. That
volume was exactly what justified Rs5,00,000 (config.TOTAL_CAPITAL) as
the sizing base, per plan_generator.py's real risk-budget calculation.

Restricting to ONE trade per day (shadow.Policy.max_trades_per_day=1 --
the FIRST candidate that clears every gate that day, chronologically,
not the best one in hindsight) is a genuinely different, much lower-
volume strategy. It is worth asking on its own terms with a smaller,
more realistic capital base for that lower volume: Rs1,00,000 here,
not carried over from the main study.

METHOD
------
Same shadow.py reconstruction, same fixed exit-price walker as
ratchet_study.py / breakeven_arm_study.py (exits at the price actually
observed, never at the trigger level). Institutional metrics computed
the same way as the dashboard's JS: max drawdown % against the FIXED
Rs1,00,000 base (never a growing peak -- see breakeven_arm_study.py's
own note on why that matters), Calmar off the same fixed base,
capital-at-risk per trade = (entry-stop)*NIFTY_LOT_SIZE (exact, since
MAX_LOTS_PER_TRADE=1 caps every trade actually taken to 1 lot).

Run: python -m research.one_trade_per_day_study
"""

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import date

import config
import shadow
from research.breakeven_arm_study import (
    historical_nifty_days, VARIANTS, SENTINEL_POLICY_KWARGS, walk_with_ratchet,
    monthly_breakdown)
from research.ratchet_study import summarise

START_CAPITAL = 100000


def run_variant_1pd(days, tiers, **policy_kwargs):
    """Same as breakeven_arm_study.run_variant, plus max_trades_per_day=1."""
    original = shadow.walk_trade_forward
    shadow.walk_trade_forward = (
        lambda index, key, entry_ts, trade, lots=1:
        walk_with_ratchet(index, key, entry_ts, trade, tiers, lots))
    policy = shadow.Policy(name="1-trade-per-day", use_learned_adjustment=False,
                           max_trades_per_day=1, **policy_kwargs)
    trades = []
    try:
        for day in days:
            try:
                trades.extend(shadow.run_policy(day, policy))
            except Exception as e:
                print(f"    {day} failed: {type(e).__name__}", flush=True)
    finally:
        shadow.walk_trade_forward = original
    return trades


def to_rows(trades):
    return [[t.opened_at[:10], round(t.net_inr or 0), round(t.net_r or 0, 3),
             round((t.entry - t.stop) * config.NIFTY_LOT_SIZE)]
            for t in trades if t.outcome]


def day_level(rows):
    by_day = defaultdict(lambda: {"pnl": 0, "risk": 0, "n": 0})
    for d, net, r, risk in rows:
        by_day[d]["pnl"] += net
        by_day[d]["risk"] += risk
        by_day[d]["n"] += 1
    days = list(by_day.values())
    win_days = [x for x in days if x["pnl"] > 0]
    loss_days = [x for x in days if x["pnl"] <= 0]
    return {
        "trading_days": len(days),
        "avg_trades_per_day": len(rows) / len(days) if days else 0,
        "avg_capital_per_day": sum(x["risk"] for x in days) / len(days) if days else 0,
        "avg_win_day_pnl": statistics.mean([x["pnl"] for x in win_days]) if win_days else 0,
        "avg_loss_day_pnl": statistics.mean([x["pnl"] for x in loss_days]) if loss_days else 0,
    }


def max_dd_pct_fixed(rows, capital):
    sorted_rows = sorted(rows, key=lambda r: r[0])
    equity = peak = capital
    worst = 0
    for r in sorted_rows:
        equity += r[1]
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst / capital * 100


def institutional_metrics(rows, capital=START_CAPITAL):
    n = len(rows)
    wins = [r for r in rows if r[1] > 0]
    losses = [r for r in rows if r[1] <= 0]
    total_profit = sum(r[1] for r in rows)
    final_capital = capital + total_profit
    total_return_pct = total_profit / capital * 100
    gross_win = sum(r[1] for r in wins)
    gross_loss = abs(sum(r[1] for r in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    expectancy_r = sum(r[2] for r in rows) / n if n else 0
    avg_win_r = statistics.mean([r[2] for r in wins]) if wins else 0
    avg_loss_r = statistics.mean([r[2] for r in losses]) if losses else 0
    max_dd_pct = max_dd_pct_fixed(rows, capital)

    dates = sorted(r[0] for r in rows)
    day_span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    years = day_span / 365.25
    # A fractional power of a NEGATIVE base returns a COMPLEX number in
    # Python, not an error -- so a variant that loses more than its whole
    # capital silently produced e.g. calmar = -0.17+0.33j and carried it
    # through the JSON and the printed table. Seen for real on
    # 2026-09-01: an RSI(5)>=70 exhaustion gate drove Bank Nifty to
    # -156.9% return, and its Calmar came back complex.
    #
    # A wiped-out book has no meaningful annualised rate, so report -100%
    # (everything and more was lost) rather than an imaginary one.
    if years < 0.1:
        ann_return_pct = None
    elif final_capital <= 0:
        ann_return_pct = -100.0
    else:
        ann_return_pct = ((final_capital / capital) ** (1 / years) - 1) * 100
    calmar = ann_return_pct / max_dd_pct if (ann_return_pct is not None and max_dd_pct > 0) else None

    return {
        "n": n, "win_rate_pct": len(wins) / n * 100 if n else 0,
        "total_profit": total_profit, "total_return_pct": total_return_pct,
        "final_capital": final_capital, "max_dd_pct": max_dd_pct, "calmar": calmar,
        "profit_factor": profit_factor, "expectancy_r": expectancy_r,
        "avg_win_r": avg_win_r, "avg_loss_r": avg_loss_r,
        **day_level(rows),
    }


def describe(all_metrics: dict) -> str:
    lines = [f"One-trade-per-day, Rs{START_CAPITAL:,.0f} account, 6yr reconstructed NIFTY", ""]
    cols = list(all_metrics.keys())
    lines.append(f"{'metric':<24}" + "".join(f"{c:>20}" for c in cols))
    rows_spec = [
        ("Total trades", "n", "{:,}"),
        ("Win rate", "win_rate_pct", "{:.1f}%"),
        ("Total profit", "total_profit", "Rs{:,.0f}"),
        ("Total return", "total_return_pct", "{:+.1f}%"),
        ("Final capital", "final_capital", "Rs{:,.0f}"),
        ("Max drawdown", "max_dd_pct", "{:.1f}%"),
        ("Calmar ratio", "calmar", "{:.2f}"),
        ("Profit factor", "profit_factor", "{:.2f}"),
        ("Expectancy", "expectancy_r", "{:+.3f}R"),
        ("Avg winning trade", "avg_win_r", "{:+.2f}R"),
        ("Avg losing trade", "avg_loss_r", "{:+.2f}R"),
        ("Trading days", "trading_days", "{:,}"),
        ("Avg trades/day", "avg_trades_per_day", "{:.2f}"),
        ("Avg capital used/day", "avg_capital_per_day", "Rs{:,.0f}"),
        ("Avg winning-day profit", "avg_win_day_pnl", "Rs{:,.0f}"),
        ("Avg losing-day loss", "avg_loss_day_pnl", "Rs{:,.0f}"),
    ]
    for label, key, fmt in rows_spec:
        vals = []
        for c in cols:
            v = all_metrics[c].get(key)
            vals.append(fmt.format(v) if v is not None else "--")
        lines.append(f"{label:<24}" + "".join(f"{v:>20}" for v in vals))
    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/one_trade_per_day.json")
    args = p.parse_args()

    days = historical_nifty_days()
    print(f"{len(days)} reconstructed NIFTY days\n", flush=True)

    configs = {
        "anchor_no_rule": (VARIANTS["no_rule"], {}),
        "anchor_breakeven": (VARIANTS["breakeven_0.5R"], {}),
        "sentinel_no_rule": (VARIANTS["no_rule"], SENTINEL_POLICY_KWARGS),
        "sentinel_breakeven": (VARIANTS["breakeven_0.5R"], SENTINEL_POLICY_KWARGS),
    }

    all_metrics, all_rows = {}, {}
    for name, (tiers, kwargs) in configs.items():
        print(f"running {name}...", flush=True)
        trades = run_variant_1pd(days, tiers, **kwargs)
        rows = to_rows(trades)
        all_rows[name] = rows
        all_metrics[name] = institutional_metrics(rows)
        print(f"  {len(rows)} trades")

    print()
    print(describe(all_metrics))

    with open(args.out, "w") as f:
        json.dump({"metrics": all_metrics, "rows": all_rows}, f, indent=2)
    print(f"\nwritten to {args.out}")
