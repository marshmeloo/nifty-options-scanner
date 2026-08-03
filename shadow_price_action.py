"""
Backtest driver for the pure price-action strategy (price_structure.py +
price_action_scanner.py + price_action_plan_generator.py) against the
reconstructed historical option-chain dataset.

Evaluates BOTH timeframe pairs from config_price_action.TIMEFRAME_PAIRS
side by side -- "daily_hourly" (HTF=daily zones, LTF=1H trigger, the
reference doc's own pairing, implying multi-day holds) and "intraday"
(HTF=15min, LTF=5min, same-day holds) -- rather than assuming either is
right. Results are kept in separate ledgers per pair; a strategy that
looks good on one timeframe pair says nothing about the other.

WHAT IS REUSED RATHER THAN REIMPLEMENTED
------------------------------------------
Signal generation, candidate selection and plan sizing come straight
from the live modules (price_structure.generate_signal,
price_action_scanner.scan, price_action_plan_generator.build_plan) --
the same discipline as shadow.py/shadow_condor.py/
shadow_directional_spread.py. The expiry-week bounding
(_nominal_expiry_date / _window_days / _load_cached) is imported from
shadow_directional_spread.py rather than duplicated: this strategy holds
a single option across possibly many days, exactly the same rolling-
series identity hazard the credit-spread backtest already solved.

THE CONSTRAINT THAT MATTERS MOST HERE: STRIKE IDENTITY ACROSS DAYS
---------------------------------------------------------------------
historical_source.py reconstructs each day's chain from ATM-RELATIVE
strike offsets (see its own docstring), so a fixed absolute strike
opened on day N is only found on day N+1 if that day's own ATM+/-10
window happens to still cover it -- normal for small spot moves, but a
large gap can silently drop the position's own quote out of the
reconstructed data before its stop or target is actually reached. When
that happens the walk-forward has no quote to act on and the position
is settled at the LAST price actually seen (or intrinsic value, if even
that never appeared) rather than assumed flat. Same "missing quote is
missing information, not a zero" principle as directional_spread and
condor's own trackers.

A held option is also, definitionally, capped at ITS OWN contract's
nominal weekly expiry (_nominal_expiry_date) -- holding a specific
option past its own expiry has no meaning, so "daily_hourly"'s
multi-day hold is bounded there, not indefinitely.

WHAT IS NOT SIMULATED
----------------------
  - Bid/ask fills: the reconstructed data carries no book (see
    historical_source.py), so every fill is at LTP -- optimistic versus
    live, same bias already documented for every other historical
    backtest in this project. Never pool these numbers with a live
    result.
  - Intra-bar path: target/stop are checked only at recorded 5-min
    cycles, so a spike that touched a level and reversed between two
    cycles is invisible.

Treat these results the same way every other shadow backtest in this
project is treated: a strong filter for REJECTING a timeframe pair, and
suggestive-not-conclusive for adopting one.
"""

import argparse
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import config_price_action as pcfg
import costs
import price_action_plan_generator as pag
import price_action_scanner as pas
import price_structure as ps
import snapshot_recorder
from models import Candle
from shadow_directional_spread import _load_cached, _nominal_expiry_date, _window_days


@dataclass
class ShadowPATrade:
    pair: str                 # "daily_hourly" | "intraday"
    strike: float
    option_type: str
    opened_at: str
    entry: float
    stop: float
    target: float
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None       # WIN / LOSS / EXPIRY_CLOSE
    peak_price: Optional[float] = None
    trough_price: Optional[float] = None
    gross_inr: Optional[float] = None
    cost_inr: Optional[float] = None
    net_inr: Optional[float] = None
    net_r: Optional[float] = None
    reasons: list = field(default_factory=list)


def _daily_bar(day_candles: list) -> Candle:
    """One completed day's intraday candles, aggregated into a single
    daily OHLC bar -- the HTF unit the "daily_hourly" pair reacts from."""
    return Candle(
        timestamp=day_candles[0].timestamp,
        open=day_candles[0].open,
        high=max(c.high for c in day_candles),
        low=min(c.low for c in day_candles),
        close=day_candles[-1].close,
        volume=sum(c.volume for c in day_candles),
    )


def _find_quote(chain, strike, option_type):
    for q in chain:
        if q.strike == strike and q.option_type == option_type:
            return q
    return None


def _intrinsic(option_type: str, strike: float, spot: float) -> float:
    if option_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _finalise(trade: ShadowPATrade, ts, exit_px, outcome, peak, trough, lots) -> ShadowPATrade:
    lot_size = pcfg.NIFTY_LOT_SIZE
    risk_unit = trade.entry - trade.stop

    trade.closed_at = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    trade.exit_price = round(exit_px, 2)
    trade.outcome = outcome
    trade.peak_price = round(peak, 2)
    trade.trough_price = round(trough, 2)

    gross_inr = (exit_px - trade.entry) * lot_size * lots
    cost_inr = costs.round_trip(trade.entry, exit_px, lots, lot_size)["total_inr"]
    trade.gross_inr = round(gross_inr, 2)
    trade.cost_inr = round(cost_inr, 2)
    trade.net_inr = round(gross_inr - cost_inr, 2)

    if risk_unit > 0:
        risk_inr = risk_unit * lot_size * lots
        trade.net_r = round((gross_inr - cost_inr) / risk_inr, 3)
    return trade


def _walk_forward(day_cache: dict, window_days: list, entry_day: str, entry_idx: int,
                  strike: float, option_type: str, trade: ShadowPATrade, lots: int) -> ShadowPATrade:
    """
    Advance the position through every later recorded cycle inside its
    own expiry window until target or stop is hit at a recorded LTP, or
    the window is exhausted (settle at the last price actually seen, or
    intrinsic value if this strike never appears again).
    """
    peak = trough = trade.entry
    last_snapshot = None

    for day in window_days:
        cycles = _load_cached(day_cache, day)
        start = entry_idx + 1 if day == entry_day else 0
        for snapshot, _candles, _meta in cycles[start:]:
            last_snapshot = snapshot
            q = _find_quote(snapshot.chain, strike, option_type)
            if q is None:
                continue
            px = q.ltp
            peak, trough = max(peak, px), min(trough, px)

            outcome = None
            if px >= trade.target:
                outcome = "WIN"
            elif px <= trade.stop:
                outcome = "LOSS"
            if outcome:
                return _finalise(trade, snapshot.timestamp, px, outcome, peak, trough, lots)

    if last_snapshot is None:
        return trade  # no recorded data at all inside this position's window

    q = _find_quote(last_snapshot.chain, strike, option_type)
    px = q.ltp if q is not None else _intrinsic(option_type, strike, last_snapshot.spot)
    return _finalise(trade, last_snapshot.timestamp, px, "EXPIRY_CLOSE",
                     max(peak, px), min(trough, px), lots)


def run_pair(pair_name: str, days: list, target_rr: float = None,
            one_lot: bool = True, verbose: bool = False) -> list:
    """
    Replay every recorded day under one timeframe pair, opening at most
    one position at a time (a new signal is ignored while a position
    from an earlier signal is still open -- mirrors every other
    strategy's one_at_a_time default in this project, and is the
    natural fit for a single-leg swing hold).

    target_rr: when given, overrides config_price_action.TARGET_RR --
    the target is RE-DERIVED from the plan's own entry/stop (entry +
    risk * target_rr), same as shadow.py's run_policy does for the
    momentum scanner, so an R:R sweep needs no config edits between runs.

    one_lot (default True, per this project's standing sizing
    convention for a first-lot test): every trade is sized at exactly 1
    lot regardless of build_plan's own risk-budget arithmetic -- the
    question being asked here is "how does this R:R perform," not
    "would the risk budget have allowed it."
    """
    tf = pcfg.TIMEFRAME_PAIRS[pair_name]
    day_cache = {}
    daily_bars = []       # completed prior days -- HTF for "daily_hourly"
    # Completed hourly bars from PRIOR days -- LTF for "daily_hourly".
    #
    # A single day only contains ~6 hourly bars (a ~375-minute session),
    # far short of generate_signal's SWING_LOOKBACK*2+2 (12) minimum, so
    # resampling only the CURRENT day's candles meant this pair could
    # never produce a single signal, ever -- silently dead across the
    # whole 497-day history, the same "looks like a verdict on the
    # strategy but is really a missing input" failure this project has
    # hit before (see historical_source.py's OI-buildup docstring). The
    # 1-hour series for this pair is genuinely multi-day, so it must
    # accumulate across days exactly like daily_bars does.
    hourly_bars_prior_days = []
    trades = []
    open_until = None   # (day, timestamp) cursor

    for day in days:
        cycles = _load_cached(day_cache, day)
        if not cycles:
            continue

        for idx, (snapshot, candles, _meta) in enumerate(cycles):
            ts = snapshot.timestamp
            if open_until and (day, ts) <= open_until:
                continue
            if not candles:
                continue

            if pair_name == "daily_hourly":
                htf = daily_bars
                if len(htf) < pcfg.SWING_LOOKBACK * 2 + 2:
                    continue
                ltf = hourly_bars_prior_days + ps.resample_candles(
                    candles, tf["ltf_interval"], source_minutes=5)
                if len(ltf) < pcfg.SWING_LOOKBACK * 2 + 2:
                    continue
            else:
                ltf = candles
                htf = ps.resample_candles(candles, tf["htf_interval"], source_minutes=5)

            try:
                signal = ps.generate_signal(htf, ltf)
            except Exception:
                signal = None
            if signal is None:
                continue

            setups = pas.scan(snapshot, signal)
            if not setups:
                continue
            setup = setups[0]
            try:
                plan = pag.build_plan(snapshot, setup, signal)
            except ValueError:
                continue

            risk_unit = plan.entry - plan.stop
            if risk_unit <= 0:
                continue
            target = (round(plan.entry + risk_unit * target_rr, 2)
                     if target_rr is not None else plan.target)
            lots = 1 if one_lot else plan.lots
            if lots <= 0:
                continue

            expiry_date = _nominal_expiry_date(date.fromisoformat(day))
            window = _window_days(days, day, expiry_date)

            trade = ShadowPATrade(
                pair=pair_name, strike=setup.strike, option_type=setup.option_type,
                opened_at=ts.isoformat(), entry=plan.entry, stop=plan.stop, target=target,
                reasons=list(setup.reasons)[:4],
            )
            trade = _walk_forward(day_cache, window, day, idx, setup.strike, setup.option_type,
                                  trade, lots)
            trades.append(trade)
            if trade.closed_at:
                open_until = (day, datetime.fromisoformat(trade.closed_at))
                if verbose:
                    print(f"  [{pair_name}] {ts} {setup.strike:.0f}{setup.option_type} "
                          f"-> {trade.outcome} {trade.net_r if trade.net_r is not None else 'n/a'}R")

        if pair_name == "daily_hourly":
            all_candles_today = cycles[-1][1]
            if all_candles_today:
                daily_bars.append(_daily_bar(all_candles_today))
                hourly_bars_prior_days.extend(
                    ps.resample_candles(all_candles_today, tf["ltf_interval"], source_minutes=5)
                )

    return trades


def summarise(trades: list, label: str = "") -> str:
    usable = [t for t in trades if t.net_r is not None]
    if not usable:
        return f"{label}: no trades"

    nets = [t.net_r for t in usable]
    wins = [t for t in usable if t.net_r > 0]
    total_inr = sum(t.net_inr for t in usable)
    unresolved = len(trades) - len(usable)
    return (
        f"{label:<28} n={len(usable):>4}  win={100*len(wins)/len(usable):>5.1f}%  "
        f"expectancy={statistics.mean(nets):+.3f}R  median={statistics.median(nets):+.3f}R  "
        f"total=Rs {total_inr:>9,.0f}" + (f"  ({unresolved} unresolved)" if unresolved else "")
    )


def sweep_target_rr(rr_values: list, pairs: list = None, days: list = None,
                    one_lot: bool = True, verbose: bool = False) -> dict:
    """
    Run every (pair, R:R) combination over the same recorded days and
    return {(pair, rr): trades}. Each combination is a FULL independent
    replay -- a different R:R changes which cycle first resolves a
    trade (target reached sooner/later), which in turn changes
    `open_until` and therefore every later entry, so results cannot be
    derived from one run's trades by rescaling -- they must be re-walked.
    """
    pairs = pairs or list(pcfg.TIMEFRAME_PAIRS)
    days = days or sorted(snapshot_recorder.available_days())
    results = {}
    for pair_name in pairs:
        for rr in rr_values:
            results[(pair_name, rr)] = run_pair(
                pair_name, days, target_rr=rr, one_lot=one_lot, verbose=verbose)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pair", choices=list(pcfg.TIMEFRAME_PAIRS) + ["both"], default="both")
    p.add_argument("--rr", type=float, default=None,
                   help="single target R:R override (default: config_price_action.TARGET_RR)")
    p.add_argument("--rr-sweep", type=str, default=None,
                   help="comma-separated list of target R:R values to test, e.g. 1.0,1.5,2.0,2.5,3.0")
    p.add_argument("--all-lots", action="store_true",
                   help="size by the plan's own risk budget instead of forcing 1 lot")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    days = sorted(snapshot_recorder.available_days())
    if not days:
        print("No recorded/backfilled snapshots yet.")
        return

    pairs = list(pcfg.TIMEFRAME_PAIRS) if args.pair == "both" else [args.pair]
    one_lot = not args.all_lots

    if args.rr_sweep:
        rr_values = [float(x) for x in args.rr_sweep.split(",")]
        results = sweep_target_rr(rr_values, pairs=pairs, days=days, one_lot=one_lot, verbose=args.verbose)
        print(f"R:R sweep over {len(days)} days, {'1 lot' if one_lot else 'risk-budgeted lots'}:\n")
        for pair_name in pairs:
            for rr in rr_values:
                print(summarise(results[(pair_name, rr)], f"{pair_name} @ 1:{rr:g}"))
            print()
        return

    for pair_name in pairs:
        trades = run_pair(pair_name, days, target_rr=args.rr, one_lot=one_lot, verbose=args.verbose)
        print(summarise(trades, pair_name))


if __name__ == "__main__":
    main()
