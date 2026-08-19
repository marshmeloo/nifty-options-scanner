"""
Opening Range Breakout (ORB): opening-range computation and per-day
trade simulation on the INDEX.

RESEARCH ONLY as of 2026-08-19 -- nothing here is wired into any live
decision. See orb_study.py for the backtest driver and README for
results.

WHY INDEX-LEVEL FIRST, NOT OPTIONS
-----------------------------------
This project buys option premium, so the eventual question is "does ORB
make money buying CE/PE." But options only ADD cost (spread, theta,
slippage) on top of whatever directional edge exists in the underlying
-- they cannot manufacture an edge that isn't there. So an index-level
edge is a NECESSARY condition, and testing it first is both cheaper and
more informative: if ORB shows nothing on the index, the options
version cannot rescue it, and we've learned that without conflating
signal quality with trade-construction quality. (Same separation
component_study.py makes for the momentum scorer.)

THE VARIATIONS, AND WHERE THEY COME FROM
-----------------------------------------
Drawn from the published ORB literature rather than invented here, so
what's tested is what people actually trade:

  - `or_direction`  -- Zarattini & Aziz (2025), QQQ/TQQQ. NO breakout
    wait at all: at the first bar after the opening range, enter in the
    direction the opening range itself closed (close vs open), stop at
    the OR's opposite extreme. Their headline result (676% on QQQ,
    2016-2023) uses this.
  - `breakout` -- the classic retail/textbook form: wait for price to
    trade beyond the OR high or low, and let whichever side breaks pick
    the direction.
  - `breakout_or_direction` -- Zarattini, Barbon & Aziz (2024), 7,000+
    US stocks: a stop order beyond the OR extreme but ONLY in the
    direction of the first candle, so a break the "wrong" way is not
    taken.
  - `close_confirm` -- breakout that additionally requires a BAR CLOSE
    beyond the level, the standard retail answer to false breakouts.
  - `random` -- coin-flip direction at the same entry time with the
    same stop/target. Not a strategy: a BENCHMARK, because "positive
    average R" means nothing until you know what a random entry with
    the same risk geometry scores on the same days. The ORB literature
    explicitly recommends benchmarking against random entries and
    mostly doesn't do it.

WHAT THE LITERATURE ACTUALLY FOUND (worth stating up front, since it
sets the prior): the 2024 US-stocks paper found UNFILTERED ORB LOST to
buy-and-hold (29% vs 198% for the S&P, Sharpe 0.48 vs 0.78). It only
worked after filtering to the ~20 highest relative-volume "stocks in
play" each day. That filter has no clean analogue for a single index
-- you cannot pick today's NIFTY out of a universe of NIFTYs -- so the
honest prior going in is that index ORB is closer to their unfiltered
(losing) case than their filtered (winning) one.

BAR CONVENTION
--------------
Bars are OPEN-STAMPED 5-minute NIFTY bars (verified empirically, see
orb_candle_cache.py): the bar labelled 09:15 covers 09:15->09:20. A
15-minute opening range is therefore bars 09:15, 09:20, 09:25, and the
first tradeable bar is 09:30.

INTRABAR AMBIGUITY, RESOLVED CONSERVATIVELY
--------------------------------------------
OHLC cannot say whether the high or the low came first within a bar.
Two places that matters, both resolved AGAINST the strategy so results
are not flattered by a favourable guess:
  - Stop and target both reachable in one bar -> assume the STOP hit
    first.
  - Both sides of the range breached in one entry bar -> assume the
    side OPPOSITE the bar's own close direction triggered first (price
    went the wrong way, then reversed).
`ambiguous_bars` is reported per simulation so the reader can see how
often the assumption was load-bearing rather than take it on trust.
"""

from dataclasses import dataclass
from typing import Optional
import random as _random

SESSION_OPEN = "09:15"
SESSION_LAST_BAR = "15:25"   # open-stamped: the 15:25 bar covers 15:25->15:30


@dataclass
class ORBVariant:
    """One testable ORB configuration."""
    name: str
    or_minutes: int = 15
    # breakout | or_direction | breakout_or_direction | close_confirm
    # | random | always_long | always_short
    entry: str = "breakout"
    stop: str = "or_opposite"        # or_opposite | or_mid
    target_r: Optional[float] = None  # None -> hold to EOD
    allow_long: bool = True
    allow_short: bool = True
    entry_cutoff: str = "15:00"      # no NEW entries at/after this bar
    buffer_pct: float = 0.0          # breakout must exceed the level by this % of price
    min_or_width_pct: Optional[float] = None   # skip the day if OR narrower than this % of price
    max_or_width_pct: Optional[float] = None   # skip the day if OR wider than this % of price
    # Floor on |entry - stop| as a % of price. The stop is WIDENED to
    # this distance when the opening-range level sits closer than it;
    # the trade is never skipped for being too tight.
    #
    # Not cosmetic -- without it the comparison is invalid. Measured on
    # the first run: the coin-flip benchmark's stop distance had a p10
    # of 5.0 NIFTY points and a minimum of 0.1, because
    # entry-at-next-bar-open can land arbitrarily close to the opening
    # range's edge. R-multiple divides by that distance, so those
    # degenerate trades manufacture enormous R-multiples out of ordinary
    # point moves -- enough to lift the coin flip above every real
    # variant. A sub-5-point stop on NIFTY is not executable: spread and
    # slippage alone exceed it. Same objection levelled at the published
    # ORB papers' "no slippage, ~$0.08 stop" results.
    #
    # WIDENING rather than SKIPPING, deliberately. Skipping was tried
    # first and is subtly wrong: which trades fall below the floor
    # depends on where price sits inside the range at entry, which is
    # direction-dependent (a short's stop is the range HIGH, so a tight
    # short stop means price is near the range top). Skipping therefore
    # selects systematically different days for longs than for shorts
    # and silently turns "always short" into "short only when price
    # already fell to the bottom of the range" -- a momentum strategy
    # wearing a benchmark's name. It also showed up directly as
    # participation dropping to ~57% for the benchmarks while real
    # variants stayed near 99%, making the two non-comparable. Widening
    # keeps every day, matches what a real trader would actually do with
    # an unusably tight stop, and is applied identically to every
    # variant.
    min_risk_pct: Optional[float] = None
    seed: int = 0                    # only used by entry="random"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def opening_range(bars: list, or_minutes: int) -> Optional[dict]:
    """
    {high, low, open, close, width, bars} for the first `or_minutes` of
    the session, or None if the day's bars don't cover it.

    Bars are open-stamped, so the range covers [09:15, 09:15+or_minutes)
    -- a 15-minute range is the bars stamped 09:15, 09:20, 09:25.
    """
    start = _minutes(SESSION_OPEN)
    end = start + or_minutes
    in_range = [b for b in bars if start <= _minutes(b["t"]) < end]
    if not in_range:
        return None
    expected = or_minutes // 5
    if len(in_range) < expected:
        return None   # incomplete opening range (short session, data gap) -- don't guess
    return {
        "high": max(b["h"] for b in in_range),
        "low": min(b["l"] for b in in_range),
        "open": in_range[0]["o"],
        "close": in_range[-1]["c"],
        "width": max(b["h"] for b in in_range) - min(b["l"] for b in in_range),
        "volume": sum(b.get("v") or 0 for b in in_range),
        "bars": len(in_range),
        "end_hhmm": f"{end // 60:02d}:{end % 60:02d}",
    }


def _stop_level(orange: dict, direction: str, variant: ORBVariant) -> float:
    if variant.stop == "or_mid":
        mid = (orange["high"] + orange["low"]) / 2
        return mid
    return orange["low"] if direction == "long" else orange["high"]


def simulate_day(bars: list, variant: ORBVariant, day: str = "") -> Optional[dict]:
    """
    Run one variant over one day's bars. Returns a trade dict, or None
    if no trade was taken (no signal, filtered out, or incomplete data).

    R is defined as |entry - stop| in index points, so r_multiple is
    comparable across days and price levels regardless of NIFTY's
    absolute level drifting from 11,000 to 26,000 over the sample.
    """
    orange = opening_range(bars, variant.or_minutes)
    if not orange or orange["width"] <= 0:
        return None

    ref_price = orange["close"] or orange["open"]
    width_pct = orange["width"] / ref_price * 100 if ref_price else 0
    if variant.min_or_width_pct is not None and width_pct < variant.min_or_width_pct:
        return None
    if variant.max_or_width_pct is not None and width_pct > variant.max_or_width_pct:
        return None

    or_end = _minutes(orange["end_hhmm"])
    cutoff = _minutes(variant.entry_cutoff)
    after = [b for b in bars if _minutes(b["t"]) >= or_end]
    if not after:
        return None

    or_dir = "long" if orange["close"] > orange["open"] else ("short" if orange["close"] < orange["open"] else None)

    entry_bar_idx = None
    direction = None
    entry_price = None
    ambiguous = 0

    if variant.entry in ("or_direction", "random", "always_long", "always_short"):
        if variant.entry == "random":
            rng = _random.Random(f"{variant.seed}:{day}")
            direction = rng.choice(["long", "short"])
        elif variant.entry == "always_long":
            direction = "long"
        elif variant.entry == "always_short":
            direction = "short"
        else:
            direction = or_dir
        if direction is not None:
            entry_bar_idx = 0
            entry_price = after[0]["o"]
    else:
        buf = ref_price * variant.buffer_pct / 100
        up_level = orange["high"] + buf
        down_level = orange["low"] - buf
        for i, b in enumerate(after):
            if _minutes(b["t"]) >= cutoff:
                break
            if variant.entry == "close_confirm":
                broke_up = b["c"] > up_level
                broke_down = b["c"] < down_level
                fill_up, fill_down = b["c"], b["c"]
            else:
                broke_up = b["h"] > up_level
                broke_down = b["l"] < down_level
                # A stop order at the level fills at the level, not at
                # the bar's extreme -- filling at the extreme would be
                # inventing a price the order never had.
                fill_up, fill_down = up_level, down_level

            if variant.entry == "breakout_or_direction":
                if or_dir == "long":
                    broke_down = False
                elif or_dir == "short":
                    broke_up = False
                else:
                    broke_up = broke_down = False

            if broke_up and broke_down:
                ambiguous += 1
                # Conservative: assume the side against the bar's own
                # close direction triggered first.
                if b["c"] >= b["o"]:
                    direction, entry_price = "short", fill_down
                else:
                    direction, entry_price = "long", fill_up
                entry_bar_idx = i
                break
            if broke_up:
                direction, entry_price, entry_bar_idx = "long", fill_up, i
                break
            if broke_down:
                direction, entry_price, entry_bar_idx = "short", fill_down, i
                break

    if direction is None or entry_bar_idx is None:
        return None
    if direction == "long" and not variant.allow_long:
        return None
    if direction == "short" and not variant.allow_short:
        return None

    stop = _stop_level(orange, direction, variant)
    risk = abs(entry_price - stop)
    stop_widened = False
    if variant.min_risk_pct is not None:
        min_risk = ref_price * variant.min_risk_pct / 100
        if risk < min_risk:
            # Push the stop out to the floor -- see ORBVariant.min_risk_pct
            # for why this widens rather than skipping.
            stop = entry_price - min_risk if direction == "long" else entry_price + min_risk
            risk = min_risk
            stop_widened = True
    if risk <= 0:
        return None

    target = None
    if variant.target_r is not None:
        target = (entry_price + risk * variant.target_r if direction == "long"
                  else entry_price - risk * variant.target_r)

    # Walk forward from the entry bar. The entry bar itself counts: a
    # stop can be hit in the same bar the position opened.
    exit_price, exit_reason, exit_time = None, None, None
    for b in after[entry_bar_idx:]:
        hit_stop = b["l"] <= stop if direction == "long" else b["h"] >= stop
        hit_target = False
        if target is not None:
            hit_target = b["h"] >= target if direction == "long" else b["l"] <= target

        if hit_stop and hit_target:
            ambiguous += 1
            exit_price, exit_reason, exit_time = stop, "stop", b["t"]   # conservative
            break
        if hit_stop:
            exit_price, exit_reason, exit_time = stop, "stop", b["t"]
            break
        if hit_target:
            exit_price, exit_reason, exit_time = target, "target", b["t"]
            break

    if exit_price is None:
        exit_price, exit_reason, exit_time = after[-1]["c"], "eod", after[-1]["t"]

    move = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
    return {
        "day": day,
        "direction": direction,
        "entry_time": after[entry_bar_idx]["t"],
        "entry": round(entry_price, 2),
        "stop": round(stop, 2),
        "target": round(target, 2) if target is not None else None,
        "exit": round(exit_price, 2),
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "risk_points": round(risk, 2),
        "points": round(move, 2),
        "r_multiple": round(move / risk, 4),
        "or_width": round(orange["width"], 2),
        "or_width_pct": round(width_pct, 4),
        "ambiguous_bars": ambiguous,
        "stop_widened": stop_widened,
    }
