"""
Shadow backtest for a LONG strangle: buy the same short_ce/short_pe
strikes the iron condor would have SOLD (same entry criteria --
config_condor.SHORT_PREMIUM_MIN/MAX + highest-OI liquidity screen via
condor_scanner.find_condor_legs), but as the buyer instead of the
seller. No hedge legs -- risk is already capped at the premium paid,
which is exactly the "less loss, more profit" SHAPE the condor's own
poor backtest result (BACKLOG.md, 2026-08-06) prompted investigating.

WHY THIS EXISTS, AND WHY IT IS NOT JUST "NEGATE THE CONDOR'S P&L"
--------------------------------------------------------------------
A live conversation found the condor's own 87-trade backtest was net
NEGATIVE (-Rs 18,941, 70.1% win rate -- big infrequent losses outweighing
small frequent wins) and asked whether reversing to a debit structure
would flip that. Mechanically negating the credit side's P&L (mirror
math, ignoring costs) says yes, but that is NOT evidence of a real
edge -- it just restates that the condor's entry TIMING underperformed
in that sample, from the other side of the same trade. This module
answers the real question properly: run the SAME strike-selection
logic as an actual buyer, through the SAME walk-forward/expiry-
settlement machinery as shadow_condor.py (LTP fills, no bid/ask in the
historical data, same optimism the condor backtest already carries),
so the two results are a fair, independently-computed comparison
rather than one being the algebraic negation of the other.

WHAT IS REUSED RATHER THAN REIMPLEMENTED
------------------------------------------
Strike selection (condor_scanner.find_condor_legs, short legs only --
hedge legs are irrelevant to a buyer, whose risk is already capped by
premium paid), the day/week walk-forward and expiry-week bounding
(shadow_directional_spread._window_days / _nominal_expiry_date /
_load_cached), and the same coverage-gap accounting shadow_condor.py
uses -- all identical to the condor's own backtest, so any difference
in the two strategies' results comes from the position direction, not
from a different data pipeline.

SAME LIMITATIONS AS shadow_condor.py: fills at LTP (no bid/ask in the
historical data -- for a long strangle this is LESS optimistic than for
the condor, since a buyer pays the ask and receives the bid in reality,
while LTP fills assume neither slippage direction), no early exit
(held to expiry, matching the condor's own documented "no auto-close"
design for a fair comparison), no path-within-a-bar modeling.

NOTHING HERE PLACES A REAL BROKER ORDER -- backtest only.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import config_condor as ccfg
import snapshot_recorder
from condor_scanner import find_condor_legs
from shadow_condor import CondorPolicy, _intrinsic
from shadow_directional_spread import (
    _load_cached, _nominal_expiry_date, _window_days, _parse_hhmm,
)


@dataclass
class ShadowStrangle:
    ce_strike: float
    pe_strike: float
    opened_at: str
    net_debit: float          # premium PAID per unit (ce_premium + pe_premium)
    net_debit_inr: float      # max loss: capped at what was paid, unlike the condor's wing-width loss
    nominal_expiry: str
    closed_at: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl_inr: Optional[float] = None
    peak_pnl_inr: Optional[float] = None
    trough_pnl_inr: Optional[float] = None
    cycles_held: int = 0


def _current_leg_prices(chain: list, ce_strike: float, pe_strike: float) -> dict:
    lookup = {(q.strike, q.option_type): q for q in chain}
    ce = lookup.get((ce_strike, "CE"))
    pe = lookup.get((pe_strike, "PE"))
    return {"ce": ce.ltp if ce else None, "pe": pe.ltp if pe else None}


def _mtm_pnl_inr(strangle: ShadowStrangle, prices: dict) -> Optional[float]:
    if prices["ce"] is None or prices["pe"] is None:
        return None
    lot_size = ccfg.NIFTY_LOT_SIZE
    lots = ccfg.LOTS_PER_LEG
    current_value = prices["ce"] + prices["pe"]
    return (current_value - strangle.net_debit) * lot_size * lots


def _settle_at_expiry(snapshot, strangle: ShadowStrangle, peak: float, trough: float, held: int) -> ShadowStrangle:
    prices = _current_leg_prices(snapshot.chain, strangle.ce_strike, strangle.pe_strike)
    if prices["ce"] is None:
        prices["ce"] = _intrinsic(strangle.ce_strike, "CE", snapshot.spot)
    if prices["pe"] is None:
        prices["pe"] = _intrinsic(strangle.pe_strike, "PE", snapshot.spot)

    mtm = _mtm_pnl_inr(strangle, prices)
    strangle.closed_at = snapshot.timestamp.isoformat()
    strangle.exit_reason = "expiry_settlement"
    strangle.pnl_inr = round(mtm, 2) if mtm is not None else None
    strangle.peak_pnl_inr = round(peak, 2)
    strangle.trough_pnl_inr = round(trough, 2)
    strangle.cycles_held = held
    return strangle


def _walk_strangle_forward(remaining_cycles: list, strangle: ShadowStrangle) -> ShadowStrangle:
    peak = trough = 0.0
    held = 0
    last_snapshot = None

    for snapshot, _candles, _meta in remaining_cycles:
        last_snapshot = snapshot
        prices = _current_leg_prices(snapshot.chain, strangle.ce_strike, strangle.pe_strike)
        mtm = _mtm_pnl_inr(strangle, prices)
        if mtm is not None:
            held += 1
            peak = max(peak, mtm)
            trough = min(trough, mtm)

    if last_snapshot is None:
        return strangle  # unresolved -- recorded history ended first

    return _settle_at_expiry(last_snapshot, strangle, peak, trough, held)


def _scan_day(day: str, policy: CondorPolicy, cycles: list, day_cache: dict,
             all_days: list, open_until, verbose: bool = False) -> tuple:
    start, end = _parse_hhmm(policy.start_time), _parse_hhmm(policy.end_time)
    min_days = policy.resolved_min_days_to_expiry()

    strangles = []
    skips = {"no_candidate": 0}

    for i, (snapshot, _candles, _meta) in enumerate(cycles):
        ts = snapshot.timestamp
        if not (start <= ts.time() <= end):
            continue
        if policy.one_at_a_time and open_until and ts <= open_until:
            continue

        nominal_expiry = _nominal_expiry_date(ts.date())
        if (nominal_expiry - ts.date()).days < min_days:
            continue

        # Same strike-selection call the condor uses -- only the short
        # legs matter here; hedge legs are irrelevant to a buyer.
        legs = find_condor_legs(snapshot.chain)
        if legs["short_ce"] is None or legs["short_pe"] is None:
            skips["no_candidate"] += 1
            continue

        ce, pe = legs["short_ce"], legs["short_pe"]
        net_debit = ce.ltp + pe.ltp
        strangle = ShadowStrangle(
            ce_strike=ce.strike, pe_strike=pe.strike, opened_at=ts.isoformat(),
            net_debit=round(net_debit, 2),
            net_debit_inr=round(net_debit * ccfg.NIFTY_LOT_SIZE * ccfg.LOTS_PER_LEG, 2),
            nominal_expiry=nominal_expiry.isoformat(),
        )

        window = _window_days(all_days, day, nominal_expiry)
        remaining = cycles[i + 1:]
        for later_day in window[1:]:
            remaining = remaining + _load_cached(day_cache, later_day)

        strangle = _walk_strangle_forward(remaining, strangle)
        strangles.append(strangle)

        if strangle.closed_at:
            open_until = datetime.fromisoformat(strangle.closed_at)
        if verbose:
            status = (f"{strangle.exit_reason} Rs {strangle.pnl_inr:+,.0f}"
                      if strangle.pnl_inr is not None else "unresolved (data ended)")
            print(f"  {ts.time()} CE {ce.strike:.0f} / PE {pe.strike:.0f}  "
                  f"debit Rs {strangle.net_debit_inr:.0f} -> expiry {nominal_expiry} -> {status}")

    return strangles, skips, open_until


def run_all(days: list = None, policy: CondorPolicy = None, verbose: bool = False) -> tuple:
    days = days or snapshot_recorder.available_days()
    day_cache = {}
    open_until = None
    all_strangles = []
    all_skips = {"no_candidate": 0}

    for day in days:
        cycles = _load_cached(day_cache, day)
        if not cycles:
            continue
        strangles, skips, open_until = _scan_day(day, policy or CondorPolicy(), cycles,
                                                  day_cache, days, open_until, verbose=verbose)
        all_strangles.extend(strangles)
        all_skips["no_candidate"] += skips["no_candidate"]

    return all_strangles, all_skips


def summarise(strangles: list, label: str = "") -> str:
    usable = [s for s in strangles if s.pnl_inr is not None]
    if not usable:
        return f"{label}: no positions"
    pnls = [s.pnl_inr for s in usable]
    wins = [p for p in pnls if p > 0]
    return (
        f"{label:<28} n={len(usable):>4}  win={100*len(wins)/len(usable):>5.1f}%  "
        f"avg=Rs {statistics.mean(pnls):>+9,.0f}  median=Rs {statistics.median(pnls):>+9,.0f}  "
        f"total=Rs {sum(pnls):>11,.0f}"
    )
