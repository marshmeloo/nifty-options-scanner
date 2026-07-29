"""
Transaction costs for a NIFTY options round trip.

WHY THIS EXISTS
---------------
Until this module, every P&L figure in the journal, on the dashboard and
in the R-multiple expectancy tables was GROSS. That is not a rounding
error at this trade size:

On a Rs 76.70 premium, 1 lot of 65, with a 1R stop distance of 23 points
(Rs 1,496), a realistic round trip costs roughly Rs 56 in statutory
charges alone -- about 0.038R -- rising to ~0.081R with a 1-point
bid-ask spread crossed both ways.

Measured gross expectancy at the current 2R target was -0.028R. Costs
are therefore 1.4x to 2.9x the entire measured edge deficit: the system
is materially more negative than the journal showed, and any future
"we're break-even now" reading taken from gross numbers would be wrong
by roughly 0.04-0.12R per trade.

Costs also bite harder at lower targets, because they're close to a
fixed R-cost amortised over a smaller win. At a 0.5R target they consume
~16% of the gross win; at 2R, ~4%. That sharpens the earlier finding
that lowering DEFAULT_TARGET_RR is worse than it first appears.

RATES ARE ASSUMPTIONS -- CHECK THEM
-----------------------------------
The defaults in config.py model a typical Indian discount broker as of
early 2026. Brokerage models differ, and statutory rates change by
circular. Substitute your broker's actual contract note values; the
point of this module is that the number exists and is explicit, not that
these particular rates are eternal truth.

Note the asymmetry that catches people out: STT on options is charged on
the SELL side only, and on premium (not notional).
"""

import config


def _rate(name: str, default: float) -> float:
    return getattr(config, name, default)


def round_trip(entry_price: float, exit_price: float, lots: int = 1,
               lot_size: int = None) -> dict:
    """
    All-in cost of opening and closing one long options position.

    Returns a breakdown dict. Slippage is NOT included here -- when
    bid/ask fills are on, crossing the spread is already reflected in the
    entry/exit prices themselves, and adding it again would double-count.
    """
    lot_size = lot_size or getattr(config, "NIFTY_LOT_SIZE", 65)
    qty = lots * lot_size
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty

    brokerage = _rate("BROKERAGE_PER_ORDER", 20.0) * 2          # two legs
    # STT: sell side only, on premium.
    stt = _rate("STT_RATE_SELL", 0.001) * sell_turnover
    exch = _rate("EXCHANGE_TXN_RATE", 0.00035) * (buy_turnover + sell_turnover)
    sebi = _rate("SEBI_TURNOVER_RATE", 0.000001) * (buy_turnover + sell_turnover)
    stamp = _rate("STAMP_DUTY_RATE_BUY", 0.00003) * buy_turnover  # buy side only
    gst = _rate("GST_RATE", 0.18) * (brokerage + exch + sebi)

    total = brokerage + stt + exch + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exch, 2),
        "sebi": round(sebi, 2),
        "stamp_duty": round(stamp, 2),
        "gst": round(gst, 2),
        "total_inr": round(total, 2),
    }


def cost_in_r(entry_price: float, exit_price: float, stop_price: float,
              lots: int = 1, lot_size: int = None) -> float:
    """
    Round-trip cost expressed in R (the trade's own risk unit), which is
    the only unit in which it can be compared against expectancy.
    Returns None when the risk unit is degenerate.
    """
    lot_size = lot_size or getattr(config, "NIFTY_LOT_SIZE", 65)
    risk_unit = entry_price - stop_price
    if risk_unit <= 0:
        return None
    risk_inr = risk_unit * lots * lot_size
    total = round_trip(entry_price, exit_price, lots, lot_size)["total_inr"]
    return round(total / risk_inr, 4)


def apply_to_trade(trade: dict) -> dict:
    """
    Compute cost fields for a closed trade. Returns the fields to merge;
    does NOT overwrite the gross figures, which stay for comparison.

    Keeping both is deliberate: gross-vs-net is the clearest way to see
    how much of a strategy's apparent performance is being handed to the
    exchange, and silently replacing gross would hide that.
    """
    entry, exit_px = trade.get("entry"), trade.get("exit_ltp")
    if entry is None or exit_px is None:
        return {}

    lots = trade.get("lots", 1)
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)
    breakdown = round_trip(entry, exit_px, lots, lot_size)
    total = breakdown["total_inr"]

    gross_inr = trade.get("pnl_inr")
    fields = {
        "costs_inr": total,
        "cost_breakdown": breakdown,
    }
    if gross_inr is not None:
        net_inr = round(gross_inr - total, 2)
        fields["pnl_inr_net"] = net_inr
        invested = entry * lots * lot_size
        if invested:
            fields["pnl_pct_net"] = round(net_inr / invested * 100, 2)

    stop = trade.get("stop")
    if stop is not None and entry - stop > 0:
        risk_inr = (entry - stop) * lots * lot_size
        fields["cost_r"] = round(total / risk_inr, 4)
        if gross_inr is not None:
            fields["r_at_exit_net"] = round((gross_inr - total) / risk_inr, 3)
    return fields


def describe_assumptions() -> str:
    """One-glance summary of the rates in force, for session startup."""
    return (
        "Cost model (VERIFY against your own contract notes):\n"
        f"  brokerage      Rs {_rate('BROKERAGE_PER_ORDER', 20.0):.2f}/order x2\n"
        f"  STT            {_rate('STT_RATE_SELL', 0.001) * 100:.4f}% of premium, SELL side only\n"
        f"  exchange txn   {_rate('EXCHANGE_TXN_RATE', 0.00035) * 100:.4f}% both legs\n"
        f"  stamp duty     {_rate('STAMP_DUTY_RATE_BUY', 0.00003) * 100:.4f}% buy side only\n"
        f"  GST            {_rate('GST_RATE', 0.18) * 100:.0f}% on brokerage + txn + SEBI"
    )


if __name__ == "__main__":
    print(describe_assumptions())
    print()
    entry, exit_px, stop = 76.7, 53.69, 53.69
    bd = round_trip(entry, exit_px)
    print(f"Example: entry {entry}, exit {exit_px}, 1 lot")
    for k, v in bd.items():
        print(f"  {k:>14}: {v}")
    print(f"  in R terms  : {cost_in_r(entry, exit_px, stop):.4f}R")
