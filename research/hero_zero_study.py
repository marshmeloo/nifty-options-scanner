"""
"Hero-Zero": buy a deep-OTM NIFTY option while it's trading near-worthless
(Rs 1-5), hold intraday, hoping for a late tail spike. RESEARCH ONLY --
nothing here trades. See BACKLOG.md's scoping entry for the original idea.

THE QUESTION THIS ASKS, SEPARATED INTO TWO PARTS
--------------------------------------------------
1. Is EXPIRY DAY SPECIFICALLY special? A cheap deep-OTM option can spike
   any day (a couple of rupees is a huge % move off a tiny base). The
   retail claim is that gamma/pin dynamics on the contract's OWN expiry
   day make this MORE likely, not that cheap options spike in general.
   Tested by running the identical selection rule on every trading day
   and comparing the EXPIRY-DAY subset (DTE=0 on that weekly contract)
   against the NON-EXPIRY subset (DTE 1-5, same weekly structure --
   this project's reconstruction is already organised by weekly cycle,
   so no extra filtering is needed to get this comparison for free).
2. Does DELIBERATELY PICKING THE CHEAPEST CANDIDATE add anything, or
   would any random deep-OTM strike in the same distance band do just
   as well? Tested with a random-strike control at the same distance
   cap, same day, same option type -- isolates the premium-selection
   rule from "OTM options in this zone are volatile."

DATA-QUALITY CAVEAT, ACTED ON RATHER THAN IGNORED
----------------------------------------------------
BACKLOG.md's corruption study found duplicate-OI/zeroed-IV bad prints
concentrated at the edge of the ATM+/-10 (~500pt) fetch window, rising
from ~0.01% within 250pts to ~1% beyond 350pts. Genuinely cheap (Rs1-5)
strikes cluster toward that edge -- probed directly 2026-08-26, most
sampled expiry days had candidates between 245 and 524pts out. This
script caps candidate selection at MAX_DISTANCE_PTS (300) to stay in
the measurably cleaner band, at the cost of dropping the very deepest,
cheapest candidates and undersampling high-IV expiry days where nothing
inside 300pts is cheap enough yet -- both are real, structural
limitations of the data on hand, not fixed here (same treatment
Condor's data-quality gap already got: state it, don't paper over it).

The consistency checker itself only validates OI/IV, never LTP -- the
one field this study actually depends on -- and a naive spike-then-
revert check on LTP is unusable here anyway: a real tail spike and a
bad print look IDENTICAL under that test (both are "value jumps, then
reverts"). No automated substitute exists; this is a documented,
unresolved risk on the far side of MAX_DISTANCE_PTS, mitigated but not
eliminated by staying inside it.

Run:
    python -m research.hero_zero_study
    python -m research.hero_zero_study --out logs/x.json
"""

import argparse
import math
import json
import random as _random
import statistics
from collections import defaultdict
from datetime import date as _date

import config
import costs
import historical_source as hs
import snapshot_recorder
from component_study import _inv_norm

SELECTION_TIME = "10:25"          # first cycle at/after this HH:MM each day
MAX_DISTANCE_PTS = 300            # clean-band cap, see module docstring
CHEAP_LO, CHEAP_HI = 1.0, 5.0     # "near-worthless" band, in rupees
LOT_SIZE = 65                     # NIFTY, matches config.NIFTY_LOT_SIZE default
TAIL_MULTIPLES = (2, 3, 5, 10)
RANDOM_SEED = "hero_zero"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _select_cycle(cycles: list, hhmm: str):
    for snap, candles, m in cycles:
        if snap.timestamp.strftime("%H:%M") >= hhmm:
            return snap, candles
    return None, None


def _side_strikes(chain, option_type: str, spot: float):
    """OTM strikes on one side, within MAX_DISTANCE_PTS, with a valid LTP."""
    out = []
    for q in chain:
        if q.option_type != option_type or q.ltp is None or q.ltp <= 0:
            continue
        otm = (q.strike > spot) if option_type == "CE" else (q.strike < spot)
        if not otm:
            continue
        dist = abs(q.strike - spot)
        if dist > MAX_DISTANCE_PTS:
            continue
        out.append((q.strike, q.ltp, dist))
    return out


def build_candidates(day: str) -> list:
    """
    One record per (day, option_type) where BOTH a hero-zero candidate
    (cheapest-looking: farthest-OTM strike inside MAX_DISTANCE_PTS whose
    LTP sits in [CHEAP_LO, CHEAP_HI]) AND a random-strike control (same
    band, any LTP) can be tracked to end of day. Skips a side entirely
    if no strike in the band qualifies -- not guessed at.
    """
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []
    snap0, _ = _select_cycle(cycles, SELECTION_TIME)
    if snap0 is None or not snap0.spot:
        return []
    spot = snap0.spot
    is_expiry = hs.nominal_expiry_date(snap0.timestamp.date()).isoformat() == day

    later = [(s.timestamp, s.chain) for s, _c, _m in cycles if s.timestamp >= snap0.timestamp]
    if len(later) < 2:
        return []

    records = []
    for option_type in ("CE", "PE"):
        strikes = _side_strikes(snap0.chain, option_type, spot)
        if not strikes:
            continue

        cheap = [s for s in strikes if CHEAP_LO <= s[1] <= CHEAP_HI]
        hero_strike = max(cheap, key=lambda s: s[2])[0] if cheap else None

        rng = _random.Random(f"{RANDOM_SEED}:{day}:{option_type}")
        random_strike = rng.choice(strikes)[0]

        for label, strike in (("hero_zero", hero_strike), ("random", random_strike)):
            if strike is None:
                continue
            entry_ltp = next((ltp for s, ltp, _d in strikes if s == strike), None)
            if not entry_ltp:
                continue
            path = []
            for ts, chain in later:
                q = next((q for q in chain if q.strike == strike and q.option_type == option_type), None)
                if q is not None and q.ltp is not None and q.ltp >= 0:
                    path.append(q.ltp)
            if len(path) < 2:
                continue
            eod_ltp = path[-1]
            max_ltp = max(path)
            records.append({
                "day": day, "is_expiry": is_expiry, "option_type": option_type,
                "selection": label, "strike": strike,
                "distance_pts": round(abs(strike - spot), 1),
                "entry_ltp": entry_ltp, "eod_ltp": eod_ltp, "max_ltp": max_ltp,
                "ret_eod_pct": round((eod_ltp - entry_ltp) / entry_ltp * 100, 2),
                "max_multiple": round(max_ltp / entry_ltp, 3),
                "gross_pnl_inr_per_lot": round((eod_ltp - entry_ltp) * LOT_SIZE, 2),
            })
    return records


def build_candidates_for_bands(day: str, bands: list) -> list:
    """
    Same mechanics as build_candidates(), generalised to test several
    premium bands in ONE pass over the day (avoids reloading/rewalking
    the day's cycles per band). For each band (lo, hi):
      - "deepest": the farthest-OTM strike inside MAX_DISTANCE_PTS whose
        LTP falls in [lo, hi) -- same selection rule build_candidates()
        uses, just parameterised.
      - "random_in_band": a uniformly random strike from the SAME set
        (same premium band, same distance cap) -- a band-matched
        control, tighter than build_candidates()'s any-premium random
        control, for isolating whether "farthest-OTM within this
        specific premium band" adds anything at THAT price level.
    """
    cycles = list(snapshot_recorder.load_day(day))
    if not cycles:
        return []
    snap0, _ = _select_cycle(cycles, SELECTION_TIME)
    if snap0 is None or not snap0.spot:
        return []
    spot = snap0.spot
    is_expiry = hs.nominal_expiry_date(snap0.timestamp.date()).isoformat() == day

    later = [(s.timestamp, s.chain) for s, _c, _m in cycles if s.timestamp >= snap0.timestamp]
    if len(later) < 2:
        return []

    records = []
    for option_type in ("CE", "PE"):
        strikes = _side_strikes(snap0.chain, option_type, spot)
        if not strikes:
            continue

        for lo, hi in bands:
            in_band = [s for s in strikes if lo <= s[1] < hi]
            if not in_band:
                continue
            deepest_strike = max(in_band, key=lambda s: s[2])[0]
            rng = _random.Random(f"{RANDOM_SEED}:{day}:{option_type}:{lo}:{hi}")
            random_strike = rng.choice(in_band)[0]

            for label, strike in (("deepest", deepest_strike), ("random_in_band", random_strike)):
                entry_ltp = next((ltp for s, ltp, _d in in_band if s == strike), None)
                if not entry_ltp:
                    continue
                path = []
                for ts, chain in later:
                    q = next((q for q in chain if q.strike == strike and q.option_type == option_type), None)
                    if q is not None and q.ltp is not None and q.ltp >= 0:
                        path.append(q.ltp)
                if len(path) < 2:
                    continue
                eod_ltp = path[-1]
                max_ltp = max(path)
                records.append({
                    "day": day, "is_expiry": is_expiry, "option_type": option_type,
                    "band": f"{lo}-{hi}", "selection": label, "strike": strike,
                    "distance_pts": round(abs(strike - spot), 1),
                    "entry_ltp": entry_ltp, "eod_ltp": eod_ltp, "max_ltp": max_ltp,
                    "ret_eod_pct": round((eod_ltp - entry_ltp) / entry_ltp * 100, 2),
                    "max_multiple": round(max_ltp / entry_ltp, 3),
                    "gross_pnl_inr_per_lot": round((eod_ltp - entry_ltp) * LOT_SIZE, 2),
                })
    return records


def _skew(xs: list) -> float:
    if len(xs) < 3:
        return 0.0
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return sum(((x - m) / sd) ** 3 for x in xs) / len(xs) if sd else 0.0


def _apply_costs(records: list) -> list:
    out = []
    for r in records:
        c = costs.round_trip(r["entry_ltp"], r["eod_ltp"], lots=1, lot_size=LOT_SIZE)
        net = r["gross_pnl_inr_per_lot"] - c["total_inr"]
        out.append({**r, "cost_inr": c["total_inr"], "net_pnl_inr_per_lot": round(net, 2)})
    return out


def profile(records: list, label: str) -> dict:
    if not records:
        return {"label": label, "n": 0}
    rets = [r["ret_eod_pct"] for r in records]
    mults = [r["max_multiple"] for r in records]
    net = [r["net_pnl_inr_per_lot"] for r in records]
    gross = [r["gross_pnl_inr_per_lot"] for r in records]
    n = len(records)
    se = statistics.pstdev(rets) / math.sqrt(n) if n > 1 else 0
    return {
        "label": label, "n": n,
        "mean_ret_eod_pct": round(statistics.mean(rets), 2),
        "median_ret_eod_pct": round(statistics.median(rets), 2),
        "skew": round(_skew(rets), 3),
        "win_rate_pct": round(100 * sum(1 for r in rets if r > 0) / n, 1),
        "pct_lost_80pct_or_more": round(100 * sum(1 for r in rets if r <= -80) / n, 1),
        **{f"pct_reached_{m}x": round(100 * sum(1 for x in mults if x >= m) / n, 2) for m in TAIL_MULTIPLES},
        "mean_gross_pnl_inr_per_lot": round(statistics.mean(gross), 1),
        "mean_net_pnl_inr_per_lot": round(statistics.mean(net), 1),
        "mean_cost_inr_per_lot": round(statistics.mean([r["cost_inr"] for r in records]), 1),
        "t_stat_net_pnl": _t_stat([r["net_pnl_inr_per_lot"] for r in records]),
    }


def _t_stat(xs: list):
    n = len(xs)
    if n < 2:
        return None
    se = statistics.pstdev(xs) / math.sqrt(n)
    return round(statistics.mean(xs) / se, 2) if se > 0 else None


def _z_diff(a: list, b: list, field: str) -> dict:
    if not a or not b:
        return {}
    xa, xb = [r[field] for r in a], [r[field] for r in b]
    ma, mb = statistics.mean(xa), statistics.mean(xb)
    sea = statistics.pstdev(xa) / math.sqrt(len(xa))
    seb = statistics.pstdev(xb) / math.sqrt(len(xb))
    sed = math.sqrt(sea ** 2 + seb ** 2)
    return {"edge": round(ma - mb, 2), "z": round((ma - mb) / sed, 2) if sed > 0 else None}


def analyse(records: list) -> dict:
    records = _apply_costs(records)
    groups = {
        "expiry_hero_zero": [r for r in records if r["is_expiry"] and r["selection"] == "hero_zero"],
        "expiry_random": [r for r in records if r["is_expiry"] and r["selection"] == "random"],
        "nonexpiry_hero_zero": [r for r in records if not r["is_expiry"] and r["selection"] == "hero_zero"],
        "nonexpiry_random": [r for r in records if not r["is_expiry"] and r["selection"] == "random"],
    }
    profiles = {k: profile(v, k) for k, v in groups.items()}
    return {
        "n_total": len(records),
        "profiles": profiles,
        "expiry_vs_nonexpiry": _z_diff(groups["expiry_hero_zero"], groups["nonexpiry_hero_zero"], "ret_eod_pct"),
        "hero_zero_vs_random_on_expiry": _z_diff(groups["expiry_hero_zero"], groups["expiry_random"], "ret_eod_pct"),
        "hero_zero_vs_random_nonexpiry": _z_diff(groups["nonexpiry_hero_zero"], groups["nonexpiry_random"], "ret_eod_pct"),
    }


def describe(summary: dict) -> str:
    lines = [
        f"Hero-Zero study: {summary['n_total']:,} candidate legs, "
        f"selection={SELECTION_TIME}, band=Rs{CHEAP_LO}-{CHEAP_HI}, "
        f"distance cap={MAX_DISTANCE_PTS}pts",
        "",
        f"{'group':<22}{'n':>7}{'meanRet%':>10}{'skew':>7}{'win%':>7}{'>=2x':>7}{'>=5x':>7}"
        f"{'>=10x':>7}{'lost80%':>9}{'netP&L/lot':>12}{'t':>7}",
    ]
    for name, p in summary["profiles"].items():
        if not p.get("n"):
            lines.append(f"{name:<22}{'0':>7}  (no candidates)")
            continue
        lines.append(
            f"{name:<22}{p['n']:>7,}{p['mean_ret_eod_pct']:>+10.2f}{p['skew']:>+7.2f}"
            f"{p['win_rate_pct']:>7.1f}{p['pct_reached_2x']:>7.2f}{p['pct_reached_5x']:>7.2f}"
            f"{p['pct_reached_10x']:>7.2f}{p['pct_lost_80pct_or_more']:>9.1f}"
            f"{p['mean_net_pnl_inr_per_lot']:>+12.1f}{(p['t_stat_net_pnl'] or 0):>+7.2f}"
        )
    lines += [
        "",
        f"Is expiry day special? (hero_zero, expiry vs non-expiry, ret% ): {summary['expiry_vs_nonexpiry']}",
        f"Does the cheap-filter add anything on expiry day? (vs random, same band): {summary['hero_zero_vs_random_on_expiry']}",
        f"Does the cheap-filter add anything on a normal day?: {summary['hero_zero_vs_random_nonexpiry']}",
        "",
        "meanRet%/skew/win% are the RAW distribution (what the market offers).",
        "netP&L/lot is AFTER real Indian options costs (costs.py) at 1 lot -- the",
        "  flat per-order brokerage this project already models is a large fraction",
        "  of a Rs1-5 premium, which is exactly the concern the backlog raised.",
        ">=Nx = % of legs whose BEST available exit (not necessarily EOD) reached",
        "  that multiple of entry premium -- the tail-capture ceiling, not what a",
        "  fixed hold-to-EOD strategy actually banks (see meanRet%/netP&L for that).",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="logs/hero_zero_study.json")
    args = p.parse_args()

    days = []
    for day in snapshot_recorder.available_days():
        first = next(snapshot_recorder.load_day(day), None)
        if first is not None and first[0].source == "dhan_historical":
            days.append(day)
    print(f"{len(days)} historical days: {days[0]} .. {days[-1]}", flush=True)

    records = []
    for i, day in enumerate(days):
        try:
            records.extend(build_candidates(day))
        except Exception as e:
            print(f"  {day} failed: {e}")
        if (i + 1) % 200 == 0:
            print(f"  ...{i+1}/{len(days)} days, {len(records):,} candidate legs", flush=True)

    summary = analyse(records)
    print()
    print(describe(summary))
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nwritten to {args.out}")


if __name__ == "__main__":
    main()
