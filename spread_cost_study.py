"""
What does the directional credit spread's backtest edge look like once
REAL costs are charged?

WHY THIS EXISTS
---------------
shadow_directional_spread.py applies NO cost model at all -- its
pnl_inr is fully gross: no brokerage, no STT, no exchange/GST, and no
bid-ask crossing (historical reconstruction carries no book, so every
leg is priced at LTP). Its headline result -- 137 trades, 87.6% win,
+Rs 81,445, positive in all three calendar years, surviving removal of
its own biggest winner -- is therefore an UPPER BOUND, not a net
number. This module turns it into a net one.

WHERE THE SPREAD FIGURE COMES FROM -- MEASURED, NOT ASSUMED
------------------------------------------------------------
spread_study.py against 5 days of real recorded order books
(2026-08-03..07, prod, 3,849 sampled cycles) measured the spread INSIDE
this strategy's own Rs 40-70 short-premium band:

    median 0.259%   p75 0.311%   p90 0.340%

All three are reported below rather than one point estimate, because a
strategy whose verdict flips between the median and the p90 case is not
robust and the reader needs to see that directly.

WHY SPREAD IS CHARGED ON GROSS PREMIUM, NOT NET CREDIT
--------------------------------------------------------
A credit spread's net_credit is (short_premium - hedge_premium), but
both legs are real orders that each cross their own book. Charging the
spread on the net would understate the cost roughly 3-4x at this
strategy's typical leg prices. Cost is therefore computed per leg, on
that leg's own premium.

LEG-CROSSINGS PER ROUND TRIP
-----------------------------
Opening crosses 2 legs (sell the short, buy the hedge). Closing crosses
2 more -- EXCEPT on expiry settlement, where the position is settled by
the exchange rather than traded out, so no closing spread is paid. The
exit_reason on each trade decides which applies, rather than assuming
one uniformly.

WHAT THIS STILL DOES NOT MODEL
-------------------------------
Where inside the spread a fill actually lands (this charges the full
quoted crossing, the conservative end for a marketable order), partial
fills, and slippage on a large order sweeping multiple book levels.
"""

import argparse
import json
import statistics
from pathlib import Path

import config_directional_spread as dcfg

# Measured from real recorded books inside this strategy's own premium
# band -- see this module's docstring. NOT assumed.
MEASURED_SPREAD_PCT = {"median": 0.259, "p75": 0.311, "p90": 0.340}

# Spread BY PREMIUM LEVEL, same 5-day recorded-book measurement
# (spread_study.py, regular session only). A flat figure is fine for the
# directional spread, whose two legs both sit in the Rs 40-70 range, but
# NOT for the iron condor: its shorts price around Rs 23-30 while its
# hedges (300pts further OTM) price under Rs 10, where percentage
# spreads are dramatically worse. Verified on a real live condor:
# hedges at Rs 5.65 / Rs 8.40, i.e. squarely in the "under 20" bucket
# below. Costing those at the shorts' spread would understate the real
# cost several-fold.
MEASURED_SPREAD_BY_BAND = {
    #  (premium_low, premium_high): {scenario: spread % of mid}
    (0, 20):     {"median": 0.823, "p75": 3.636, "p90": 5.714},
    (20, 50):    {"median": 0.301, "p75": 0.343, "p90": 0.404},
    (50, 100):   {"median": 0.249, "p75": 0.279, "p90": 0.309},
    (100, 200):  {"median": 0.226, "p75": 0.251, "p90": 0.270},
    (200, 1e9):  {"median": 0.249, "p75": 0.271, "p90": 0.291},
}


def spread_pct_for_premium(premium: float, scenario: str = "median") -> float:
    """
    Measured spread % for a leg at this premium level. Deep-OTM legs
    (the condor's hedges) get their own, far worse, real figure rather
    than inheriting a near-ATM leg's.
    """
    for (lo, hi), row in MEASURED_SPREAD_BY_BAND.items():
        if lo <= premium < hi:
            return row[scenario]
    return MEASURED_SPREAD_BY_BAND[(200, 1e9)][scenario]


def multi_leg_costs(leg_premiums: list, scenario: str = "median",
                    lots: int = 1, lot_size: int = None,
                    expiry_settled: bool = False,
                    sold_legs: list = None) -> dict:
    """
    Statutory + spread cost for an arbitrary multi-leg structure, each
    leg costed at the spread measured for ITS OWN premium level.

    `sold_legs` is a parallel list of bools (True = this leg is SOLD at
    open, so STT applies to it). Defaults to all-sold, which is wrong
    for a hedge -- callers should pass it explicitly.

    Returns {"taxes", "spread", "total"} in rupees.
    """
    import config

    lot_size = lot_size or getattr(config, "NIFTY_LOT_SIZE", 65)
    qty = lots * lot_size
    sold_legs = sold_legs if sold_legs is not None else [True] * len(leg_premiums)

    def _rate(name, default):
        return getattr(config, name, default)

    n_legs = len(leg_premiums)
    n_orders = n_legs if expiry_settled else n_legs * 2
    brokerage = _rate("BROKERAGE_PER_ORDER", 20.0) * n_orders

    sell_turnover = sum(p * qty for p, sold in zip(leg_premiums, sold_legs) if sold)
    buy_turnover = sum(p * qty for p, sold in zip(leg_premiums, sold_legs) if not sold)
    if not expiry_settled:
        # Closing reverses every leg. Entry premiums are the honest
        # available proxy for exit turnover (see _brokerage_and_taxes).
        sell_turnover += buy_turnover
        buy_turnover += sum(p * qty for p, sold in zip(leg_premiums, sold_legs) if sold)

    stt = _rate("STT_RATE_SELL", 0.001) * sell_turnover
    exch = _rate("EXCHANGE_TXN_RATE", 0.00035) * (buy_turnover + sell_turnover)
    sebi = _rate("SEBI_TURNOVER_RATE", 0.000001) * (buy_turnover + sell_turnover)
    stamp = _rate("STAMP_DUTY_RATE_BUY", 0.00003) * buy_turnover
    gst = _rate("GST_RATE", 0.18) * (brokerage + exch + sebi)
    taxes = brokerage + stt + exch + sebi + stamp + gst

    crossings = 1 if expiry_settled else 2
    spread = sum(
        p * (spread_pct_for_premium(p, scenario) / 100.0) * qty * crossings
        for p in leg_premiums
    )
    return {"taxes": taxes, "spread": spread, "total": taxes + spread}

# Exit reasons that settle at expiry rather than being traded out, so
# no closing spread is crossed. Matched against shadow_directional_
# spread.py's own exit_reason values.
EXPIRY_SETTLED_REASONS = {"expiry_settlement", "EXPIRY_CLOSE", "expiry"}


def _brokerage_and_taxes(short_premium: float, hedge_premium: float,
                         lots: int = None, lot_size: int = None,
                         expiry_settled: bool = False) -> float:
    """
    Statutory + broker costs for a 2-leg credit spread round trip.

    Mirrors costs.py's own rate structure but for TWO legs opened and
    (usually) two closed, rather than costs.round_trip's single long
    position. Rates are read from config so this can't silently drift
    from the rest of the project's cost assumptions.
    """
    import config

    lots = lots if lots is not None else dcfg.LOTS_PER_LEG
    lot_size = lot_size or dcfg.NIFTY_LOT_SIZE
    qty = lots * lot_size

    def _rate(name, default):
        return getattr(config, name, default)

    n_orders = 2 if expiry_settled else 4
    brokerage = _rate("BROKERAGE_PER_ORDER", 20.0) * n_orders

    # Opening turnover: the short leg is SOLD (STT applies on sell side,
    # on premium), the hedge is BOUGHT.
    sell_turnover = short_premium * qty
    buy_turnover = hedge_premium * qty
    if not expiry_settled:
        # Closing reverses both. Exit premiums aren't recorded per leg;
        # using entry premiums as the turnover proxy is the honest
        # approximation available here, and is conservative for a
        # winning credit spread (whose legs are CHEAPER at exit).
        sell_turnover += hedge_premium * qty
        buy_turnover += short_premium * qty

    stt = _rate("STT_RATE_SELL", 0.001) * sell_turnover
    exch = _rate("EXCHANGE_TXN_RATE", 0.00035) * (buy_turnover + sell_turnover)
    sebi = _rate("SEBI_TURNOVER_RATE", 0.000001) * (buy_turnover + sell_turnover)
    stamp = _rate("STAMP_DUTY_RATE_BUY", 0.00003) * buy_turnover
    gst = _rate("GST_RATE", 0.18) * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def _spread_cost(short_premium: float, hedge_premium: float, spread_pct: float,
                 lots: int = None, lot_size: int = None,
                 expiry_settled: bool = False) -> float:
    """
    Cost of crossing the quoted bid-ask on every leg traded.

    Charged as (spread_pct/100) x leg premium x qty, per crossing --
    i.e. the full quoted spread, the cost of an immediate marketable
    fill. Both legs at open; both again at close unless settled at
    expiry.
    """
    lots = lots if lots is not None else dcfg.LOTS_PER_LEG
    lot_size = lot_size or dcfg.NIFTY_LOT_SIZE
    qty = lots * lot_size
    per_open = (short_premium + hedge_premium) * (spread_pct / 100.0) * qty
    return per_open if expiry_settled else per_open * 2


def analyse(path: str, spread_pcts: dict = None) -> dict:
    """
    Re-price a saved shadow_directional_spread result file with real
    costs. Returns gross vs net under each measured spread scenario.
    """
    spread_pcts = spread_pcts or MEASURED_SPREAD_PCT
    trades = json.load(open(path))
    resolved = [t for t in trades if t.get("pnl_inr") is not None]

    missing_legs = [t for t in resolved if t.get("short_premium") is None]
    usable = [t for t in resolved if t.get("short_premium") is not None]

    gross_total = sum(t["pnl_inr"] for t in resolved)
    out = {
        "n_resolved": len(resolved),
        "n_usable": len(usable),
        "n_missing_leg_premiums": len(missing_legs),
        "gross_total_inr": round(gross_total, 2),
        "scenarios": {},
    }
    if not usable:
        return out

    for label, pct in spread_pcts.items():
        net_pnls, tax_total, spread_total = [], 0.0, 0.0
        for t in usable:
            settled = (t.get("exit_reason") or "") in EXPIRY_SETTLED_REASONS
            tax = _brokerage_and_taxes(t["short_premium"], t["hedge_premium"],
                                       expiry_settled=settled)
            spr = _spread_cost(t["short_premium"], t["hedge_premium"], pct,
                               expiry_settled=settled)
            tax_total += tax
            spread_total += spr
            net_pnls.append(t["pnl_inr"] - tax - spr)

        wins = [p for p in net_pnls if p > 0]
        ranked = sorted(net_pnls, reverse=True)
        out["scenarios"][label] = {
            "spread_pct": pct,
            "n": len(net_pnls),
            "win_pct": round(100 * len(wins) / len(net_pnls), 1),
            "net_total_inr": round(sum(net_pnls), 2),
            "net_ex_biggest_inr": round(sum(ranked[1:]), 2) if len(ranked) > 1 else None,
            "avg_net_inr": round(statistics.mean(net_pnls), 2),
            "taxes_inr": round(tax_total, 2),
            "spread_cost_inr": round(spread_total, 2),
        }
    return out


def describe(result: dict, label: str = "") -> str:
    lines = [f"{label}  ({result['n_resolved']} resolved trades)"]
    if result["n_missing_leg_premiums"]:
        lines.append(
            f"  WARNING: {result['n_missing_leg_premiums']} trade(s) have no recorded leg "
            f"premiums and are EXCLUDED -- re-run the backtest to capture them "
            f"(shadow_directional_spread.ShadowSpread gained short_premium/hedge_premium "
            f"after those results were saved)."
        )
    lines.append(f"  GROSS (what the backtest reports): Rs {result['gross_total_inr']:>+12,.0f}")
    if not result["scenarios"]:
        lines.append("  no usable trades to cost")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"  {'scenario':<10}{'spread%':>9}{'n':>6}{'win%':>8}{'NET total':>15}{'net ex-biggest':>17}{'taxes':>12}{'spread cost':>14}")
    for name, s in result["scenarios"].items():
        lines.append(
            f"  {name:<10}{s['spread_pct']:>9.3f}{s['n']:>6}{s['win_pct']:>7.1f}%"
            f"{s['net_total_inr']:>+15,.0f}{(s['net_ex_biggest_inr'] or 0):>+17,.0f}"
            f"{s['taxes_inr']:>12,.0f}{s['spread_cost_inr']:>14,.0f}"
        )
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", nargs="+", help="saved shadow_directional_spread JSON result file(s)")
    args = p.parse_args()
    for path in args.results:
        print(describe(analyse(path), Path(path).name))
        print()


if __name__ == "__main__":
    main()
