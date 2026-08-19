"""
Intraday entry/exit variants for stocks selected as "in play".
RESEARCH ONLY -- nothing here trades.

SCOPE: this module answers "given that a stock was selected this
morning, what is the best way to trade it?" -- NOT "which stocks?".
Selection lives in stocks_in_play.py. Keeping them apart matters: a
good selection with a bad exit and a useless selection with a lucky
exit look identical if measured together, which is the mistake the
index ORB study was specifically built to avoid.

THE VARIANTS, AND WHERE EACH COMES FROM
----------------------------------------
  - `orb`             -- breakout of the opening range, the setup the
                         whole track started from.
  - `momentum`        -- enter at the selection point in the direction
                         the stock has already moved. No breakout wait;
                         the equivalent of Zarattini's `or_direction`.
  - `pullback`        -- wait for a retrace toward the opening range
                         edge and enter on continuation. The standard
                         retail answer to "don't chase the spike".
  - `always`          -- enter every selected name, direction from the
                         opening move. A CONTROL, not a strategy: it
                         isolates how much of any result comes from
                         SELECTION rather than from the entry rule.
  - `random`          -- coin-flip direction, same entry bar, same stop.
                         The second control. Together with `always` it
                         separates three things that are otherwise
                         conflated: the selection, the direction call,
                         and the payoff geometry of a stop-plus-exit.

EXIT FAMILIES
-------------
  - `eod`             -- hold to the close.
  - `fixed_r`         -- single target at N x risk.
  - `runner`          -- scale out at T1 and T2, let the remainder run
                         to the close. This is the structure the
                         reference screenshots showed (T1 / T2 /
                         "T3 = 2R close"), and it is the exit that
                         actually harvests a fat right tail -- which is
                         the hypothesis under test. A fixed target
                         CANNOT express "win large", so testing only
                         fixed targets would answer the wrong question.

INTRABAR AMBIGUITY is resolved exactly as orb.py does: OHLC cannot say
whether the high or low came first, so when a bar could have hit both
stop and target, the STOP is assumed. Results are therefore
conservative rather than flattered.
"""

from dataclasses import dataclass
from typing import Optional
import random as _random

SESSION_OPEN = "09:15"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


SESSION_OPEN_MIN = _minutes(SESSION_OPEN)


@dataclass
class StockVariant:
    name: str
    entry: str = "momentum"       # orb | momentum | pullback | always | random
    exit: str = "runner"          # eod | fixed_r | runner
    selection_minutes: int = 15   # must match the selection window used upstream
    stop_atr_mult: float = 1.0    # stop = this x the opening-range height
    target_r: float = 2.0         # for exit="fixed_r"
    t1_r: float = 1.0             # runner: first scale-out
    t2_r: float = 2.0             # runner: second scale-out
    runner_frac: float = 1 / 3    # fraction left to run after T1 and T2
    entry_cutoff: str = "14:30"   # no new entries at/after this bar
    allow_long: bool = True
    allow_short: bool = True
    seed: int = 0


def opening_range(bars: list, minutes: int) -> Optional[dict]:
    end = SESSION_OPEN_MIN + minutes
    head = [b for b in bars if SESSION_OPEN_MIN <= _minutes(b[0]) < end]
    if len(head) < minutes // 5:
        return None
    return {
        "high": max(b[2] for b in head),
        "low": min(b[3] for b in head),
        "open": head[0][1],
        "close": head[-1][4],
        "end_min": end,
    }


def simulate(bars: list, variant: StockVariant, day: str = "", symbol: str = "") -> Optional[dict]:
    """
    One stock, one day. Returns a trade dict or None if no entry.

    Returns GROSS rupee-per-share figures plus turnover, so costs and
    slippage are applied by the caller (see stock_costs) rather than
    baked in at a single assumed level -- the break-even slippage is the
    headline result and cannot be computed if costs are pre-applied.
    """
    orange = opening_range(bars, variant.selection_minutes)
    if not orange:
        return None
    height = orange["high"] - orange["low"]
    if height <= 0:
        return None

    after = [b for b in bars if _minutes(b[0]) >= orange["end_min"]]
    if len(after) < 2:
        return None
    cutoff = _minutes(variant.entry_cutoff)

    or_dir = ("long" if orange["close"] > orange["open"]
              else "short" if orange["close"] < orange["open"] else None)

    direction, entry_px, entry_i = None, None, None

    if variant.entry in ("momentum", "always"):
        direction = or_dir
        if direction:
            direction, entry_px, entry_i = direction, after[0][1], 0
    elif variant.entry == "random":
        direction = _random.Random(f"{variant.seed}:{symbol}:{day}").choice(["long", "short"])
        entry_px, entry_i = after[0][1], 0
    elif variant.entry == "orb":
        for i, b in enumerate(after):
            if _minutes(b[0]) >= cutoff:
                break
            if b[2] > orange["high"]:
                direction, entry_px, entry_i = "long", orange["high"], i
                break
            if b[3] < orange["low"]:
                direction, entry_px, entry_i = "short", orange["low"], i
                break
    elif variant.entry == "pullback":
        # Wait for price to come back INTO the range, then take the
        # first push back out in the opening move's direction.
        if or_dir is None:
            return None
        came_back = False
        for i, b in enumerate(after):
            if _minutes(b[0]) >= cutoff:
                break
            if not came_back:
                if (or_dir == "long" and b[3] <= orange["high"]) or \
                   (or_dir == "short" and b[2] >= orange["low"]):
                    came_back = True
                continue
            if or_dir == "long" and b[2] > orange["high"]:
                direction, entry_px, entry_i = "long", orange["high"], i
                break
            if or_dir == "short" and b[3] < orange["low"]:
                direction, entry_px, entry_i = "short", orange["low"], i
                break

    if not direction or entry_px is None:
        return None
    if direction == "long" and not variant.allow_long:
        return None
    if direction == "short" and not variant.allow_short:
        return None

    risk = height * variant.stop_atr_mult
    stop = entry_px - risk if direction == "long" else entry_px + risk
    sign = 1 if direction == "long" else -1

    def r_price(r):
        return entry_px + sign * risk * r

    # --- walk forward ---------------------------------------------------
    legs = []          # (fraction_of_position, exit_price, reason)
    remaining = 1.0
    t1_done = t2_done = False
    t1, t2 = r_price(variant.t1_r), r_price(variant.t2_r)
    target = r_price(variant.target_r)
    ambiguous = 0

    for b in after[entry_i:]:
        hi, lo = b[2], b[3]
        hit_stop = lo <= stop if direction == "long" else hi >= stop

        if variant.exit == "eod":
            if hit_stop:
                legs.append((remaining, stop, "stop")); remaining = 0; break
            continue

        if variant.exit == "fixed_r":
            hit_t = hi >= target if direction == "long" else lo <= target
            if hit_stop and hit_t:
                ambiguous += 1
                legs.append((remaining, stop, "stop")); remaining = 0; break
            if hit_stop:
                legs.append((remaining, stop, "stop")); remaining = 0; break
            if hit_t:
                legs.append((remaining, target, "target")); remaining = 0; break
            continue

        # runner: scale at T1, T2, remainder to close
        hit_t1 = (hi >= t1 if direction == "long" else lo <= t1) and not t1_done
        hit_t2 = (hi >= t2 if direction == "long" else lo <= t2) and not t2_done
        if hit_stop and (hit_t1 or hit_t2):
            ambiguous += 1
            legs.append((remaining, stop, "stop")); remaining = 0; break
        if hit_stop:
            legs.append((remaining, stop, "stop")); remaining = 0; break
        if hit_t1:
            part = (1 - variant.runner_frac) / 2
            legs.append((part, t1, "t1")); remaining -= part; t1_done = True
            # Standard practice and the honest conservative choice: once
            # T1 pays, the stop moves to entry so the rest cannot become
            # a loser. Without this the runner is strictly worse than a
            # fixed target and the comparison is unfair to it.
            stop = entry_px
        if hit_t2:
            part = (1 - variant.runner_frac) / 2
            legs.append((part, t2, "t2")); remaining -= part; t2_done = True

    if remaining > 0:
        legs.append((remaining, after[-1][4], "eod"))

    gross_per_share = sum(frac * (px - entry_px) * sign for frac, px, _ in legs)
    turnover_per_share = entry_px + sum(frac * px for frac, px, _ in legs)

    return {
        "symbol": symbol, "day": day, "direction": direction,
        "entry": round(entry_px, 2), "stop": round(stop, 2),
        "risk_per_share": round(risk, 4),
        "gross_per_share": round(gross_per_share, 4),
        "turnover_per_share": round(turnover_per_share, 4),
        "r_multiple": round(gross_per_share / risk, 4) if risk else 0.0,
        # Position fractions are NOT rounded. They must sum to exactly
        # 1.0 so a consumer can verify the whole position is accounted
        # for; rounding a 1/3 scale-out to any number of decimals makes
        # three legs sum to 0.999... and creates a phantom unaccounted
        # sliver. Only PRICES are rounded, where it is cosmetic.
        "exits": [(f, round(p, 2), why) for f, p, why in legs],
        "outcome": legs[0][2] if legs else None,
        "ambiguous_bars": ambiguous,
    }
