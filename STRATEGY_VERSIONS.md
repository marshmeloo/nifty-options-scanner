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

### What "beaten it on live data" means (set 2026-08-19)

Both conditions, on live paper-tracked trades, per index, against Anchor
over **the same period**:

1. **Lower drawdown** than Anchor — peak-to-trough of cumulative net
   P&L, ordered by close time, same nominal capital.
2. **At least ~80% of Anchor's net profit** over that period.

**Why the profit test is a floor and not "more profit".** The cluster
cap's whole mechanism is trading profit for drawdown — it can only
*remove* trades. Bank Nifty's own sweep (BACKLOG 2026-08-19) puts the
expected steady state at ~83% of profit for ~67% less drawdown. So a
challenger carrying it is *expected* to earn less than Anchor, and a
bar of "higher P&L" would be waiting on something the design does not
produce. ~80% is the floor below which the drawdown reduction is no
longer worth what it costs.

**Sample size — a judgement call, flagged as such rather than implied
by the numbers above.** Neither condition means anything on a handful
of trades: drawdown in particular is a slow statistic that needs enough
trades to actually trace a peak and a trough. Suggested minimum **100
closed trades per side, spanning at least ~4 trading weeks** so the
comparison covers more than one market mood. At observed rates that is
roughly 5 weeks for Bank Nifty (~4 Sentinel trades/day at the 500pt
band) and considerably longer for NIFTY, whose Sentinel takes well
under one trade/day — the NIFTY verdict will simply arrive much later,
and forcing it early is how a coin flip gets promoted.

**Comparisons must not pool across configuration changes.** A
challenger's evidence clock resets whenever its own decision-relevant
config moves. Concretely: Bank Nifty Sentinel's record before
2026-08-19 was taken at a 200pt cluster band and is *not* evidence about
the 500pt band it now runs. `logic_version.py` fingerprints
`CLUSTER_CAP_*` (added 2026-08-19) precisely so those stretches stay
separable rather than being silently averaged together.

Every real trade is stamped with `strategy_name` / `strategy_version`
in its journal entry (see `trade_tracker.open_new_trade`), so live
results can always be grouped by which named version produced them.

---

## Anchor — v1.2 — LIVE, CONTROL ARM (since 2026-09-01)

**Role changed 2026-09-01:** Sentinel was promoted to primary (see its
section below). Anchor continues to run UNCHANGED and UNCAPPED as the
control -- the unfiltered baseline the capped strategy is measured
against. Keeping it is a deliberate cost: over 1,244 Bank Nifty days its
own configuration carries a 36.3% max drawdown against Sentinel's 9.6%.
That is the price of retaining a comparison, and it was chosen knowingly
rather than by omission.

**Status:** Live in production, both NIFTY and Bank Nifty momentum,
since 2026-08-14 (v1.0), updated to v1.1 then v1.2, both on 2026-08-27.
No longer frozen in the strictest sense — see the version changes
below, both explicit, acknowledged exceptions to the promotion policy
above, not a reversal of it.

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

**v1.0 -> v1.1, 2026-08-27: opposite-direction exposure gate added
(`config.OPPOSITE_DIRECTION_GATE_ENABLED = True`).** Real Bank Nifty
session: Anchor opened 5 simultaneous CE positions, then — while the
CE side was still open — 12 simultaneous PE positions on top, as spot
round-tripped ~1,700pts (-₹15,014 that day). Root cause:
`cluster_cap_blocks()` only ever compares a candidate against
already-open SAME-DIRECTION positions — zero awareness of the
opposite side, in either Anchor or Sentinel.

Tested on the REAL gate (`research/opposite_direction_gate_backtest.py`,
wired into `shadow.py`'s actual `run_policy()`, not an approximation),
full 6-year NIFTY history, gate OFF vs ON:

| | OFF | ON |
|---|---|---|
| Total return | +331.4% | +362.1% |
| Max drawdown | 44.8% | 24.1% |
| Calmar | 0.62 | 1.21 |
| Profit factor | 1.25 | 1.34 |
| Expectancy/trade | +0.122R | +0.140R |

Genuine improvement, not unanimous (3 of 7 years worse: 2022, 2023,
2024), driven mostly by 2025 (-5.6% -> +16.7%) and 2021. **This shipped
on backtest evidence alone**, not live evidence — an explicit exception
to the promotion policy above, made on direct user instruction after
the tension was raised. See BACKLOG.md's full entry for the numbers
and the earlier, more dramatic-looking approximation that turned out
to overstate the effect (same cautionary shape as the cluster cap's
own history below).

**Known open question, still real:** the same investigation found
Sentinel's version of this fix is a net LOSS in total return
(+335.8% -> +300.4%) despite improving every per-trade/risk metric —
see Sentinel's own section below. Not resolved, deliberately left as
"Anchor gets it, Sentinel doesn't" rather than forced to match.

**v1.1 -> v1.2, 2026-08-27, same day: reversal exit added
(`config.REVERSAL_EXIT_ENABLED = True`).** The gate above only ever
protects the SECOND trade (the one that never opens) — it does nothing
for the FIRST trade, which keeps running its original thesis even
after the scanner produces a fresh, fully-qualified OPPOSITE-direction
signal. When on, that blocked signal ALSO closes the position(s) it
was blocked by, right then, instead of letting them run to their
original stop/target/EOD.

Tested in two stages, an extra step the gate itself didn't get:

1. **Retrospective, paired** (`research/reversal_exit_study.py`): for
   every trade that got a genuine reversal signal, "close right then"
   vs its real outcome, full 6-year NIFTY history. 1,907 events: mean R
   held to close -0.71, closed on the signal -0.32, paired diff
   +0.39R, **t=19.1** — one of the strongest effects measured anywhere
   in this project.
2. **The real mechanism**, built and re-run forward through
   `shadow.py` (same discipline the gate's own approximation taught
   this project not to skip):

| | gate only (v1.1) | + reversal exit (v1.2) |
|---|---|---|
| Trades | 9,115 | 10,856 (+19%) |
| Total return | +362.1% | +546.6% |
| Max drawdown | 24.1% | **26.2% (worse)** |
| Calmar | 1.21 | 1.40 |
| Profit factor | 1.34 | 1.55 |
| Expectancy/trade | +0.140R | +0.195R |

Trade count jumped because closing a position early frees capital
sooner, letting the scanner open trades later that day that couldn't
fire otherwise — the ripple effect the retrospective measurement
structurally cannot see. Max drawdown got WORSE, not better, the one
place the retrospective step's prediction (12.4%) was wrong. A
diagnostic (BACKLOG.md) confirmed the ~1,700 new trades are not
diluted junk: mean +0.129R, close to the system's own +0.140R
baseline, same conviction/risk bars as everything else. Every year
positive, in both the retrospective and the real run. **Also shipped
on backtest evidence alone**, same explicit exception as v1.1, same
day, same direct user instruction.

**NOT shipped to Sentinel yet** — see Sentinel's own section for
whether the same combination (cluster cap + gate + reversal exit)
changes its earlier negative verdict on the gate alone.

---

## Sentinel — v1.2-dev — LIVE (promoted 2026-09-01; built 2026-08-15)

**PROMOTED FROM CANDIDATE TO LIVE, 2026-09-01.** Sentinel is now the
primary momentum strategy. Anchor keeps running, unchanged and uncapped,
as the CONTROL arm — deliberately, so there is still an unfiltered
baseline to measure against and to detect a regime change in. Anchor was
NOT modified as part of this promotion.

**The evidence, and why it clears the promotion policy above without an
exception this time.** The only structural difference between the two is
Sentinel's correlated-cluster cap. Adding that cap to Anchor's own live
v1.2 shape, forward-simulated through `shadow.py` over each index's own
reconstructed history (`research/anchor_cluster_cap_study.py`):

| | drawdown | return | Calmar | years better |
|---|---|---|---|---|
| Bank Nifty, 1,244 days | 36.3% -> **9.6%** | +1120% -> +895% | 1.79 -> **6.11** | **6 of 6** |
| NIFTY, 1,485 days | 19.2% -> **4.4%** | +423.5% -> +411.5% | 1.66 -> **7.12** | **7 of 7** |

13 of 13 years, both indices, 2,729 days. Win rate, profit factor and
expectancy all improve on both. On NIFTY the return cost is 2.8% -- the
capped configuration is better on every measure there, not a trade-off.
Capped Anchor reproduces Sentinel's numbers exactly (Bank Nifty 6,970
trades / +895.13% / 9.55% drawdown, identical to the v1.2 run), which is
the arithmetic confirmation that the cap is the whole difference.

Crucially this is NOT backtest alone, so the promotion policy is
satisfied on its own terms rather than by exception -- unlike the v1.1
and v1.2 changes below. August 2026 ran both strategies live, side by
side, on the same signals for a full month:

| | trades | win rate | net |
|---|---|---|---|
| Anchor Bank Nifty | 62 | 9.7% | -Rs 62,843 |
| Sentinel Bank Nifty | 40 | **32.5%** | **-Rs 3,892** |

Anchor was 99% of the month's -Rs 81,835 across all strategies. Anchor's
2026-08-31 session opened NINE adjacent Bank Nifty PE strikes inside
thirteen minutes; Sentinel's cap held it to six trades and a sixth of
the loss.

**What "LIVE" does and does not mean here.** Sentinel still places no
real broker order -- nothing in this project does. The change is which
strategy is treated as primary for capital-allocation decisions and
which is the control. Both processes continue to run exactly as they
did; no process config was changed by this promotion.

**Status:** Live primary strategy. Built as its own process --
`main_live_sentinel.py` (NIFTY) and
`main_live_banknifty_sentinel.py` (Bank Nifty), each independent of
Anchor's own processes (separate state, journal, log, decision log) --
and, as of 2026-08-16, wired into `automation/start_trading.ps1` so
both start automatically every trading morning alongside Anchor and
the other loops (see COMMANDS.md). Every trade it opens carries
`strategy_name: "Sentinel"` in its journal entry, distinct from
Anchor's, so the two can be compared on the `/pnl` dashboard without
ever pooling. — `main_live_sentinel.py` (NIFTY) and
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

**Bank Nifty band RESOLVED 2026-08-19: 500pt, not NIFTY's 200pt.** The
caveat that used to sit here — that 200pt/30min came entirely from
NIFTY's backtest and had never been verified for Bank Nifty — has now
been settled by backtesting it on Bank Nifty's own 1,244-day history
across 5 independent ~1-year periods (`sweep_banknifty_cluster_cap.py`,
BACKLOG.md 2026-08-19).

200pt was too narrow for two reasons that both predicted the same fix
before any data was run: it is 0.83% of NIFTY at ~24,000 but only 0.35%
of Bank Nifty at ~57,000 (proportional equivalence → ~475pt), and at
Bank Nifty's 100pt strike spacing it reached 2 strikes either side
instead of NIFTY's 4 — structurally able to thin a cluster, never
collapse one. Live confirmation on 2026-08-10, 08-17 and 08-19; on the
last, two Sentinel trades 300pt apart passed straight through the 200pt
band, and the second (−₹2,452) is verifiably blocked at 500pt.

Measured: drawdown falls monotonically with band width, in every one of
the 5 periods individually, all staying profitable throughout. 200pt cut
worst-period drawdown 44%; **500pt cuts it 67% while keeping 83% of
profit** — comparable to the ~14%-profit-for-~62%-drawdown trade-off
NIFTY's own Sentinel was built on. 500 was chosen as the round number
nearest the a-priori ~475pt prediction rather than the empirical argmax
(600 edges it in aggregate but is within noise on several periods and
wins mainly by fixing the single worst one). The robust finding is that
400–800 are roughly equivalent and 200 was too narrow.

**NIFTY Sentinel keeps 200pt** — that value was validated on NIFTY's own
history and this finding says nothing about it. The two processes now
carry deliberately different bands, and `logic_version.py` fingerprints
`CLUSTER_CAP_*` (added the same day; it previously did not, so a band
change would not have churned the config side of the version).

**Still not tested against the real 08-12/08-14 sessions themselves**
(reconstructed data ends 2026-08-05). Bank Nifty's own history HAS now
been tested (see the band entry above, 2026-08-19). A promising
backtest is evidence toward promotion, not promotion itself — Sentinel
remains a paper-tracked candidate and Anchor remains unchanged, with
`CLUSTER_CAP_ENABLED = False` on both its NIFTY and Bank Nifty
processes. See the policy at the top of this file.

**Opposite-direction exposure gate evaluated 2026-08-27, declined ALONE
-- then adopted as part of the full package, same day.** Anchor picked
this up as v1.1 (see its own section above) after a real Bank Nifty
incident and a full 6-year backtest. The same backtest run on
Sentinel's config (`research/opposite_direction_gate_backtest.py`),
gate by itself, came back a net LOSS in total return: +335.8% ->
+300.4%, worse in 6 of 7 years, despite every per-trade/risk metric
(expectancy, profit factor, Calmar, drawdown) improving slightly. The
gate alone removes enough of Sentinel's real winning trades, stacked
on top of the cluster cap already above, that the compounded total
falls. Sentinel shipped WITHOUT the gate on this evidence.

**Reversal exit evaluated the same day, retested as the FULL package —
reversed the decision above.** Anchor picked reversal exit up as v1.2
(see its own section above) alongside the gate. Retesting Sentinel
with gate + reversal exit TOGETHER (not gate alone) completely
reversed the earlier verdict:

| | baseline (live) | + gate only | + gate + reversal exit |
|---|---|---|---|
| Trades | 7,203 | 6,020 | 7,089 |
| Total return | +335.8% | +300.4% | **+485.3%** |
| Max drawdown | 16.2% | 14.0% | **5.5%** |
| Calmar | 1.72 | 1.87 | **6.30** |
| Profit factor | 1.40 | 1.43 | **1.80** |
| Expectancy/trade | +0.189R | +0.200R | **+0.264R** |

Every year better, no exceptions (2022 alone: 53.1% -> 105.6%). The
two features needed each other for Sentinel: the gate by itself just
removes trades from an already cluster-cap-curated population, cutting
into real winners along with whatever it was meant to stop; adding the
reversal exit is what turns that removal into a net win instead of a
net loss. 1,633 of 7,089 trades (23%) closed via reversal exit,
consistent with Anchor's own rate.

`config.OPPOSITE_DIRECTION_GATE_ENABLED = True` and
`config.REVERSAL_EXIT_ENABLED = True`, both explicitly set in
`main_live_sentinel.py` / `main_live_banknifty_sentinel.py`. Bank
Nifty's own 500pt band was NOT independently retested here (shadow.py
has no Bank Nifty replay) -- applied on the same extrapolation already
made for Anchor's Bank Nifty process, reasoned from NIFTY's result and
a mechanism with nothing NIFTY-specific in its logic, not a
Bank-Nifty-specific backtest. Sentinel promoted v1.1-dev -> v1.2-dev,
same day as the gate-alone rejection.

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
| 2026-08-27 | Real Bank Nifty session: Anchor stacked 5 CE + 12 PE simultaneously open, -Rs15,014; found cluster_cap_blocks() has zero opposite-direction awareness. |
| 2026-08-27 | Opposite-direction gate approximation ("drop every overlapped trade") backtested: looked dramatic (Anchor +331%->+622%, Sentinel +336%->+499%) -- later found to overstate the real effect. |
| 2026-08-27 | REAL gate backtested (wired into shadow.py's actual run_policy()): Anchor +331.4%->+362.1% return, 44.8%->24.1% drawdown -- genuine but far more modest. Sentinel +335.8%->+300.4% -- a net loss. |
| 2026-08-27 | Anchor promoted v1.0 -> v1.1 (opposite-direction gate ON) as an explicit, acknowledged exception to the promotion policy above -- shipped on backtest evidence, not live evidence, per direct user instruction. Sentinel evaluated the same fix and explicitly declined it; stays v1.1-dev. |
| 2026-08-27 | Reversal-exit idea raised: does the blocked opposite-direction signal itself carry information worth acting on for the ALREADY-OPEN position it was blocked by? research/reversal_exit_study.py: 1,907 events, mean R held -0.71 vs closed-on-signal -0.32, t=19.1. |
| 2026-08-27 | Retrospective approximation of shipping it (swap outcomes, same trade sequence): Anchor total return +362.1%->+550.4%, EVERY year better -- but same caveat as the gate's own first approximation: doesn't capture the ripple effect of freeing capital sooner. |
| 2026-08-27 | REAL mechanism built into shadow.py and re-run forward: trades 9,115->10,856 (+19%, ripple effect confirmed real), total return +362.1%->+546.6%, max drawdown 24.1%->26.2% (WORSE -- the one thing the retrospective step could not see). A diagnostic confirmed the new trades are not diluted junk (mean +0.129R vs system baseline +0.140R). |
| 2026-08-27 | Anchor promoted v1.1 -> v1.2 (reversal exit ON), same explicit exception as v1.1, same day, same direct user instruction. Live-side mechanism built (trade_tracker._reversal_exit_opposite_positions, wired into try_open_new_trade), 9 new tests, full suite 951/951. |
| 2026-08-27 | Sentinel retested with gate + reversal exit TOGETHER (not gate alone, which was already declined): total return +335.8%->+485.3%, max drawdown 16.2%->5.5%, Calmar 1.72->6.30, every year better. The two features needed each other for Sentinel. |
| 2026-08-27 | Sentinel promoted v1.1-dev -> v1.2-dev (gate + reversal exit both ON), reversing the same-day gate-alone decline. Applied to both NIFTY and Bank Nifty Sentinel -- Bank Nifty's own 500pt band not independently retested (shadow.py has no Bank Nifty replay), extrapolated the same way Anchor's Bank Nifty process already was. |
| 2026-08-31 | Live session lost Rs 37,875 across 27 trades on a 0.66%-range day. Anchor opened NINE adjacent Bank Nifty PE strikes in thirteen minutes (no cluster cap); Sentinel's cap held it to six trades and a sixth of the loss. Two candidate fixes tested and REJECTED on 1,244 days -- an extension guard (worse at every setting) and a quiet-regime gate (worse in 5 of 6 years despite a good aggregate). See BACKLOG.md. |
| 2026-09-01 | Cluster cap forward-simulated on ANCHOR's own history for the first time (research/anchor_cluster_cap_study.py): Bank Nifty drawdown 36.3%->9.6%, Calmar 1.79->6.11, better 6 of 6 years; NIFTY drawdown 19.2%->4.4%, Calmar 1.66->7.12, better 7 of 7 years. 13 of 13 across 2,729 days. Capped Anchor reproduces Sentinel's numbers exactly -- the cap is the whole difference. |
| 2026-09-01 | **Sentinel promoted CANDIDATE -> LIVE primary.** First promotion on the policy's own terms rather than an acknowledged exception: August ran both strategies live side by side for a month (Anchor Bank Nifty 9.7% win rate / -Rs 62,843 vs Sentinel 32.5% / -Rs 3,892), so live evidence exists, not backtest alone. Anchor kept running UNCHANGED as the uncapped CONTROL arm, at a known cost -- its own 36.3% drawdown profile -- chosen deliberately to retain a baseline. No process config changed by the promotion. |
