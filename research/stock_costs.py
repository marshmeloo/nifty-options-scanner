"""
Intraday EQUITY cost model for the stocks-in-play research track.
RESEARCH ONLY.

WHY NOT REUSE costs.py
----------------------
costs.py models OPTIONS: STT at 0.10% of premium sell-side, exchange
transaction at 0.035%. Intraday equity is a different schedule
entirely -- STT 0.025% sell-side, exchange transaction ~0.00297%,
stamp duty 0.003% buy-side. Reusing the options numbers would overstate
costs by roughly an order of magnitude on the statutory side and
quietly invalidate every result.

THE PART THAT ACTUALLY DECIDES THE ANSWER IS NOT IN THIS TABLE
---------------------------------------------------------------
Statutory charges are small, known, and boring. SLIPPAGE is what kills
intraday equity strategies, and it is the reason this module refuses to
pick a single number:

An independent replication of the Zarattini QQQ paper reproduced the
strategy's mechanics almost exactly and then found it BREAK-EVEN at
~2.2 cents/share of slippage -- the entire published edge lived inside
the bid-ask spread of QQQ, one of the most liquid instruments on earth.
Indian single stocks are wider, and a relative-volume screen
PREFERENTIALLY SURFACES the less-liquid ones, because unusual volume is
the selection criterion. So slippage is not a detail to bolt on at the
end; it is the hypothesis's main adversary.

This project also cannot MEASURE stock spreads from the data it has:
the backfill holds OHLCV only, no bid/ask. Rather than invent a spread
number and present results as if it were measured, the intended use is:

    report BREAK-EVEN SLIPPAGE and let the reader judge it against what
    these names plausibly trade at

which is what break_even_slippage_bps() exists for. A strategy whose
break-even slippage is comparable to the typical spread of the stocks
it selects is not tradeable, however good its gross numbers look.

RATES are the standard published Indian discount-broker/statutory
schedule as of 2026-08. They are approximate and broker-dependent;
they are also not where the answer lives (see above), so precision here
matters far less than the slippage treatment.
"""

BROKERAGE_PCT = 0.0003          # 0.03% per executed order...
BROKERAGE_CAP_PER_ORDER = 20.0  # ...capped at Rs 20, typical discount broker
STT_RATE_SELL = 0.00025         # 0.025%, SELL side only (intraday equity)
EXCHANGE_TXN_RATE = 0.0000297   # NSE equity, both sides
SEBI_TURNOVER_RATE = 0.000001   # Rs 10 per crore
STAMP_DUTY_RATE_BUY = 0.00003   # 0.003%, BUY side only
GST_RATE = 0.18                 # on brokerage + exchange + SEBI


def statutory_costs(entry: float, exit_price: float, qty: int) -> dict:
    """
    Brokerage + taxes for one intraday round trip, in rupees.
    Direction-agnostic: STT applies to whichever leg is the sell, and a
    long and a short round trip both have exactly one of each.
    """
    buy_turnover = entry * qty
    sell_turnover = exit_price * qty

    brokerage = sum(min(t * BROKERAGE_PCT, BROKERAGE_CAP_PER_ORDER)
                    for t in (buy_turnover, sell_turnover))
    stt = STT_RATE_SELL * sell_turnover
    exch = EXCHANGE_TXN_RATE * (buy_turnover + sell_turnover)
    sebi = SEBI_TURNOVER_RATE * (buy_turnover + sell_turnover)
    stamp = STAMP_DUTY_RATE_BUY * buy_turnover
    gst = GST_RATE * (brokerage + exch + sebi)

    total = brokerage + stt + exch + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi,
            "stamp": stamp, "gst": gst, "total": total}


def slippage_cost(entry: float, exit_price: float, qty: int, slippage_bps: float) -> float:
    """
    Rupee cost of crossing the spread on BOTH legs, at `slippage_bps`
    basis points of price per leg. 10 bps = 0.10% per side.
    """
    return (entry + exit_price) * qty * (slippage_bps / 10_000)


def round_trip(entry: float, exit_price: float, qty: int, slippage_bps: float = 0.0) -> dict:
    s = statutory_costs(entry, exit_price, qty)
    slip = slippage_cost(entry, exit_price, qty, slippage_bps)
    s["slippage"] = slip
    s["total"] = s["total"] + slip
    return s


def break_even_slippage_bps(trades: list) -> float:
    """
    The per-leg slippage, in basis points, at which a set of trades nets
    exactly zero. THE headline number for this research track.

    `trades`: dicts with gross_inr (already net of statutory costs) and
    turnover_inr (entry+exit notional). Returns 0.0 if the set is
    already unprofitable before slippage, and inf if it somehow has no
    turnover to charge against.

    Interpretation: compare the result against the spread these names
    actually trade at. If break-even slippage is ~5 bps and the stocks
    a relative-volume screen surfaces trade at 10-20 bps, the strategy
    is dead regardless of how good the gross figures look -- which is
    exactly how the QQQ replication concluded.
    """
    gross = sum(t["gross_inr"] for t in trades)
    turnover = sum(t["turnover_inr"] for t in trades)
    if gross <= 0:
        return 0.0
    if turnover <= 0:
        return float("inf")
    return gross / turnover * 10_000
