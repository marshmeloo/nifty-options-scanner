"""
Catches gross corruption in historical data we have no live recording to
cross-validate against.

WHY THIS EXISTS
---------------
validate_historical.py (the real check -- decoding matches a live
recording field by field) only works for days we ALSO have a live
recording of. As of 2026-07-31 that's 3 days: 07-29, 07-30, 07-31. A
multi-year backfill is almost entirely days with nothing on the other
side of that comparison -- there is no live snapshot from 2024 to check
2024's reconstruction against, and there never will be.

This is NOT a substitute for validate_historical.py. It cannot catch a
systematic decoding error that is internally self-consistent (exactly the
kind the one-bar-interval timestamp bug would have been, had it not
happened to also move OI/IV, which stayed correct). What it CAN catch is
corruption that isn't self-consistent: values that contradict physical
reality or contradict each other within the same reconstructed day --
still worth ruling out given we can't check the alternative.

Any report built from data this module hasn't run over pre-July-2026
days should say so explicitly: decoding is confirmed only for the 2
live-matched July 2026 days; earlier data has passed consistency checks,
not cross-validation.

WHAT A CHECK HAS TO EARN (added 2026-08-04)
--------------------------------------------
A corruption sweep is only useful if a failure MEANS something. The
first version of the OI/IV checks flagged any large step between
adjacent bars, which failed 72% of a sample of days that had already
been used for every backtest in this project -- and on inspection, most
of those flags were real market moves, not corruption. A check that
cries wolf on the majority of good data is worse than no check: it
buries the genuine signal and trains the reader to skip the report.

Both step checks now require the value to SNAP BACK afterwards, which
is what actually distinguishes a bad print from a real move (see
MAX_OI_STEP_RATIO and REVERSION_RATIO). If a future check can't
articulate what makes its own failures meaningful, it doesn't belong
here.
"""

from datetime import date as _date

import config
import market_regime

# A normal session is MARKET_OPEN..MARKET_CLOSE at the pull's own
# interval. Fewer than this fraction of that count means a partial or
# failed fetch, not a real quiet day -- a quiet day still has every bar,
# just small moves within them.
MIN_BAR_COVERAGE_FRACTION = 0.6

# NIFTY moving further than this in one session, even accounting for the
# 2020-2021 volatility this project has otherwise never seen, is more
# likely a bad tick than a real print. Kept generous on purpose: this is
# a corruption filter, not a volatility judgment -- config.py's regime
# tools are for the latter.
MAX_PLAUSIBLE_DAILY_RANGE_PCT = 8.0

# OI more than doubling or halving between consecutive bars is the FIRST
# half of the bad-print test, not the whole test on its own -- see
# REVERSION_RATIO below.
#
# The original assumption here ("not organic buildup/unwinding at any
# liquidity this project trades") was measured and found WRONG on
# 2026-08-04. Across a 20-day sample, 93 of 146 flagged jumps were
# genuinely real -- e.g. 2,133,625 -> 4,811,850 -> 6,515,375, OI simply
# building hard on a liquid near-ATM weekly strike. The flagged strikes
# were not thin either: median OI of the larger side was ~158,000. On
# NIFTY weeklies, OI really can more than double in five minutes.
#
# Flagging on ratio alone therefore failed 72% of a sample of days that
# had already been used for every backtest in this project, which is
# worse than useless -- it buries the real signal in noise and trains
# the reader to ignore the sweep.
MAX_OI_STEP_RATIO = 2.0

# The SECOND half, and what actually separates corruption from a real
# move: does the jump PERSIST?
#
# A genuine OI change stays changed -- the next bar continues from the
# new level. A bad print spikes for exactly one bar and the level
# immediately snaps back to where it was, e.g. 43,800 -> 329,125 ->
# 43,950 (2024-09-17, 24850CE, where LTP jumped anomalously on the same
# bar and reverted with it). A jump is only reported when the reading
# AFTER it returns within this ratio of the reading BEFORE it.
#
# On the same 20-day sample this splits 146 raw flags into 53 genuine
# transient spikes and 93 real market moves that are no longer reported.
REVERSION_RATIO = 1.3

# An IV jump this large between adjacent bars for the SAME strike is a
# decode/misalignment symptom, not a real vol move -- see the 2026-07-31
# incident where a wrong shift corrupted price without erroring.
#
# Zero IV is EXCLUDED from this check rather than treated as a value.
# Dhan doesn't publish IV for thinner strikes and reports 0.0 instead, so
# a strike flickering in and out of coverage produces 0.0 -> 22.9 -> 0.0
# steps all day. That is missing data, not a vol move: an option with a
# real traded price cannot have zero implied volatility, and 483 of 554
# zero-IV quotes on a sample day had LTP >= 1. Counting them flagged 391
# of 493 days and buried the real signal. The live recording shows the
# same 18.6% zero rate, which is itself evidence the reconstruction is
# faithful rather than broken.
MAX_IV_STEP_POINTS = 15.0


def _expected_bar_count(interval_minutes: int) -> int:
    return max(1, market_regime._SESSION_MINUTES // interval_minutes)


def check_day(snapshots: list, interval_minutes: int = 5) -> dict:
    """
    Run all checks over one day's reconstructed snapshots (already sorted
    by timestamp). Returns {"day", "issues": [...], "passed": bool}.

    Every check APPENDS a human-readable issue rather than raising, so
    one bad cycle doesn't stop the rest of the day's checks from running
    -- for a corruption sweep, seeing every problem in one pass matters
    more than failing fast on the first.
    """
    if not snapshots:
        return {"day": None, "issues": ["no snapshots for this day"], "passed": False}

    day = snapshots[0].timestamp.date()
    issues = []

    expected = _expected_bar_count(interval_minutes)
    coverage = len(snapshots) / expected
    if coverage < MIN_BAR_COVERAGE_FRACTION:
        issues.append(
            f"only {len(snapshots)}/{expected} expected bars ({coverage:.0%} coverage) "
            f"-- likely a partial or failed fetch, not a quiet day"
        )

    spots = [s.spot for s in snapshots if s.spot]
    if spots:
        day_open = spots[0]
        rng_pct = (max(spots) - min(spots)) / day_open * 100 if day_open else 0.0
        if rng_pct > MAX_PLAUSIBLE_DAILY_RANGE_PCT:
            issues.append(
                f"spot range {rng_pct:.1f}% of open exceeds the "
                f"{MAX_PLAUSIBLE_DAILY_RANGE_PCT}% plausibility ceiling -- suspect a bad print"
            )
        if any(sp <= 0 for sp in spots):
            issues.append("non-positive spot value present")

    # Per-(strike, option_type) OI/IV series, at exactly the granularity
    # live buildup classification already uses. Built as full series
    # rather than checked pairwise on the fly because the spike test
    # needs the reading AFTER a jump as well as the one before it -- see
    # REVERSION_RATIO.
    series = {}
    for snap in snapshots:
        for q in snap.chain:
            if q.oi < 0:
                issues.append(f"negative OI at strike {q.strike}{q.option_type}, {snap.timestamp}")
            if q.iv < 0:
                issues.append(f"negative IV at strike {q.strike}{q.option_type}, {snap.timestamp}")
            series.setdefault((q.strike, q.option_type), []).append(
                (snap.timestamp, q.oi, q.iv, abs(q.strike - snap.spot) if snap.spot else None)
            )

    # Distance from spot is carried on every reported issue, and
    # aggregated below, because WHERE the corruption sits decides who it
    # affects. Measured 2026-08-04 across 50 sampled days: contamination
    # is zero within 150pts of spot in both the 2024-2026 and 2020-2024
    # datasets, and rises with distance (2020-2024: 0.13% at 150-250pts,
    # 0.53% at 250-350pts, 1.06% beyond 350pts). A near-ATM strategy is
    # therefore unaffected by a day that fails purely on far-OTM strikes,
    # while a strategy whose legs sit 400pts+ out is squarely exposed --
    # a distinction a bare pass/fail cannot make.
    flagged_distances = []

    for (strike, option_type), points in series.items():
        # Stop one short of the end: a jump on the very last reading has
        # no "after" to confirm against, so it cannot be distinguished
        # from a real move and is deliberately NOT reported rather than
        # guessed at either way.
        for i in range(1, len(points) - 1):
            _prev_ts, prev_oi, prev_iv, _pd = points[i - 1]
            ts, oi, iv, dist = points[i]
            _next_ts, next_oi, next_iv, _nd = points[i + 1]
            where = f", {dist:.0f}pts from spot" if dist is not None else ""

            if prev_oi > 0 and oi > 0 and next_oi > 0:
                ratio = max(oi, prev_oi) / min(oi, prev_oi)
                reverted = max(next_oi, prev_oi) / min(next_oi, prev_oi) <= REVERSION_RATIO
                if ratio > MAX_OI_STEP_RATIO and reverted:
                    issues.append(
                        f"transient OI spike {prev_oi}->{oi}->{next_oi} "
                        f"at {strike}{option_type}, {ts}{where}"
                    )
                    if dist is not None:
                        flagged_distances.append(dist)

            if prev_iv > 0 and iv > 0 and next_iv > 0:
                spiked = abs(iv - prev_iv) > MAX_IV_STEP_POINTS
                reverted = abs(next_iv - prev_iv) <= MAX_IV_STEP_POINTS / 2
                if spiked and reverted:
                    issues.append(
                        f"transient IV spike {prev_iv:.1f}->{iv:.1f}->{next_iv:.1f} "
                        f"at {strike}{option_type}, {ts}{where}"
                    )
                    if dist is not None:
                        flagged_distances.append(dist)

    # `nearest_flagged_pts` is the number to read first on a failure: it
    # is the closest-to-spot corruption found, so anything trading nearer
    # than that is untouched by this day's issues.
    nearest_flagged = min(flagged_distances) if flagged_distances else None

    # Cap the report: a genuinely corrupt day can produce thousands of
    # per-strike issues, which buries the ones worth reading rather than
    # helping triage them.
    MAX_ISSUES_REPORTED = 20
    truncated = len(issues) > MAX_ISSUES_REPORTED
    if truncated:
        extra = len(issues) - MAX_ISSUES_REPORTED
        issues = issues[:MAX_ISSUES_REPORTED] + [f"... and {extra} more issue(s), truncated"]

    return {
        "day": day.isoformat() if isinstance(day, _date) else str(day),
        "bars": len(snapshots),
        "expected_bars": expected,
        "issues": issues,
        "passed": not issues,
        "nearest_flagged_pts": round(nearest_flagged, 1) if nearest_flagged is not None else None,
    }


def check_range(day_to_snapshots: dict, interval_minutes: int = 5) -> dict:
    """
    Run check_day over every day in a backfill. `day_to_snapshots` maps
    an ISO date string to that day's sorted snapshot list (the same
    grouping historical_source.backfill() already builds internally).

    Returns a summary plus the per-day results, so a caller can decide
    whether to trust the whole pull or go look at specific days.
    """
    results = {day: check_day(snaps, interval_minutes=interval_minutes)
              for day, snaps in day_to_snapshots.items()}
    failed_days = [d for d, r in results.items() if not r["passed"]]
    return {
        "total_days": len(results),
        "failed_days": sorted(failed_days),
        "passed": not failed_days,
        "by_day": results,
    }


def describe(summary: dict) -> str:
    lines = [
        f"Consistency sweep: {summary['total_days']} day(s), "
        f"{len(summary['failed_days'])} with issues",
    ]
    if summary["passed"]:
        lines.append("  All days passed internal consistency checks.")
    else:
        for day in summary["failed_days"]:
            result = summary["by_day"][day]
            lines.append(f"  {day} ({result['bars']}/{result['expected_bars']} bars):")
            lines.extend(f"    - {issue}" for issue in result["issues"])
    lines.append(
        "\nReminder: this checks internal plausibility only. Decoding is "
        "cross-validated against a live recording for 2026-07-29 and "
        "2026-07-30 alone -- treat other days as consistency-checked, not "
        "confirmed correct."
    )
    return "\n".join(lines)
