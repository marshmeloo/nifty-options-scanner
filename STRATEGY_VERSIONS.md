# Strategy Versions

A named, human-readable registry of momentum strategy configurations —
separate from `logic_version.py`'s hash fingerprint, which changes
automatically whenever any decision-relevant setting does. This file is
the opposite: **stable names that don't move**, so live results stay
attributable to a known, fixed configuration even while candidates are
tested elsewhere.

Applies to both NIFTY (`main_live.py`) and Bank Nifty
(`main_live_banknifty.py`) momentum, since both share `config.py`.

## Promotion policy

**A version listed here as "Live" does not change** — not
`BREAKEVEN_ARM_R`, not `DEFAULT_TARGET_RR`, not any other
decision-relevant constant it covers — without either:

1. **Solid evidence from real live trading** that this specific version
   has a real problem (not a backtest concern — an actual pattern seen
   in real paper-tracked trades), or
2. **A challenger version that has beaten it on live data**, not
   backtest performance alone.

A promising backtest is *not* sufficient on its own to promote a
challenger or retire the incumbent. See the 2026-08-15
correlated-cluster-cap episode below for exactly why: a cap that looked
like a clean drawdown reduction in a 6-year backtest turned out, on
closer inspection, to be cutting a large amount of completely ordinary
trading along with the specific pattern it targeted — the backtest
alone made it look better than it was.

Every real trade is stamped with `strategy_name` / `strategy_version`
in its journal entry (see `trade_tracker.open_new_trade`), so live
results can always be grouped by which named version produced them.

---

## Anchor — v1.0 — LIVE

**Status:** Live in production, both NIFTY and Bank Nifty momentum,
since 2026-08-14. **Frozen** — protected by the promotion policy above.

**What it is:** 2R target, ATR-derived stop, breakeven stop armed at
+0.5R (`config.BREAKEVEN_ARM_R = 0.5`), forced end-of-day close. Every
armed-and-closed contract is watched forward to its original 2R
target/stop by a separate shadow-tracking system with no further P&L
impact, purely to keep gathering live evidence on whether 0.5R is the
right threshold.

**Backtest evidence** (fresh reruns, real statutory + measured spread
costs, 2026-08-15): NIFTY 10,660 trades / +₹36,79,201 / 16.75% max
drawdown over 2020-08 to 2026-08; Bank Nifty 8,324 trades /
+₹45,73,883 / 40.9% max drawdown over 2021-08 to 2026-08. Full detail
in `research/nifty_momentum_breakeven_0.5R.html` and
`research/banknifty_momentum_breakeven_0.5R.html`.

**Known open question:** correlated same-direction clusters on adjacent
strikes (NIFTY 2026-08-12, 23 trades in three bursts; Bank Nifty
2026-08-14, 6 adjacent PE strikes) are not risk-capped by
`MAX_TOTAL_EXPOSURE_PCT`, which only sums risk rupees and cannot tell a
diversified position from the same bet repeated. This is exactly what
Sentinel (below) is investigating a fix for.

---

## Sentinel — v1.1-dev — CANDIDATE, LIVE PAPER-TRACKING (built 2026-08-15)

**Status:** Backtested (below), built as its own live paper-tracking
process — `main_live_sentinel.py` (NIFTY) and
`main_live_banknifty_sentinel.py` (Bank Nifty), each independent of
Anchor's own processes (separate state, journal, log, decision log) —
and, as of 2026-08-16, wired into `automation/start_trading.ps1` so
both start automatically every trading morning alongside Anchor and
the other loops (see COMMANDS.md). Every trade it opens carries
`strategy_name: "Sentinel"` in its journal entry, distinct from
Anchor's, so the two can be compared on the `/pnl` dashboard without
ever pooling.

**Anchor is untouched by this build.** Both new files patch the same
shared `config`/`trade_tracker` modules Anchor's processes import, but
only within their OWN separate OS process — confirmed directly:
importing `main_live.py` and `main_live_banknifty.py` in isolation
still shows `STRATEGY_NAME = "Anchor"` and `CLUSTER_CAP_ENABLED =
False`, unaffected by Sentinel's files existing at all.

**What it changes vs. Anchor:** adds a time-windowed correlated-cluster
cap on top of Anchor's exact same breakeven@0.5R exit logic — reject a
new signal if a same-direction position opened *recently* (within a
configurable window) on a nearby strike. Nothing about the exit rule
itself changes; this only affects which entries are taken.

**Why a time window, specifically:** the first two designs tried
(`max_open_per_direction`, plain `strike_adjacency_band_points`, both
in `shadow.py`, 2026-08-15) had no time component — a position blocked
new same-direction entries for its *entire* open lifetime, which for an
EOD-outcome trade can be hours. Backtested across the full NIFTY
history, both cut profit 30-54% for a 71-75% drawdown reduction —
correct direction, wrong magnitude, because they were blocking a lot of
ordinary trading, not just the specific rapid-fire pattern. See the
2026-08-15 conversation log / `shadow.py` git history for the full
sweep. A window should target the actual pattern (the same signal
firing again within minutes) without touching unrelated same-direction
trades hours apart.

**Status of testing:** time-window sweep run 2026-08-15, full NIFTY
history (2020-08 to 2026-08), real costs, `strike_adjacency_band_points
= 200`, breakeven@0.5R -- same methodology as Anchor's own backtest:

| Config | Trades | Net P&L | Max drawdown | vs. Anchor (profit / drawdown) |
|---|---|---|---|---|
| Anchor v1.0 (no cap) | 10,660 | +₹36,79,200 | 16.75% | — |
| Untimed cap (no window) | 6,049 | +₹25,86,343 | 4.50% | -30% / -73% |
| Sentinel, 15-min window | 8,868 | +₹33,27,158 | 10.69% | **-10% / -36%** |
| Sentinel, 30-min window | 7,713 | +₹31,49,585 | 6.34% | **-14% / -62%** |
| Sentinel, 60-min window | 6,929 | +₹29,22,207 | 6.19% | -21% / -63% |

The window works as intended: the 30-minute version gives up only 14%
of Anchor's profit while cutting drawdown by 62% — a far better trade
than the untimed version's 30%-for-73%. 60 minutes is strictly worse
than 30 (lower profit, no real further drawdown improvement), so there
is little reason to go wider than 30-45 minutes. 15 minutes is the
gentlest but may not fully cover slower-forming bursts (the real
Bank Nifty 08-14 cluster spanned close to an hour end to end, even
though most of it fired within 3 minutes).

**Bank Nifty caveat, stated plainly:** the 200pt/30min values above
come entirely from NIFTY's own backtest. NIFTY strikes are 50pt apart;
Bank Nifty's are 100pt apart, and Bank Nifty's own trade profile is
already known to differ substantially (69.6% EOD-close trades vs
NIFTY's 30.7%, per the Bank Nifty research note). Running the same
values on Bank Nifty's Sentinel process is a reasonable first pass,
not a claim that they've been separately verified for it.

**Still not deployed, still not tested against the real 08-12/08-14
sessions themselves** (reconstructed data ends 2026-08-05) or against
Bank Nifty's own history. A promising backtest is evidence toward
promotion, not promotion itself — see the policy at the top of this
file.

---

## Version history

| Date | Event |
|---|---|
| 2026-08-14 | Anchor v1.0 deployed live (NIFTY + Bank Nifty momentum) |
| 2026-08-15 | Correlated-cluster pattern found in real NIFTY (08-12) and Bank Nifty (08-14) sessions |
| 2026-08-15 | First two cap designs backtested, found too blunt (see Sentinel above) |
| 2026-08-15 | This registry created; Anchor formally named and frozen; Sentinel v1.1-dev (time-windowed) opened for testing |
| 2026-08-15 | Sentinel v1.1-dev time-window sweep complete: 30-min window gives -14% profit for -62% drawdown vs Anchor -- best trade-off found so far, not yet promoted |
| 2026-08-15 | Sentinel v1.1-dev built as its own live paper-tracking process (NIFTY + Bank Nifty), 200pt/30min. Anchor confirmed unaffected in isolation. Not yet started. |
| 2026-08-16 | Sentinel v1.1-dev wired into `automation/start_trading.ps1` (both NIFTY and Bank Nifty) -- now starts automatically every trading morning alongside Anchor. Anchor's own two entries in the script left byte-for-byte unchanged. |
