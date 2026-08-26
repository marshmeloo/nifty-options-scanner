"""
Does ORB work on individual "stocks in play" (high relative-volume F&O
stocks), rather than on the index? RESEARCH ONLY -- nothing here trades.

WHY THIS IS A DIFFERENT QUESTION FROM THE INDEX STUDY
--------------------------------------------------------
Index ORB (orb_study.py) closed null specifically because "you cannot
pick today's NIFTY out of a universe of NIFTYs" -- the relative-volume
FILTER, which the published evidence (Zarattini, Barbon & Aziz 2024)
says IS the strategy (unfiltered ORB across 7,000+ US stocks LOST to
buy-and-hold; filtering to the top-20 by opening relative volume turned
it into 1,637% total return), has no index analogue. The 208-name F&O
stock universe this project backfilled for the stocks-in-play pullback
study restores exactly that missing ingredient.

WHAT THE BACKLOG SAID GOING IN
--------------------------------
"The one ORB variant genuinely worth testing, but gated on a cost test
that the published evidence suggests it will probably fail." An
independent replication of the flagship paper found it break-even at
~2.2 cents/share of slippage on QQQ, one of the most liquid instruments
on earth -- the entire published edge lived inside the bid-ask spread.
So this measures BOTH halves: the gross R-multiple edge (same three
bars orb_study.py required on the index) AND the break-even slippage
against what these specific selected names actually, measurably trade
at (research/stock_spread_recorder.py's real recordings).

SELECTION: reconstructed, look-ahead-safe by construction
-------------------------------------------------------------
Same RVOL definition as stocks_in_play.py: first-15-minute volume vs.
that SAME stock's trailing 20-day average first-15-minute volume, only
prior days feeding the baseline. Cross-sectional: every eligible stock
(>=10 days of trailing history) is ranked EACH DAY and the top N are
"in play" for that day only. Two sizes tested (N=10, N=20) -- there is
no clean way to scale the published "top 20 of 7,000+" ratio onto a
208-name universe, so both are reported rather than picking one.

SCOPE DECISION: OR=15min ONLY, not the full 4-length grid orb_study.py
used on the index. Matches the RVOL selection window itself (both read
the first 15 minutes), and keeps the cross product (2 selection sizes x
2 selection methods x ~9 years x up to 20 stocks/day x variants)
tractable. If this comes back promising, sweeping OR length is the
natural follow-up -- not done here because the cost test is the one
that matters first (see backlog).

TWO CONTROLS, ANSWERING DIFFERENT QUESTIONS
----------------------------------------------
  - RANDOM entry (orb.py's own benchmark): same day, same stock, same
    OR geometry, coin-flip direction. Isolates whether ORB's entry RULE
    beats the payoff SHAPE it's built from (frequent small -1R losses,
    occasional large open-ended wins).
  - RANDOM-N SELECTION (this script's own addition): N stocks chosen at
    RANDOM from the same day's eligible pool, instead of by RVOL rank,
    then run through the SAME ORB entry rule. Isolates whether the RVOL
    FILTER ITSELF adds anything, separately from the entry rule --
    directly tests the literature's central claim that "the filter IS
    the strategy."

Run:
    python -m research.orb_stocks_in_play_study
    python -m research.orb_stocks_in_play_study --out logs/x.json
"""

import argparse
import json
import math
import random as _random
import statistics
from collections import defaultdict

from research import orb
from research import stock_costs
from research import stock_data
from component_study import _inv_norm   # Acklam inverse-normal, already used for the same purpose

SELECTION_MINUTES = 15
RVOL_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS = 10
SESSION_OPEN_MIN = 9 * 60 + 15

# Same floor orb_study.py applies on the index, same reasoning: without
# it a near-zero-width opening range manufactures an absurd R-multiple
# out of an ordinary move. Applied identically to every variant here too.
MIN_RISK_PCT = 0.1

TOP_N_OPTIONS = (10, 20)
OOS_SPLIT_DATE = "2025-01-01"

# Fixed risk per trade for the cost layer, in rupees -- an arbitrary but
# CONSISTENT unit (see module docstring: qty is sized so a stop-out
# loses this much, mirroring how a trader would actually size an ORB
# trade, rather than a fixed share count that would let expensive
# stocks dominate the pooled cost figure).
RISK_PER_TRADE_INR = 5000.0


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_bar_dicts(rows: list) -> list:
    return [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows]


def compute_rvol_by_day(symbols: list) -> dict:
    """day -> [(symbol, rvol), ...] for every symbol with >= MIN_LOOKBACK_DAYS
    of trailing opening-volume history that day. One sequential pass per
    symbol -- RVOL only ever looks at that same symbol's own past."""
    rvol_by_day = defaultdict(list)
    for sym in symbols:
        data = stock_data.load(sym)
        hist = []
        for day in sorted(data):
            bars = data[day]
            if len(bars) < 40:
                hist.append(None)
                continue
            head = [b for b in bars if SESSION_OPEN_MIN <= _minutes(b[0]) < SESSION_OPEN_MIN + SELECTION_MINUTES]
            if len(head) < SELECTION_MINUTES // 5:
                hist.append(None)
                continue
            open_vol = sum(b[5] or 0 for b in head)
            prior = [v for v in hist[-RVOL_LOOKBACK_DAYS:] if v]
            hist.append(open_vol)
            if len(prior) < MIN_LOOKBACK_DAYS:
                continue
            baseline = statistics.mean(prior)
            if baseline <= 0:
                continue
            rvol_by_day[day].append((sym, open_vol / baseline))
    return rvol_by_day


def build_selections(rvol_by_day: dict) -> dict:
    """{selection_name: {day: set(symbols)}} for top-N (by RVOL rank) and
    random-N (same eligible pool, unranked) at each N in TOP_N_OPTIONS."""
    selections = {}
    for n in TOP_N_OPTIONS:
        top, rand = {}, {}
        for day, rows in rvol_by_day.items():
            ranked = sorted(rows, key=lambda x: -x[1])
            top[day] = {sym for sym, _ in ranked[:n]}
            pool = [sym for sym, _ in rows]
            rng = _random.Random(f"orb_stocks:{n}:{day}")
            rand[day] = set(rng.sample(pool, min(n, len(pool))))
        selections[f"top{n}"] = top
        selections[f"random{n}"] = rand
    return selections


def core_variants() -> list:
    """OR=15min only -- see module docstring for why. Same entry set and
    same three benchmarks orb_study.py used on the index."""
    out = []
    for entry in ("or_direction", "breakout", "breakout_or_direction", "close_confirm"):
        out.append(orb.ORBVariant(name=entry, or_minutes=SELECTION_MINUTES,
                                  entry=entry, min_risk_pct=MIN_RISK_PCT))
    for bench in ("random", "always_long", "always_short"):
        out.append(orb.ORBVariant(name=bench.upper(), or_minutes=SELECTION_MINUTES,
                                  entry=bench, seed=42, min_risk_pct=MIN_RISK_PCT))
    return out


def run_study(selections: dict, variants: list, symbols: list) -> dict:
    """One pass per symbol (loads its bars once), checking every
    selection set's membership for every day that symbol has data.
    Returns {selection_name: {variant_name: [trade, ...]}}."""
    trades = {sel_name: {v.name: [] for v in variants} for sel_name in selections}
    for sym in symbols:
        data = stock_data.load(sym)
        for day, bars in data.items():
            bar_dicts = None
            for sel_name, sel_by_day in selections.items():
                sel = sel_by_day.get(day)
                if not sel or sym not in sel:
                    continue
                if bar_dicts is None:
                    bar_dicts = _to_bar_dicts(bars)
                for v in variants:
                    # orb.py's RANDOM entry seeds on `day` alone (fine for
                    # the single-instrument index study it was built for).
                    # Here many symbols share a day -- an unmodified seed
                    # would give every stock the SAME coin-flip direction
                    # on the same day, which is not an independent control.
                    # Pass a symbol-qualified key for the seed, then
                    # restore the real calendar day for OOS splitting.
                    t = orb.simulate_day(bar_dicts, v, day=f"{sym}:{day}")
                    if t:
                        t["day"] = day
                        t["symbol"] = sym
                        trades[sel_name][v.name].append(t)
    return trades


def summarize(trades: list) -> dict:
    if not trades:
        return {"n_trades": 0}
    rs = [t["r_multiple"] for t in trades]
    mean_r = statistics.mean(rs)
    sd = statistics.pstdev(rs)
    se = sd / math.sqrt(len(rs)) if rs else 0
    wins = sum(1 for r in rs if r > 0)
    ins = [t for t in trades if t["day"] < OOS_SPLIT_DATE]
    oos = [t for t in trades if t["day"] >= OOS_SPLIT_DATE]
    return {
        "n_trades": len(trades),
        "mean_r": round(mean_r, 4),
        "se_r": round(se, 4),
        "t_stat": round(mean_r / se, 2) if se > 0 else None,
        "win_rate_pct": round(wins / len(rs) * 100, 1),
        "in_sample_mean_r": round(statistics.mean([t["r_multiple"] for t in ins]), 4) if ins else None,
        "in_sample_n": len(ins),
        "out_sample_mean_r": round(statistics.mean([t["r_multiple"] for t in oos]), 4) if oos else None,
        "out_sample_n": len(oos),
    }


def _z_diff(a: list, b: list) -> dict:
    if not a or not b:
        return {}
    ra, rb = [t["r_multiple"] for t in a], [t["r_multiple"] for t in b]
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    sea = statistics.pstdev(ra) / math.sqrt(len(ra))
    seb = statistics.pstdev(rb) / math.sqrt(len(rb))
    sed = math.sqrt(sea ** 2 + seb ** 2)
    return {"edge": round(ma - mb, 4), "z": round((ma - mb) / sed, 2) if sed > 0 else None}


def cost_layer(trades: list) -> dict:
    """Break-even slippage for a set of trades, sized at a fixed rupee
    risk per trade (see RISK_PER_TRADE_INR). Statutory costs are real
    Indian intraday-equity rates (stock_costs.py); slippage is left as
    the free variable this reports the break-even for, since the
    backfill holds OHLCV only -- no bid/ask -- so it cannot be measured
    from this data (see stock_costs.py's own docstring)."""
    if not trades:
        return {"n": 0}
    cost_rows = []
    for t in trades:
        risk_per_share = t["risk_points"]
        if risk_per_share <= 0:
            continue
        qty = max(1, round(RISK_PER_TRADE_INR / risk_per_share))
        entry, exitp = t["entry"], t["exit"]
        move = (exitp - entry) if t["direction"] == "long" else (entry - exitp)
        raw_gross = move * qty
        turnover = (entry + exitp) * qty
        statutory = stock_costs.statutory_costs(entry, exitp, qty)["total"]
        cost_rows.append({"gross_inr": raw_gross - statutory, "turnover_inr": turnover})
    if not cost_rows:
        return {"n": 0}
    total_gross = sum(r["gross_inr"] for r in cost_rows)
    return {
        "n": len(cost_rows),
        "total_gross_inr_net_of_statutory": round(total_gross, 0),
        "break_even_slippage_bps": round(stock_costs.break_even_slippage_bps(cost_rows), 2),
    }


def analyse(selections: dict, variants: list, symbols: list) -> dict:
    all_trades = run_study(selections, variants, symbols)
    tested_entries = [v.name for v in variants if v.name.islower()]  # excludes RANDOM/ALWAYS_*
    n_tests = len(tested_entries) * len(TOP_N_OPTIONS)  # entries x {top10, top20}
    bonferroni_t = round(abs(_inv_norm(0.025 / max(n_tests, 1))), 2)

    results = {}
    for sel_name, by_variant in all_trades.items():
        sel_results = {}
        for v in variants:
            trades = by_variant[v.name]
            row = summarize(trades)
            row["cost"] = cost_layer(trades)
            if v.name in tested_entries:
                rand_trades = by_variant.get("RANDOM", [])
                row.update({"vs_random_entry": _z_diff(trades, rand_trades)})
            sel_results[v.name] = row
        results[sel_name] = sel_results

    # RVOL-filter value: same entry rule, RVOL-ranked selection vs
    # random selection at the same N -- the direct test of "the filter
    # IS the strategy."
    filter_value = {}
    for n in TOP_N_OPTIONS:
        top_trades, rand_trades = all_trades[f"top{n}"], all_trades[f"random{n}"]
        filter_value[f"N={n}"] = {
            entry: _z_diff(top_trades[entry], rand_trades[entry])
            for entry in tested_entries
        }

    return {
        "n_tests": n_tests,
        "bonferroni_t_bar": bonferroni_t,
        "oos_split": OOS_SPLIT_DATE,
        "selections": results,
        "filter_value_vs_random_n": filter_value,
    }


def describe(summary: dict) -> str:
    lines = [
        f"ORB on stocks-in-play: OR={SELECTION_MINUTES}min, N in {TOP_N_OPTIONS}",
        f"{summary['n_tests']} real variants tested -> Bonferroni bar |t| > {summary['bonferroni_t_bar']}",
        f"out-of-sample split at {summary['oos_split']}, risk/trade = Rs{RISK_PER_TRADE_INR:,.0f} for the cost layer",
    ]
    for sel_name, rows in summary["selections"].items():
        lines.append("")
        lines.append(f"-- selection: {sel_name} --")
        lines.append(f"{'variant':<24}{'n':>7}{'meanR':>9}{'t':>7}{'win%':>7}"
                     f"{'vsRand z':>10}{'IS':>8}{'OOS':>8}{'BEslip(bps)':>13}")
        order = sorted(rows.items(), key=lambda kv: -(kv[1].get("mean_r") or -99))
        for name, r in order:
            if not r.get("n_trades"):
                lines.append(f"{name:<24}{'0':>7}  (no trades)")
                continue
            t = r.get("t_stat") or 0
            vr = (r.get("vs_random_entry") or {}).get("z")
            # both bars require the POSITIVE direction -- beats zero AND
            # beats random, not just "significantly different from"
            # either one (a variant can be significantly WORSE than
            # random, which is a real and important result, not a pass).
            mark = " ***" if t > summary["bonferroni_t_bar"] and vr is not None and vr > 1.96 else ""
            lines.append(
                f"{name:<24}{r['n_trades']:>7,}{r['mean_r']:>+9.4f}{t:>+7.2f}{r['win_rate_pct']:>7.1f}"
                f"{(vr if vr is not None else 0):>+10.2f}"
                f"{(r.get('in_sample_mean_r') or 0):>+8.3f}{(r.get('out_sample_mean_r') or 0):>+8.3f}"
                f"{r['cost'].get('break_even_slippage_bps', 0):>13.2f}{mark}"
            )
    lines.append("")
    lines.append("RVOL-filter value (top-N vs random-N, SAME entry rule, isolates the filter itself):")
    for n_label, entries in summary["filter_value_vs_random_n"].items():
        for entry, d in entries.items():
            if d:
                lines.append(f"  {n_label:<8} {entry:<24} edge={d.get('edge', 0):>+8.4f}R  z={d.get('z', 0):>+6.2f}")
    lines += [
        "",
        "BEslip(bps) = break-even slippage in basis points (per leg) -- compare against",
        "  what the selected names actually trade at (stock_spread_recorder.py's real",
        "  measurements: universe median ~3.3bps, selected high-RVOL names often lower).",
        "  If BEslip is below that, the strategy is dead regardless of gross R.",
        "*** clears Bonferroni AND beats the random-entry benchmark at 95%",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/orb_stocks_in_play_study.json")
    p.add_argument("--limit", type=int, default=None, help="cap symbols, for a quick smoke run")
    args = p.parse_args()

    symbols = [u["symbol"] for u in stock_data.universe()]
    if args.limit:
        symbols = symbols[:args.limit]
    print(f"{len(symbols)} symbols, computing cross-sectional RVOL...", flush=True)

    rvol_by_day = compute_rvol_by_day(symbols)
    print(f"{len(rvol_by_day):,} trading days with an RVOL-eligible pool", flush=True)

    selections = build_selections(rvol_by_day)
    variants = core_variants()
    print(f"Running {len(variants)} variants x {len(selections)} selection sets...", flush=True)

    summary = analyse(selections, variants, symbols)
    print()
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
