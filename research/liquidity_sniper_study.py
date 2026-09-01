"""
"Liquidity Sniper Entry Model" tested on Bank Nifty's own history.

THE STRATEGY, as specified in the source PDF ("Liquidity Sniper Setup
Guide", 2026-07-25):

  1. 4H bias      -- discount zone => BUY setups only, premium => SELL only
  2. 15-min sweep -- BUY: a recent swing LOW is swept (wick through, close
                     back above). SELL: a recent swing HIGH is swept.
  3. BOS          -- after the sweep, a Break of Structure in the OPPOSITE
                     direction: BUY = close above a swing high.
  4. FVG          -- the fair-value gap left by the BOS impulse; entry must
                     be inside it.
  5. Fibonacci    -- Point 1 = sweep extreme, Point 2 = BOS extreme. LIMIT
                     entry at the 71% retracement.
  6. SL / target  -- SL just beyond 100% (the sweep extreme), target 0%
                     (the BOS extreme). Claimed RR ~2.4:1.
  All three of sweep + BOS + FVG required; never trade against 4H bias.

WHY IT IS TESTED ON THE INDEX, NOT ON OPTIONS. The model is a directional
price-action system whose stop and target are INDEX levels. Run it on the
index and the question is clean: does the signal have an edge in points?
If it does not, wrapping it in options cannot rescue it -- that would only
add premium decay, spread and strike selection on top of an unproven
signal. Options come second, if at all.

STRUCTURE IS READ FROM A CONTINUOUS 15-MIN SERIES, NOT A FRESH CHART EACH
MORNING. A 6h15m NSE session yields only 25 15-min bars, and a 5-bar
swing lookback each side leaves almost no room for a swing to form, be
swept, and then produce a BOS. Measured directly, per-day charts produced
SEVEN sweeps in 113 days and zero completed setups -- the model could
never fire. A trader watching a 15-min Bank Nifty chart sees one
continuous chart, and swings span sessions. Positions still do not carry
overnight (the PDF labels it "15-MIN DAYTRADING"), so anything open at a
day boundary is closed at that day's last price.

WHAT THE PDF DOES NOT SPECIFY -- each is a judgement call, and results
move with them, so they are parameters rather than constants:

  * "discount / premium zone" is never defined. Standard ICT reading is a
    dealing range: over the last N 4H bars take the highest high and the
    lowest low, split at the 50% midpoint. DEALING_RANGE_BARS.
  * A 4H bar does not exist in a 6h15m session, so 4H bars are built from
    CONTINUOUS SESSION TIME across days (48 x 5-min), ignoring overnight
    gaps.
  * "recent swing" has no lookback. config.SWING_LOOKBACK (default 5).
  * How long a BOS may take after the sweep. BOS_MAX_BARS.
  * How long the 71% limit order rests. LIMIT_MAX_BARS.
  * "SL a little beyond 100%" is unquantified. SL_BUFFER_PCT.

PROVENANCE OF THE CLAIM BEING TESTED: the source is a marketing graphic
(156 trades, profit factor 2.32, Sharpe 2.71, Calmar 7.28, "+99.9%") with
no instrument, exchange or trade log named. This script replaces that
claim with a measurement on data we control.

    python -m research.liquidity_sniper_study
"""

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import config
import price_action
import snapshot_recorder

BN_SNAPSHOT_DIR = Path(__file__).parent.parent / "logs" / "snapshots_banknifty"

BARS_PER_15MIN = 3          # source candles are 5-minute
BARS_PER_4H = 48            # 48 x 5min = 240min of session time
DEALING_RANGE_BARS = 20     # 4H bars used for the discount/premium split
BOS_MAX_BARS = 12           # 15-min bars a BOS may take after a sweep
LIMIT_MAX_BARS = 12         # 15-min bars the 71% limit order rests
SL_BUFFER_PCT = 0.05        # % beyond the sweep extreme
FIB_ENTRY = 0.71
# How far back "recent structure" reaches, in 15-min bars (~25/session, so
# 100 is about four sessions). THIS MATTERS: price_action's own
# detect_liquidity_sweeps/detect_fair_value_gaps are DAY-SCOPED -- they take
# max() over EVERY prior swing and drop any FVG ever filled, which on a
# single ~75-bar day means "today's high" and "still-fresh gap". Applied to
# a multi-year continuous series those become the ALL-TIME high and
# almost-nothing: measured, 4 sweeps and 23 unmitigated FVGs across 4,854
# bars, so no setup could ever complete. The detectors below are rolling
# equivalents -- a trader reads recent swings, not the multi-year extreme.
STRUCTURE_WINDOW = 100


@dataclass
class Candle:
    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Trade:
    day: str
    direction: str
    entry_ts: object
    entry: float
    stop: float
    target: float
    exit_ts: object = None
    exit: float = None
    outcome: str = None
    points: float = 0.0
    r_multiple: float = 0.0
    bias: str = ""


def resample(candles, n):
    out = []
    for i in range(0, len(candles), n):
        chunk = candles[i:i + n]
        if not chunk:
            continue
        out.append(Candle(
            timestamp=chunk[0].timestamp,
            open=chunk[0].open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=chunk[-1].close,
            volume=sum(getattr(c, "volume", 0) or 0 for c in chunk),
        ))
    return out


def day_candles(day, snapshot_dir, symbol):
    """The most complete 5-min series recorded for a day."""
    best = []
    for _snap, candles, _meta in snapshot_recorder.load_day(
            day, snapshot_dir=snapshot_dir, symbol=symbol):
        if candles and len(candles) > len(best):
            best = candles
    return best


def bias_at(four_h, idx):
    """Discount or premium from a dealing range over the PRECEDING
    DEALING_RANGE_BARS 4H bars. Returns 'discount' | 'premium' | None."""
    window = four_h[max(0, idx - DEALING_RANGE_BARS):idx]
    if len(window) < 3:
        return None
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    if hi <= lo:
        return None
    return "discount" if window[-1].close < (hi + lo) / 2 else "premium"


def rolling_swings(m15, lookback):
    """Local extrema over `lookback` bars each side."""
    highs, lows = [], []
    for i in range(lookback, len(m15) - lookback):
        w = m15[i - lookback:i + lookback + 1]
        if m15[i].high == max(c.high for c in w):
            highs.append((i, m15[i].high))
        if m15[i].low == min(c.low for c in w):
            lows.append((i, m15[i].low))
    return highs, lows


def _last_before(points, i, window):
    """Most recent (idx, price) with i-window <= idx < i. Points are sorted
    by idx, so walk backwards and stop early."""
    for idx, p in reversed(points):
        if idx >= i:
            continue
        if idx < i - window:
            return None
        return p
    return None


def rolling_sweeps(m15, highs, lows, window, min_wick_pct):
    """
    A wick beyond THE MOST RECENT swing that closes back inside it.

    "Koi recent swing LOW sweep ho" -- A recent swing, not the extreme of
    the window. Using the window's max/min instead demanded a poke through
    the 4-session extreme and, with the BOS test applied to that same
    extreme, produced ZERO completed setups across 4,854 bars.
    """
    out = []
    for i, c in enumerate(m15):
        ph = _last_before(highs, i, window)
        pl = _last_before(lows, i, window)
        if ph is not None and c.high > ph and c.close < ph                 and (c.high - ph) / ph * 100 >= min_wick_pct:
            out.append({"i": i, "buy": False, "extreme": c.high})
        if pl is not None and c.low < pl and c.close > pl                 and (pl - c.low) / pl * 100 >= min_wick_pct:
            out.append({"i": i, "buy": True, "extreme": c.low})
    return out


def local_fvgs(m15):
    """3-bar imbalance, indexed by the bar that completes it. No global
    mitigation filter: the PDF's FVG is the one the BOS impulse just
    created, so it is fresh by construction."""
    out = []
    for i in range(2, len(m15)):
        a, c = m15[i - 2], m15[i]
        if a.high < c.low:
            out.append({"i": i, "kind": "bull", "low": a.high, "high": c.low})
        elif a.low > c.high:
            out.append({"i": i, "kind": "bear", "low": c.high, "high": a.low})
    return out


def find_setups(m15, bias_of, prm):
    """The sweep -> BOS -> FVG -> 71% sequence over the continuous series."""
    if len(m15) < prm["swing_lookback"] * 2 + 4:
        return []
    highs, lows = rolling_swings(m15, prm["swing_lookback"])
    sweeps = rolling_sweeps(m15, highs, lows, prm["structure_window"], prm["min_wick_pct"])
    fvgs = local_fvgs(m15)
    out = []

    for sw in sweeps:
        i, want_buy = sw["i"], sw["buy"]
        bias = bias_of[i] if i < len(bias_of) else None
        if bias is None:
            continue
        # The PDF calls the bias filter the single most important rule.
        if want_buy and bias != "discount":
            continue
        if not want_buy and bias != "premium":
            continue

        sweep_extreme = sw["extreme"]
        # BOS breaks THE MOST RECENT opposing swing -- the PDF says "swing
        # high ke upar close", a swing high, not the highest one in the
        # window. Using the window extreme required clearing a 4-session
        # high within 12 bars and rejected all 21 bias-passing sweeps.
        level = _last_before(highs if want_buy else lows, i + 1, prm["structure_window"])
        if level is None:
            continue

        bos_idx = None
        for j in range(i + 1, min(i + 1 + prm["bos_max_bars"], len(m15))):
            if (want_buy and m15[j].close > level) or (not want_buy and m15[j].close < level):
                bos_idx = j
                break
        if bos_idx is None:
            continue

        leg = m15[i:bos_idx + 1]
        bos_extreme = max(c.high for c in leg) if want_buy else min(c.low for c in leg)
        span = abs(bos_extreme - sweep_extreme)
        if span <= 0:
            continue
        entry = (bos_extreme - FIB_ENTRY * span) if want_buy else (bos_extreme + FIB_ENTRY * span)

        want_kind = "bull" if want_buy else "bear"
        has_fvg = any(f["kind"] == want_kind and i <= f["i"] <= bos_idx
                      and f["low"] <= entry <= f["high"] for f in fvgs)
        if not has_fvg:
            continue

        buf = sweep_extreme * prm["sl_buffer_pct"] / 100
        stop = (sweep_extreme - buf) if want_buy else (sweep_extreme + buf)
        out.append({"direction": "BUY" if want_buy else "SELL", "bos_idx": bos_idx,
                    "entry": entry, "stop": stop, "target": bos_extreme, "bias": bias})
    return out


def _close(t, ts, px, outcome):
    t.exit_ts, t.exit, t.outcome = ts, px, outcome
    t.points = (t.exit - t.entry) if t.direction == "BUY" else (t.entry - t.exit)
    risk = abs(t.entry - t.stop)
    t.r_multiple = t.points / risk if risk else 0.0
    return t


def simulate(m15, day_of, bias_of, prm):
    """One position at a time; force-close at each day boundary."""
    pending = {}
    for st in find_setups(m15, bias_of, prm):
        pending.setdefault(st["bos_idx"], st)

    trades, open_trade = [], None
    for k, c in enumerate(m15):
        if open_trade is not None:
            t = open_trade
            if day_of[k] != t.day:
                prev = m15[k - 1]
                trades.append(_close(t, prev.timestamp, prev.close, "EOD"))
                open_trade = None
            else:
                hit_stop = c.low <= t.stop if t.direction == "BUY" else c.high >= t.stop
                hit_tgt = c.high >= t.target if t.direction == "BUY" else c.low <= t.target
                # Stop checked first when one bar spans both: the intrabar
                # path is unknown, so assume the worse ordering.
                if hit_stop:
                    trades.append(_close(t, c.timestamp, t.stop, "LOSS"))
                    open_trade = None
                elif hit_tgt:
                    trades.append(_close(t, c.timestamp, t.target, "WIN"))
                    open_trade = None
            if open_trade is not None:
                continue

        st = pending.get(k)
        if st is None:
            continue
        for j in range(k + 1, min(k + 1 + prm["limit_max_bars"], len(m15))):
            if day_of[j] != day_of[k]:
                break               # the limit order does not survive the session
            bar = m15[j]
            filled = (bar.low <= st["entry"]) if st["direction"] == "BUY" \
                else (bar.high >= st["entry"])
            if filled:
                open_trade = Trade(day=day_of[j], direction=st["direction"],
                                   entry_ts=bar.timestamp, entry=st["entry"],
                                   stop=st["stop"], target=st["target"], bias=st["bias"])
                break

    if open_trade is not None:
        last = m15[-1]
        trades.append(_close(open_trade, last.timestamp, last.close, "EOD"))
    return trades


def run(days, snapshot_dir, symbol, label, prm, cache=None):
    per_day, five_min_all = [], []
    for day in days:
        c = day_candles(day, snapshot_dir, symbol)
        if not c:
            continue
        per_day.append((day, c))
        five_min_all.extend(c)
    if not per_day:
        return summarise([], label, 0)

    four_h = resample(five_min_all, BARS_PER_4H)

    m15, day_of = [], []
    for day, c in per_day:
        bars = resample(c, BARS_PER_15MIN)
        m15.extend(bars)
        day_of.extend([day] * len(bars))

    bars_per_4h_in_15 = BARS_PER_4H // BARS_PER_15MIN
    bias_of = [bias_at(four_h, k // bars_per_4h_in_15) for k in range(len(m15))]

    return summarise(simulate(m15, day_of, bias_of, prm), label, len(per_day))


def summarise(trades, label, n_days):
    if not trades:
        return {"label": label, "n_days": n_days, "n_trades": 0,
                "note": "no setups met all three conditions"}
    wins = [t for t in trades if t.points > 0]
    losses = [t for t in trades if t.points <= 0]
    gross_win = sum(t.points for t in wins)
    gross_loss = abs(sum(t.points for t in losses))
    rs = [t.r_multiple for t in trades]
    eq = peak = dd = 0.0
    for t in trades:
        eq += t.r_multiple
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "label": label, "n_days": n_days, "n_trades": len(trades),
        "trades_per_year": round(len(trades) / max(n_days, 1) * 250, 1),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "total_points": round(sum(t.points for t in trades), 1),
        "expectancy_r": round(statistics.mean(rs), 4),
        "total_r": round(sum(rs), 1),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_dd_r": round(dd, 2),
        "avg_win_r": round(statistics.mean([t.r_multiple for t in wins]), 3) if wins else 0,
        "avg_loss_r": round(statistics.mean([t.r_multiple for t in losses]), 3) if losses else 0,
        "outcomes": dict(Counter(t.outcome for t in trades)),
        "directions": dict(Counter(t.direction for t in trades)),
    }


def describe(rows):
    keys = [("n_days", "{}"), ("n_trades", "{}"), ("trades_per_year", "{}"),
            ("win_rate_pct", "{}%"), ("total_points", "{}"), ("expectancy_r", "{:+}"),
            ("total_r", "{:+}"), ("profit_factor", "{}"), ("max_dd_r", "{}"),
            ("avg_win_r", "{:+}"), ("avg_loss_r", "{:+}")]
    lines = ["", f"  {'metric':<18}" + "".join(f"{r['label']:>18}" for r in rows)]
    for k, fmt in keys:
        cells = [(fmt.format(r[k]) if r.get(k) is not None else "n/a") for r in rows]
        lines.append(f"  {k:<18}" + "".join(f"{c:>18}" for c in cells))
    lines.append("")
    for r in rows:
        lines.append(f"  {r['label']}: outcomes={r.get('outcomes')} dirs={r.get('directions')}")
    return "\n".join(lines)


DEFAULT_PRM = {
    "swing_lookback": 5, "min_wick_pct": 0.1, "structure_window": 100,
    "bos_max_bars": 12, "limit_max_bars": 12, "sl_buffer_pct": 0.05,
}

# The PDF fixes none of these, so one configuration is not an answer. The
# grid spans the range an SMC trader might plausibly read into the rules:
# tighter swings and smaller wicks = more setups, looser = fewer.
GRID = [
    {"swing_lookback": sl, "min_wick_pct": w, "limit_max_bars": lb}
    for sl in (2, 3, 5)
    for w in (0.02, 0.05, 0.10)
    for lb in (12, 25)
]


def load_series(days, snapshot_dir, symbol):
    per_day, five = [], []
    for day in days:
        c = day_candles(day, snapshot_dir, symbol)
        if not c:
            continue
        per_day.append((day, c))
        five.extend(c)
    if not per_day:
        return None
    four_h = resample(five, BARS_PER_4H)
    m15, day_of = [], []
    for day, c in per_day:
        bars = resample(c, BARS_PER_15MIN)
        m15.extend(bars)
        day_of.extend([day] * len(bars))
    per_4h = BARS_PER_4H // BARS_PER_15MIN
    bias_of = [bias_at(four_h, k // per_4h) for k in range(len(m15))]
    return m15, day_of, bias_of, len(per_day)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/liquidity_sniper_study.json")
    p.add_argument("--limit-days", type=int, default=None)
    args = p.parse_args()

    all_days = sorted(snapshot_recorder.available_days(snapshot_dir=BN_SNAPSHOT_DIR))
    hist, live = [], []
    for d in all_days:
        first = next(snapshot_recorder.load_day(
            d, snapshot_dir=BN_SNAPSHOT_DIR, symbol="BANKNIFTY"), None)
        if first is None:
            continue
        (hist if first[0].source == "dhan_historical" else live).append(d)
    if args.limit_days:
        hist = hist[-args.limit_days:]

    print(f"Bank Nifty: {len(hist)} reconstructed days, "
          f"{len(live)} live-recorded days\n", flush=True)

    out = {"grid": [], "live": []}
    series = load_series(hist, BN_SNAPSHOT_DIR, "BANKNIFTY")
    live_series = load_series(live, BN_SNAPSHOT_DIR, "BANKNIFTY") if live else None

    print(f"  {'swing':>6}{'wick%':>7}{'limit':>7}{'trades':>8}{'per yr':>8}"
          f"{'win%':>7}{'expR':>9}{'totalR':>9}{'PF':>7}{'maxDDr':>8}")
    for g in GRID:
        prm = dict(DEFAULT_PRM, **g)
        m15, day_of, bias_of, n_days = series
        r = summarise(simulate(m15, day_of, bias_of, prm), "hist", n_days)
        r["params"] = g
        out["grid"].append(r)
        print(f"  {g['swing_lookback']:>6}{g['min_wick_pct']:>7}{g['limit_max_bars']:>7}"
              f"{r['n_trades']:>8}{r.get('trades_per_year', 0):>8}"
              f"{r.get('win_rate_pct', 0):>7}{r.get('expectancy_r', 0):>9}"
              f"{r.get('total_r', 0):>9}{str(r.get('profit_factor')):>7}"
              f"{r.get('max_dd_r', 0):>8}", flush=True)

        if live_series:
            lm, ld, lb, ln = live_series
            lr = summarise(simulate(lm, ld, lb, prm), "live", ln)
            lr["params"] = g
            out["live"].append(lr)

    best = max((r for r in out["grid"] if r["n_trades"] >= 20),
               key=lambda r: r.get("total_r", 0), default=None)
    print()
    if best:
        print(f"  most-traded/best-R config with >=20 trades: {best['params']}")
        print(f"    {best['n_trades']} trades, {best['win_rate_pct']}% win, "
              f"expectancy {best['expectancy_r']:+}R, total {best['total_r']:+}R, "
              f"PF {best['profit_factor']}, maxDD {best['max_dd_r']}R")
        lv = next((r for r in out["live"] if r["params"] == best["params"]), None)
        if lv:
            print(f"    same config on {lv['n_days']} LIVE-recorded days: "
                  f"{lv['n_trades']} trades")
    else:
        print("  NO configuration in the grid produced 20+ trades.")

    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
