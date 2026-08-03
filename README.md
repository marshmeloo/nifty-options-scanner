# NIFTY Options Scanner & Trade Tracker

A decision-support pipeline for NIFTY options: scans the live option chain,
flags setups, builds a trade plan, runs it through a risk check, and tracks
outcomes over time. **It is analytics-only — it never places an order.**

Pipeline: `SCAN -> SIGNALS -> PLAN -> RISK -> DECISION`

## What it does

- Pulls a live NIFTY option chain and intraday candles from the Dhan API
  (`dhan_source.py`), polling every 30 seconds during market hours.
- Scans the chain for setups using OI buildup, IV percentile, PCR, VWAP
  deviation, and price-action structure (order blocks, FVGs, support/
  resistance, liquidity sweeps) — see `scanner.py` and `price_action.py`.
- Classifies OI + price moves into long buildup / short covering / short
  buildup / long unwinding to read whether buyers or writers are behind a
  move (`config.py` / `dhan_source.py`).
- Builds a concrete trade plan (entry, target, stop, lot size, invalidation)
  in `plan_generator.py`.
- Runs every plan through a risk checker (`risk_checker.py`) covering
  per-trade risk %, total exposure, and a daily-loss circuit breaker.
- Tracks every tracked trade to its actual outcome in a JSONL journal
  (`trade_tracker.py`), and uses recent outcomes to adjust future scoring
  by tag win-rate.

## Files

| File | Purpose |
|---|---|
| `main.py` | Runs the pipeline once against a CSV snapshot (`sample_data.csv`) |
| `main_live.py` | Live polling loop against the real Dhan API; logs every session |
| `scanner.py` | Core scan logic, market bias, and setup scoring |
| `plan_generator.py` | Turns a flagged setup into a concrete trade plan |
| `risk_checker.py` | Approves/rejects a plan against risk rules |
| `price_action.py` | Structure detection: swings, OB, FVG, S/R, sweeps, trend, momentum |
| `trade_tracker.py` | Journals tracked trades and their outcomes |
| `dhan_source.py` | Dhan API client: option chain, snapshot, intraday candles |
| `nse_source.py` | Fallback: NSE's public option-chain API (full chain, no Greeks) |
| `tradingview_source.py` | Last-resort fallback: spot + candles only (no option chain exists on TradingView) |
| `resilient_source.py` | Orchestrates the Dhan -> NSE -> TradingView fallback; `main_live.py` imports from here |
| `oi_analytics.py` | Chain-wide OI reads: Max Pain, call/put OI walls, net delta OI |
| `trade_staging.py` | Approval-gate placeholder for future order execution ("Trading as Git" pattern) -- not wired in yet |
| `approve_orders.py` | Interactive CLI to review/approve/reject staged orders |
| `premarket.py` | Pre-market brief: previous session recap, projected levels, global cues, FII/DII, expiry/event flags |
| `global_cues.py` | Overnight US/crude/USD-INR/India VIX cues (free, unauthenticated Yahoo endpoint) |
| `news_source.py` | RSS-based news fetch + keyword tagging into an event-risk level ("elevated"/"normal") |
| `banknifty_context.py` | Bank Nifty spot/trend + a divergence flag against NIFTY's own move (context only, not traded) |
| `participant_oi.py` | NSE's daily FII/DII/Pro/Client positioning in index options -- the actual "smart money" data |
| `volume_profile.py` | Volume-at-price distribution from OHLCV bars: Point of Control, Value Area, HVN/LVN |
| `anchored_vwap.py` | True VWAP anchored from session open, recent swing high, recent swing low |
| `opening_gap.py` | Captures NIFTY + Bank Nifty opening gap (points/%) once per day |
| `decision_log.py` | Full per-cycle audit trail: every candidate considered, adjusted score, exact rejection reason -- not just the winner |
| `session_summary.py` | End-of-day digest -- the one compact file to hand to a fresh Claude session, not the raw logs |
| `watchdog.py` | Standalone monitor: warns if main_live.py's log goes stale during market hours (run in a separate terminal) |
| `supervisor.py` | Run this INSTEAD of main_live.py directly -- auto-restarts it on crash or freeze |
| `atomic_state.py` | Shared atomic JSON-write helper used by every state file, so a forced kill can never corrupt one |
| `dashboard_server.py` | Local read-only live dashboard server (stdlib only, no new dependency) |
| `dashboard/live_dashboard.html` | The live dashboard UI itself -- spot/OI/bias/Bank Nifty/news, open trades, condor position, decision activity |
| `config_condor.py` | Dedicated config for the (separate) iron condor strategy |
| `condor_scanner.py` | Finds short CE/PE strikes by premium band + their hedge legs |
| `condor_plan_generator.py` | Prices the 4-leg condor: net credit, max profit/loss, breakevens |
| `condor_risk_checker.py` | Concurrency/credit/capital-at-risk gate for opening a condor |
| `condor_tracker.py` | Mark-to-market, breach-warning staging, expiry settlement |
| `main_condor.py` | Standalone live loop for the condor strategy -- run alongside main_live.py |
| `open_approved_condor.py` | Opens a condor exactly as staged + approved (see below) |
| `config_directional_spread.py` | Dedicated config for the (separate) directional credit-spread strategy |
| `directional_spread_scanner.py` | Picks a side (PE/CE) from market bias, finds the short strike + hedge leg |
| `directional_spread_plan_generator.py` | Prices the 2-leg spread: net credit, max profit/loss, breakeven |
| `directional_spread_risk_checker.py` | Concurrency/daily-cap/credit/capital-at-risk gate for opening a spread |
| `directional_spread_tracker.py` | Mark-to-market, profit-target/stop-loss auto-exit, breach staging, expiry settlement |
| `main_directional_spread.py` | Standalone live loop for the directional spread strategy -- run alongside main_live.py |
| `open_approved_directional_spread.py` | Opens a directional spread exactly as staged + approved |
| `data_source.py` | CSV-based snapshot loader (offline/testing) |
| `models.py` | Shared dataclasses (snapshot, setup, plan, verdict) |
| `config.py` | Every threshold and risk parameter — tune to your own setup |
| `tests/` | Regression tests pinning the 2026-07-28 scoring/risk/OI-freshness fixes (`python -m pytest tests/ -q`) |
| `dedupe_journal.py` | One-off repair: removes duplicate `trade_journal.jsonl` entries left by the pre-fix recovery bug (dry-run by default, backs up before writing) |
| `snapshot_recorder.py` | Records the raw chain + candles behind every cycle, so any future logic version can be replayed against identical history |
| `replay.py` | Re-runs the pipeline over recorded history; diffs two logic versions; forward-return evaluation of every candidate |
| `logic_version.py` | Fingerprints the decision-relevant config + git SHA so results are never pooled across versions |
| `costs.py` | Round-trip transaction costs (brokerage/STT/exchange/GST/stamp), applied at trade close so P&L is net as well as gross |
| `market_regime.py` | Today's range vs a trailing 6-month distribution -- is this even a normal trading day? |
| `shadow.py` | Simulates any trading policy (score bar, R:R, time window, stop rules) against recorded history -- answers "how would this have done?" without capital |
| `workspace.py` | Declares whether a checkout is production or development, and warns when that's violated |
| `sync_from_prod.py` | One-way copy of recorded data production → development |

## Setup

```bash
git clone <this-repo-url>
cd nifty-options-scanner
pip install -r requirements.txt
```

### Live mode (real Dhan data)

**Recommended: run via `supervisor.py`, not `main_live.py` directly** --
it auto-restarts on crash or freeze (see "Fixed: 2026-07-24" below for
why this matters; it's not optional hardening, it's the difference
between a multi-hour blind spot and a few-seconds one):

```bash
export DHAN_CLIENT_ID="your-client-id"
export DHAN_ACCESS_TOKEN="your-jwt-access-token"
python3 supervisor.py
```

Windows:
```cmd
set DHAN_CLIENT_ID=...
set DHAN_ACCESS_TOKEN=...
python3 supervisor.py
```

If you'd rather run `main_live.py` directly without the supervisor
(e.g. while debugging), that still works exactly as before -- just know
you're on your own for noticing if it goes silent. `watchdog.py`, run in
a separate terminal, gives you at least a warning in that case.

### Offline / test mode (no API key needed)

```bash
python3 main.py
```
Runs the same pipeline against `sample_data.csv` (not included in this repo —
supply your own CSV with the expected columns, see `data_source.py`).

## Configuration

All thresholds live in `config.py` — capital, risk %, lot size, IV/OI/PCR
thresholds, price-action tolerances, and trade-tracking rules. Current
live values:

- `NIFTY_LOT_SIZE = 65`
- `MAX_LOTS_PER_TRADE = 1`
- `MAX_NEW_TRADES_PER_DAY` — effectively uncapped (training/evaluation phase)

## OI analytics ("where is smart money positioned")

`oi_analytics.py` runs on every snapshot (Dhan, NSE, or CSV) and adds a
chain-wide read, separate from the per-strike buildup classification:

- **Max Pain** — the strike where option writers collectively lose the
  least at expiry, and how far spot currently sits from it.
- **Call wall / put wall** — the single strikes with the largest CE / PE
  OI, which tend to act as resistance / support.
- **Net delta OI** — today's fresh call-side OI minus fresh put-side OI
  across the whole chain, with a bullish/bearish/neutral read.
- **OI concentration table** — top strikes by combined CE+PE OI.

All of it is on `snapshot.oi_analysis`, and `main_live.py` logs it every
cycle.

## Fixed: 2026-07-22 -- open trades going untrackable ("current ?")

A live session on 2026-07-22 showed a tracked trade (24000 PE) stuck at
`current ?` for the whole day and force-closed at entry price (flat
0.0%), even though it had genuinely traded up to 192 intraday (confirmed
against a manually-tracked chart). Root cause: `PREMIUM_MIN`/`PREMIUM_MAX`
was being applied when the option chain snapshot was *built*
(`dhan_source.py`/`nse_source.py`), not just when picking new candidates.
The instant an already-open trade's premium moved outside that band --
completely normal as a position runs toward its target -- its quote
silently disappeared from every subsequent snapshot, making it
permanently untrackable, and this was also quietly trimming strikes out
of the OI analytics (Max Pain/PCR need every strike, not just the
tradeable-premium slice).

Fixed by moving the premium filter to `scanner.py` (candidate-selection
time only); the chain-building sources now always return the full chain
within `STRIKE_RANGE_POINTS`. Also fixed a related issue where the
end-of-day settlement re-fetched a brand-new snapshot right at market
close (which can come back thin/stale) instead of reusing the last
snapshot confirmed while the market was still open -- and added an
explicit `exit_price_estimated` flag + journal note for the rare case a
quote genuinely can't be found at close, instead of silently reporting
a misleading flat 0% outcome.

## P&L in rupees, not just percent

Every P&L figure (live tracking, trade close, EOD close, and the
dashboard) now shows rupee P&L (`pnl_inr` / `running_pnl_inr`) alongside
the percentage, computed as `(price move) * NIFTY_LOT_SIZE * lots` --
percentage alone doesn't tell you what a move was actually worth.

## Pre-market brief

Run before 9:15 IST to get a written plan for the day instead of walking
into the open cold:

```bash
python3 premarket.py
```

Combines the previous session's recap, structural levels projected from
recent daily candles (reusing `price_action.py`), overnight global cues
(US index closes, crude, USD/INR, India VIX -- see `global_cues.py` for
why this isn't GIFT Nifty and what to swap in if you get a real feed),
the previous session's FII/DII net flow, whether today is an expiry day
(computed from the actual expiry date, not a hardcoded weekday --
NSE has moved NIFTY's weekly expiry more than once), and any event you've
flagged in `config.KNOWN_EVENT_DATES` (RBI/Budget/Fed -- you maintain
this list, there's no free clean API for it). Everything rolls up into
one synthesized "lean," explicitly framed as a starting point rather
than a trade signal. Output goes to `logs/premarket_brief_YYYYMMDD.md`
and prints to console.

## Anchored VWAP

`anchored_vwap.py` computes VWAP starting from a specific meaningful
reference point, not just "since session open." Institutions often use
anchored VWAP as their own execution benchmark -- e.g. "what's our
average cost since the last major swing low" -- so price reacting at an
anchored level can reflect where a large participant's average entry
sits, in a way a single rolling session VWAP can't show.

This is also a genuine upgrade over the existing VWAP: `dhan_source.py`'s
`vwap` field is an incrementally-updated PROXY (built tick-by-tick as the
session runs, since Dhan doesn't hand us a ready-made VWAP), not a true
volume-weighted calculation from the full candle series. `anchored_vwap.py`
computes the real thing directly from OHLCV bars using the standard
typical-price formula `(high + low + close) / 3`, weighted by volume.

Three anchors, all derivable from the same intraday candles main_live.py
already fetches (no extra API call):
- **Session Open** -- VWAP since today's open
- **Recent Swing High** -- VWAP since the most recent significant swing
  high (via `price_action.find_swing_points`)
- **Recent Swing Low** -- VWAP since the most recent significant swing low

For each anchor: the VWAP value, and whether current price sits above,
below, or at it. Verified the core math against hand-calculated
known-answer cases (constant price, a two-tier price/volume mix, and
confirming an anchor index correctly excludes candles before it) before
testing the full swing-detection integration on a realistic session with
a clear peak and trough.

Logged every cycle in `main_live.py`, included in every `decision_log.py`
record, and shown as its own card on the live dashboard (kept separate
from the OI-based Strike Landscape, since VWAP anchoring is a
price-action/execution concept, not an OI-structure one). Not currently
wired into `premarket.py` -- anchored VWAP is inherently a live,
intraday-unfolding concept; a static previous-day recap doesn't gain much
from it that the existing OHLC + volume profile don't already cover.

## Volume profile (Point of Control, Value Area, HVN/LVN)

`volume_profile.py` builds a distribution of volume AT EACH PRICE LEVEL
(as opposed to `price_action.py`'s `volume_ratio`, which is volume over
*time*). Reveals high-volume nodes (HVN -- price areas with real
"acceptance," often where big positions were built or unwound) and
low-volume nodes (LVN -- price skipped through quickly, often
rejection/imbalance).

**Honesty note on the method**: a true volume profile is built from
tick-by-tick trade prints. Dhan's REST candle endpoint only gives OHLCV
bars, not a trade tape, so this uses the standard approximation for
bar-based data -- each candle's volume is spread evenly across every
price bin between its low and high. Coarser than a tick-built profile,
but the accepted practice when tick data isn't available; resolution is
governed by `config.VOLUME_PROFILE_BIN_POINTS` (10 points by default),
not by pretending the approximation isn't there.

Computes:
- **POC** (Point of Control) -- the single price with the most volume
- **Value Area** (VAH/VAL) -- the range containing ~70% of volume,
  built outward from the POC
- **HVN/LVN** -- bins well above/below average volume

Wired in three places: `main_live.py` logs it every cycle, reusing the
SAME candles already fetched for `price_action.py` (no extra API call);
`decision_log.py` includes it in every cycle's audit record; the live
dashboard shows POC as a 5th landmark on the Strike Landscape (same
NIFTY-point scale as spot/max pain/walls) plus a dedicated card.
`premarket.py`'s brief also gets a coarser (hourly-bar) previous-session
version. Tested against a synthetic session with a deliberate
consolidation zone and fast-move zones -- POC and Value Area correctly
landed in the consolidation zone, HVN/LVN correctly separated it from
the fast-move zones; all edge cases (empty candles, zero volume,
invalid high<low, single candle) handled without crashing.

## Following big money: Participant-wise OI

`participant_oi.py` pulls NSE's daily Participant-wise Open Interest
report (`https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv`)
-- the actual data on how FII (foreign institutions), DII (domestic
institutions), Pro (proprietary desks), and Client (retail) are
positioned in **index options specifically**. This is different from
two things already in the codebase:
- `oi_analytics.py`'s Max Pain/walls are aggregate OI across *everyone*
  combined -- a proxy for where positioning is concentrated, not who's
  behind it.
- `nse_source.get_fii_dii_activity()` is FII/DII **cash market** flow
  (equity buying/selling), not their derivatives positioning.

Distills FII+Pro's net call vs. net put positioning into a
bullish/bearish/neutral lean (the common "FII/Pro as smart money, Client
as the other side of that trade" heuristic -- real, but a lean, not a
rule; every contract has to have a counterparty, so these positions
mechanically offset). DII is reported but excluded from the lean itself
-- regulation mostly restricts domestic institutions to hedging in
derivatives, so their options OI is structurally small and not very
informative here.

Published once daily after market close, so this is **pre-market
context** (`premarket.py`'s brief), not something polled during the live
session -- it now counts as a genuine vote in the day's synthesized
bias, alongside global cues, chain net-delta-OI, and trend.

Tested the CSV parsing against NSE's actual published format (verified
column names/shape against a real historical report) and the
lookback-walking logic (which tries today, then walks backward through
weekends/holidays to find the most recently published report) against
both a delayed-success and a total-failure case.

## End-of-day digest for reviewing with Claude

Run once the market's closed:

```bash
python3 session_summary.py
```

Writes `logs/session_summary_YYYYMMDD.md` -- **hand this file to a
fresh Claude conversation or Claude Code session, not the raw logs.**
`logs/nifty_scan_*.log` repeats OI/bias context every single cycle
(noisy), and `logs/decision_log.jsonl` has one record per cycle
(hundreds per day) -- both are expensive to feed a fresh conversation
for what you actually want reviewed. This pulls just the high-signal
parts into one small file:

- Every trade closed today (outcome, P&L, capture efficiency, lesson)
- **Near-miss candidates** -- setups rejected after the learned
  adjustment but within 1 point of the conviction bar, exactly the ones
  worth a human look if the bar or penalty ever seems miscalibrated
- **Operational health** -- gaps in the scan log longer than 3 minutes
  during market hours (the same evidence that caught the 2026-07-24
  freeze incident, found automatically instead of by manual log
  archaeology) plus any WARNING/error lines
- End-of-day context: spot, bias, opening gap, news risk

Includes a ready-to-use prompt at the bottom of the output for handing
it to a new session. Tested against seeded data reproducing a deliberate
freeze gap and a genuine near-miss candidate -- both correctly detected;
also verified it doesn't crash when no logs exist at all.

## Fast position check (added 2026-07-27)

A real gap: with only a 30s scan cycle, a trade could spike through its
target and back down between two snapshots -- neither ever sees the
touch, so a real target hit just gets missed. Fixed by interleaving a
much lighter check every `config.FAST_CHECK_INTERVAL_SECONDS` (5s
default) between full 30s scan cycles: it re-checks already-open
trades' target/stop only (no new-candidate scanning, no OI analytics,
no news/Bank Nifty/volume-profile computation), and skips the fetch
entirely when nothing's open. Tested directly against the exact
spike-through-target scenario (price briefly touches above target
between two checks, then drops) -- caught and closed correctly, where a
30s-only cadence would have missed it. Also verified the interleaving
timing itself (full cycles firing on schedule, ~5 fast checks in
between, at an accelerated interval for a fast test).

**Currently still re-fetches the full option chain** for the fast
check, not a lightweight per-contract quote -- see `BACKLOG.md` for the
plan to switch to Dhan's dedicated `marketfeed/ltp` endpoint before
relying on this with real capital.

## Fixed: 2026-07-28 -- same strike entered 3x, and a dead circuit breaker

A session opened the **same contract (24050 PE) three times at
effectively the same premium** (75.7 / 76.7 / 76.7), stopping out twice
and closing the third at EOD, for a combined Rs -4,017. The first
question was whether the system had followed its own rules. It had --
`try_open_new_trade()` only ever blocked a strike while a trade on it was
*currently open*, so once a trade closed the strike was immediately
eligible again. But investigating why it kept re-qualifying turned up
several independent defects, listed here worst-first.

**1. The daily-loss circuit breaker and total-exposure cap were dead
code.** `main_live.py` passed a hardcoded `current_daily_loss_pct=0.0`
and `current_open_exposure_pct=0.0` into `risk_checker.check()` and never
updated them, so `MAX_DAILY_LOSS_PCT` could not trip no matter how much a
day lost, and `MAX_TOTAL_EXPOSURE_PCT` was never enforced. Both are
advertised in this README as active risk controls; neither was. Every
input needed was already on disk -- today's journal entries for realized
P&L, `state/open_trades.json` for unrealized and for capital at risk --
it just was never read back. Now computed every cycle by
`trade_tracker.compute_risk_state()` and logged as a "Risk state" line so
it's visible rather than implicit. It reflects trades THIS TOOL tracked,
not your broker account.

**2. The structure score was a one-way ratchet.** `price_action.analyze()`
returned every FVG / order block / breakout ever detected in the candle
series and never pruned them, while `scanner.py` added a flat +0.5 per
nearby level with no cap. So a strike's score could only climb as a
session went on. Measured from the real decision log for 24050 PE that
day: 0 levels at 09:16, 14 by 14:00, **never once decreasing**, with the
raw score going 6.0 -> 11.0 almost entirely on that count. That is why
the setup re-qualified at *higher* conviction after failing twice. Fixed
in two parts: FVGs are now dropped once price trades back through them
(standard mitigation), and one-off events age out after
`LEVEL_MAX_AGE_CANDLES`. Support/resistance is deliberately exempt --
it's defined by repeated touches over time. On a simulated choppy
session this took the level count from a monotonic 2->29 down to a
non-monotonic 0-4.

**3. Structure scoring ignored direction, and rewarded contradiction.**
Every nearby level added a *positive* score regardless of which way it
pointed, so a **bullish** FVG raised conviction on a **PE**. And because
overlapping bull and bear levels both scored positive, a chopping market
that printed both across the same zone read as double confluence when it
means the opposite. The real entry listed both `Bullish FVG
(24013.4-24015.2)` and `Bearish FVG (24014.0-24015.2)`, plus support
*and* resistance on the same strike -- four separate bonuses for
indecision. Levels are now scored by directional agreement with the
contract, opposing levels net against each other, and the total is capped
by `MAX_LEVEL_SCORE_CONTRIBUTION` so confluence *count* can't dominate.

**4. Rich and cheap IV both scored +1.0.** This scanner only ever *buys*
premium, so those aren't symmetric -- buying expensive volatility means
vega works against you and the move has to be bigger just to break even.
Now `IV_CHEAP_SCORE` / `IV_RICH_SCORE`, the latter negative.

**5. Plan geometry took no account of the instrument.** A flat -30% stop
and +60% target meant that on this contract (real intraday range
51.15-102.55) the target sat at 122.72, about **20 points above the
highest price it traded all day** -- unreachable from the moment it was
set -- while the stop at 53.69 sat *inside* the option's normal
oscillation. Stop inside the noise, target outside the range. Stop
distance is now derived from `ATR x |delta| x STOP_ATR_MULTIPLE` (ATR in
NIFTY points, delta converting that into premium points), clamped into a
`MIN_STOP_PCT`-`MAX_STOP_PCT` band, with the flat percentage kept as a
fallback for the NSE tier, which returns no Greeks. Each plan records a
`stop_basis` string so the geometry is auditable afterward.

**6. Nothing knew it was expiry day.** All four trades were same-day
expiry *long* options -- almost pure extrinsic value decaying to zero by
15:30 -- and the last was opened at 14:32. Same-day expiry now raises the
conviction bar by `EXPIRY_DAY_EXTRA_CONVICTION` and blocks new entries
after `EXPIRY_DAY_NO_NEW_TRADES_AFTER`.

**7. Re-entry gate.** A blanket post-stop time cooldown was implemented
first and then **rejected**: on the real data a 60-minute window caught
only one of the two re-entries (the other came 2h31m after its stop), and
it would also wrongly block a genuinely different setup just because the
clock hadn't run out. What actually went wrong was re-running an
*identical failed plan* -- same entry price means the same stop and the
same target. `is_repeat_of_stopped_plan()` now blocks a new entry when a
trade on that strike+type already stopped out today within
`REENTRY_PRICE_TOLERANCE_PCT` of the new entry. Catches both real
re-entries; a materially different premium passes through.

**8. OI buildup was a day-scale read driving 30-second decisions.**
`oi_change_pct` is measured against Dhan's `previous_oi` -- the *previous
session's* closing OI -- and the premium baseline is likewise previous
close or first-seen-today. Both only ever grow as a session runs, so
`buildup_type` was effectively pinned for the day. The three real entries
were classified `long_buildup` at 10:21 (OI +112.2%), 13:14 (+338.2%) and
14:32 (+217.7%): three different numbers, three different times, one
identical verdict, across completely different intraday price action --
because OI versus *yesterday* had of course risen in all three cases.
Worse, `scanner.py`'s magnitude multiplier saturates its 3.0 cap at any
day-cumulative move past ~45%, so it carried no information either.

`dhan_source.py` now keeps a short rolling per-contract history
(`state/oi_history.json`, reset daily, sample spacing and window in
config) and classifies buildup on the change over the last
`OI_INTRADAY_LOOKBACK_MINUTES` instead. The day-cumulative figure is
retained alongside it and still shown in the reason string, because
`oi_analytics.py` genuinely needs it to derive "OI added today" and "OI
is 3x yesterday" is useful context in its own right. When there isn't
enough history yet -- the session's first minutes, or the NSE/CSV
sources, which don't track it -- the intraday fields are `None` and the
old daily behaviour is used, labelled as such in the reason rather than
passed off as a fresh read. Simulated at the real 30s cadence against
the 2026-07-28 OI shape: the morning ramp still classifies as
`long_buildup`, and then from ~60 minutes in the intraday signal
correctly goes **silent** while the daily read stays `long_buildup` for
the rest of the day -- meaning both the 13:14 and 14:32 entries would
have had no buildup signal at all.

**9. Interrupted-session recovery journalled trades twice.**
`settle_stale_trades()` journalled every stale trade but never cleared
`state["trades"]`, leaving that to the caller -- unlike
`force_close_end_of_day()`, which has always cleared it itself. Anything
interrupting the process between journalling and the caller's save left
those trades still marked OPEN on disk, so the next start recovered and
journalled them **again**. That really happened: 8 trades from the
2026-07-23 session were journalled twice on 2026-07-24, once at 00:39:42
(market shut, no quote available, `exit_price_estimated: true`) and again
at 07:28:46 (fresh quote, slightly different exit prices), duplicating
Rs -1,976 of P&L. 37 journal lines held only 29 distinct trades.

This was found by auditing the **full** journal history rather than the
single session under investigation, and it matters well beyond tidiness:
the newly-wired daily-loss breaker (fix 1 above) sums today's journalled
P&L, so duplicates would trip the circuit breaker early on phantom
losses. Recovery is now idempotent -- it skips any trade id already in
the journal and clears state itself -- and `compute_risk_state()`
deduplicates by trade id so it stays correct against the duplicate
entries already sitting in existing journals.

### Validated against the full history, not just one session

Re-running the whole 6-day / 29-trade journal through the new rules:

- **Re-entry gate**: of 7 same-day repeats across all sessions, it blocks
  3 (the 24050 PE pair 1.3% and 0.0% apart, and a 2026-07-21 24200 PE
  pair 1.5% apart where *both* legs lost) and allows 4 that had
  materially different geometry -- including the two re-entries that
  actually **won** (+60.4% and +64.7%). A blanket cooldown risks
  suppressing exactly those.
- **Expiry-day cutoff**: blocks 2 historical trades -- one at -16.8%
  (Rs -835) and one at +22.8% (Rs +172). It is **not** free: it would
  have cost a winner. Net across the two it still saves Rs 663, because
  the loser was far larger in absolute rupees, but that is a small sample
  and the rule is a judgment call, not a proven edge.

### What this does NOT fix

**The target is systematically unreachable, and that is not a
2026-07-28 problem.** A `DEFAULT_STOP_LOSS_PCT` of 30% with
`DEFAULT_TARGET_RR = 2.0` demands a **+60%** move in the premium. Across
the full journal history:

- **2 WINs in 29 trades.**
- Of the 13 trades that tracked max-favourable excursion, only **2 ever
  reached +60%** at any point while open. The median best-move-ever was
  **+20.3%**.

So for roughly 85% of trades the target was never in reach at any moment
of their life -- the exit was always going to be a stop or an EOD close.
Meanwhile the stop sits inside normal oscillation (the three 24050 PE
trades drew down ~30% while their best move was +33.7%). Volatility-based
sizing (fix 5) helps, but it cannot resolve the underlying tension:
**you cannot have both a stop outside a ±30% noise band and a 2:1 target
on an instrument whose realistic move is +20%.**

**But lowering the target is NOT the fix -- checked, and it makes things
worse.** The obvious inference from "only 15% of trades ever reach 2R" is
to lower the target. Simulating every candidate target against the real
journal (`trade_tracker.rr_milestone_stats()`, which replays each trade
as if the target had been set at that level and it had exited there)
says otherwise:

```
  0.5R reached by 7 (53.8%), simulated expectancy -0.178R
    1R reached by 4 (30.8%), simulated expectancy -0.111R
  1.2R reached by 3 (23.1%), simulated expectancy -0.185R
    2R reached by 2 (15.4%), simulated expectancy -0.028R  <-- current
```

A lower target hits far more often but wins less each time. On this
sample the **current 2R setting has the least-bad expectancy of the lot**,
and moving to 1.2R would roughly *sextuple* the loss per trade. Hit-rate
is the seductive number here and it is the wrong one; expectancy is the
one that decides.

The real conclusion is less comfortable: **every simulated target is
negative, which points at entry quality rather than target placement.**
No exit rule rescues a signal with no edge. That is what fixes 2-4 and 8
(structure ratchet, direction, contradiction, OI freshness) are aimed at,
and whether they worked is an empirical question that needs more
sessions.

So `DEFAULT_TARGET_RR` stays at 2.0 -- not because it is right, but
because 13 measurable trades cannot justify changing it and the evidence
that does exist points the other way from intuition. The system now
records the R-multiples every trade reaches (see below) so this can be
decided on data later.
`tests/test_scoring_and_risk.py::test_20260728_excursion_was_symmetric`
pins the underlying finding so it stays visible instead of being quietly
tuned away.

### R-multiple milestone tracking

"R" is a trade's own risk unit: 1R = entry - stop, so `DEFAULT_TARGET_RR`
is literally how many R we aim for. Every open trade now records which
multiples in `config.RR_MILESTONES` it actually touched **and when**,
plus `max_r_reached`, `r_at_exit`, and `rr_would_have_won_at` (the list
of targets under which it would have been a winner rather than the
outcome it got). The journal's `lesson` field states it plainly, e.g.
*"Peaked at +1.12R (aiming for 2R) -- would have hit target at: 0.5R,
0.8R, 1R."*

The timing is the part that can't be reconstructed afterwards: "reached
1.2R at 11:04, then gave it all back and stopped out at 14:05" is a
completely different lesson from "never got going," and a pass/fail
outcome hides both. `trade_tracker.summarize_rr_milestones()` prints the
aggregate table above at session startup and it is shown on the live
dashboard.

This is evidence-gathering only -- **it changes no trading behaviour.**

Everything above is covered by `tests/` (44 tests), which now run in CI
as a hard failure rather than advisory.

## Fixed: 2026-07-27 -- dashboard overlap, missing worst-seen, vanishing exits, no totals

Real feedback from the first live day using the dashboard, all fixed:

1. **Strike Landscape label overlap** -- when two landmarks landed at
   the same or very close price (e.g. Max Pain and Call Wall both at
   24800), their labels rendered on top of each other, unreadable. Fixed
   with a proper collision-avoidance algorithm: landmarks within 11% of
   each other on the strip get assigned to different vertical tiers
   (with a thin connector line back to the track), while dots always
   stay at their true position. Tested directly against the exact
   overlapping scenario from the screenshots, plus a case with well-spread
   landmarks to confirm it doesn't over-trigger when nothing's colliding.
2. **Only "Best Seen" was shown, not the worst** -- a losing trade's
   `min_ltp_seen` existed in the data but wasn't displayed. Added a
   "Worst Seen" column so both extremes of a trade's excursion are
   visible regardless of whether it's currently up or down.
3. **Exited trades just vanished** -- once a trade closed, it left
   `state/open_trades.json` (by design, that's how the tracker works)
   and disappeared from the live view entirely. Added a "Closed Today"
   panel, reading `logs/trade_journal.jsonl` filtered to trades closed
   today, so an exit is visible immediately instead of only in the
   separate post-trade dashboard.
4. **No capital deployed or total P&L** -- added a "Capital" column per
   trade (`entry x lot size x lots`, computed server-side) with a totals
   footer row, and a "Today's P&L" figure in the ticker header combining
   today's realized P&L (from Closed Today) with unrealized P&L (from
   still-open trades) into one number.

All four verified end-to-end against seeded data reproducing the actual
screenshots: capital/totals math checked by hand, closed-today filtering
confirmed to correctly exclude a trade closed yesterday, and the
overlap fix confirmed against the exact Max-Pain-equals-Call-Wall case.

## Live dashboard

Run in its own terminal, alongside `main_live.py` / `supervisor.py`:

```bash
python3 dashboard_server.py
```

Then open `http://127.0.0.1:8787` in a browser. **Read-only, stdlib-only
(no Flask, no new dependency), bound to 127.0.0.1 only** -- not reachable
from other machines on your network, let alone the internet. It never
writes to any state file and never talks to Dhan/NSE itself; it's purely
a window onto what `main_live.py`/`decision_log.py`/`condor_tracker.py`
are already writing to disk.

Shows, refreshed every 8s:
- **Risk gates** -- live exposure vs `MAX_TOTAL_EXPOSURE_PCT` and day
  drawdown vs `MAX_DAILY_LOSS_PCT`, with the circuit breaker's actual
  armed/tripped state. On the dashboard specifically because both were
  silently hardcoded to 0.0 and therefore inert until 2026-07-28; if they
  ever go inert again that should be visible, not buried in a log line
- **R-multiple progress per open trade** -- current R, peak R, and a bar
  running from the stop (-1R) to the target, with a faint segment showing
  the best the trade has EVER been. That gap between marker and peak is
  the "ran up, gave it all back" pattern, which a current-price number
  cannot show. Hovering lists each milestone and the time it was hit
- **R-multiple history** -- how often trades actually reached each
  candidate target and the simulated expectancy of setting the target
  there, so `DEFAULT_TARGET_RR` can eventually be chosen on evidence.
  Expectancy is shown next to hit-rate deliberately: hit-rate is the
  number intuition reaches for and it is the misleading one
- Live spot/VWAP/PCR and a **strike landscape** -- a visual strip
  showing where spot currently sits relative to the put wall, max pain,
  and call wall, so you can see the OI structure at a glance instead of
  reading four separate numbers
- Trend/RSI, market bias, Bank Nifty divergence, news risk, opening gap
- Open trades (momentum scanner) with running P&L and best-price-seen
- The iron condor position, if one's open (structure, net credit, max
  loss, live mark-to-market)
- Anything sitting in `trade_staging.py`'s approval queue
- A recent-activity feed pulled straight from `decision_log.py` -- every
  candidate's raw/adjusted score and exact accept/reject reason, live,
  not just after the fact in a log file
- A live/stale indicator (green dot -> red) if `main_live.py`'s log
  hasn't updated in 90s, the same staleness signal `watchdog.py` uses

## Fixed: 2026-07-24 -- silent multi-hour freeze, orphaned open trade

A session opened a trade at 11:04, then went **completely silent** for
the rest of the day -- no error, no further cycle logs, nothing -- until
the process was apparently restarted, but by then the market had already
closed. Three things came out of this, in increasing order of how
seriously they take the risk:

1. **The orphaned trade wasn't caught** (see "load_open_trades" fix
   below -- same-day restarts after close are now recovered too).

2. **`supervisor.py`** -- run this **instead of** `main_live.py`
   directly:
   ```bash
   python3 supervisor.py
   ```
   It launches `main_live.py` as a child process and checks every 20s
   whether it's still alive AND whether its log is still being updated.
   If either check fails during market hours, it kills and immediately
   relaunches the child. This shrinks the exposure window for a silent
   freeze from "however long until a human notices" (4+ hours, in the
   actual incident) down to roughly a health-check interval (tens of
   seconds). Tested against a real hung child process (writes once, then
   sleeps forever, matching the incident's shape exactly) -- detected and
   killed within 2 seconds of the configured staleness threshold.

   **This does not eliminate the risk, and that's stated deliberately,
   not modestly**: nothing running only on this machine can protect an
   open position across the gap between "the process just died" and
   "the supervisor notices." Fully closing that gap requires a
   protective stop-loss order sitting on the exchange itself, which this
   project does not do -- everything here is decision support, not order
   execution. That's a real, separate decision, not something to add
   quietly.

3. **`atomic_state.py`** -- every module that persists state (trade
   tracker, condor tracker, trade staging, opening gap, Dhan's IV/price
   baselines) now writes atomically (temp file + `os.replace`), so a
   forced kill mid-write -- which `supervisor.py` can now do -- can never
   leave a corrupted state file. Tested directly: no leftover temp files,
   full data intact.

Likely root cause of the original freeze, if it recurs: **Windows Quick
Edit Mode** in cmd.exe/PowerShell -- clicking or selecting text in the
console window pauses the process with zero output until you press
Escape or right-click to deselect, matching the symptoms exactly (alive,
silent, no error). Disable it (right-click the console title bar ->
Properties), or run from Windows Terminal instead. `watchdog.py` (run in
a separate terminal from `supervisor.py`) gives independent visibility
into this same failure mode if you'd rather monitor than auto-restart.

## Decision audit trail + opening gap (fixed/added 2026-07-24)

Prompted by a real incident: a candidate logged as "score 5.75 (bar is
5.0)" didn't open a trade, with no visible reason why. Traced it to
`apply_learned_adjustment()` (tag win-rate based, up to ±0.5/±0.25 per
tag) only ever being computed *inside* the trade-opening loop -- the
"Highest candidate" log line showed the **raw** score, never the
adjusted one a candidate actually has to clear. Two things came out of
that:

**`decision_log.py`** -- a full structured audit trail, `logs/decision_log.jsonl`,
one JSON record per cycle. Unlike `trade_journal.jsonl` (which only ever
records trades that actually opened -- a survivorship-biased sample if
you're trying to judge whether the conviction bar or learned adjustment
is well-calibrated), this logs every top candidate each cycle regardless
of outcome: raw score, contributing reasons, adjusted score + the exact
learned-adjustment notes that produced it, the risk verdict, and a
precise `final_decision` (`OPENED` / `REJECTED_BELOW_BAR_AFTER_ADJUSTMENT`
/ `REJECTED_RISK` / `ALREADY_OPEN` / `DAILY_CAP_REACHED` / `NOT_SELECTED`)
-- plus the full market context that produced it (OI analytics, trend/
RSI/volume, market bias, Bank Nifty divergence, news risk, opening gap).
The live "Highest candidate" console line was also fixed to show the
adjusted score and this same precise reason, not just the raw score
against the bar.

**`opening_gap.py`** -- captures NIFTY's and Bank Nifty's opening gap
(today's actual open vs. the previous session's close, in both points
and %) once per trading day, cached in `state/opening_gap.json`. Logged
every cycle in `main_live.py` and included in every `decision_log.py`
record, so gap context is available for future analysis of whether gap
days behave differently -- it isn't yet wired into the scanner's own
scoring, deliberately: that's a further step worth taking once there's
evidence for how gaps actually correlate with outcomes here, not a rule
invented upfront.

## Bank Nifty context / divergence

Financials (banks + NBFCs + insurance) are roughly 30-35% of NIFTY 50's
weight, so a lot of what moves NIFTY is really Bank Nifty moving
underneath it. `banknifty_context.py` fetches Bank Nifty's own spot and
trend (Dhan security ID 25 on IDX_I -- confirmed against a
community-maintained SDK's published index list, not Dhan's own docs
directly, so worth a first-run sanity check that the returned level
looks like Bank Nifty's, not NIFTY's) and computes a simple same-day
divergence read: is Bank Nifty confirming NIFTY's move, fighting it, or
inconclusive (one or both roughly flat).

This is a **context signal only** -- nothing here trades Bank Nifty
options. It shows up in two places:
- `main_live.py` logs it every cycle (cached, refetched at most every
  `config.BANKNIFTY_CACHE_MINUTES`, not every 30s poll).
- `premarket.py`'s brief includes a "Bank Nifty (previous session)"
  section, and if Bank Nifty is diverging, that's appended as an
  explicit caveat on the day's synthesized bias (a diverging financials
  sector doesn't flip the lean to the other side -- it's a reason for
  less confidence in the lean you already have, not a vote against it).

## News tracking / event-risk flags

`news_source.py` pulls headlines from Economic Times' RSS feed and a
Google News RSS search query (covering NIFTY/RBI/Fed/budget/SEBI/crude
oil terms), and tags them against keyword categories that historically
move the NIFTY: RBI/monetary policy, Fed/FOMC, Union Budget, geopolitical
shocks, crude oil, inflation/growth data, SEBI/regulatory action,
elections. This is deliberately simple keyword tagging, not sentiment
analysis or an LLM read -- same philosophy as the tag-adjustment loop in
`trade_tracker.py`: "keep a spreadsheet of what matters," not a trained
model.

(Moneycontrol and Business Standard's direct RSS feeds were tried first
but both returned HTTP 403 in live testing -- almost certainly
Cloudflare-style bot protection that header spoofing won't reliably get
past. Google News' RSS search endpoint sidesteps that by aggregating
across publishers instead of hitting each one directly.)

Matched categories roll up into a single `elevated` / `normal` risk level
for the day (`config.NEWS_RISK_ELEVATED_THRESHOLD`). This shows up in
two places:
- `premarket.py`'s brief, under "News / event risk"
- `main_live.py`, which checks it at most every `NEWS_CACHE_MINUTES`
  (not every 30s poll) and passes it into `risk_checker.check()`. By
  default this is **advisory only** -- an elevated day adds a cautionary
  reason to the verdict but doesn't block anything. Set
  `config.NEWS_RISK_BLOCKS_NEW_TRADES = True` if you'd rather it reject
  new trades outright on flagged days.

## Fixed: 2026-07-24 -- stale open trades silently lost on restart

If `main_live.py` was stopped (Ctrl+C, crash, machine sleep) before the
15:30 EOD settlement ran, `load_open_trades()` used to discard any
still-open trades the moment the script was next started on a new date
-- no journal entry, no warning, just gone. This happened for real on
2026-07-23 (8 trades stopped tracking around 15:20, never settled).

Fixed: `load_open_trades()` now flags stale-but-still-open state instead
of discarding it, and `main_live.py`'s startup recovers and journals
those trades (via the new `trade_tracker.settle_stale_trades()`) before
starting a fresh day -- using a fresh quote if available, falling back
to each trade's last known live price (not a guess), and tagging the
outcome `RECOVERED_INTERRUPTED_SESSION` so it's clearly auditable in the
journal rather than indistinguishable from a normal close.

## Fixed: 2026-07-24 -- news risk showing "elevated" almost every session

The Google News query and the keyword-tagging categories in
`news_source.py` used to search for and then tag against the exact same
words (bare "RBI", "SEBI", "Federal Reserve", "OPEC", "war", "crude
oil") -- meaning every headline the feed returned was pre-guaranteed to
match, and several of those words show up in completely routine
market-wrap reporting ("Sensex ends flat amid Fed rate worries, crude
oil dips"), not just genuine events. Fixed by neutralizing the Google
News query (just "is this about the Indian stock market", no
pre-filtering on event words) and requiring actual decision/action/shock
language in every category ("SEBI bans", "Fed rate cut", "crude oil
surges" rather than bare institution/commodity names), plus matching
only against the headline title (not the summary, which often carries
unrelated boilerplate). Verified against both genuine-event and
routine-market-wrap headlines after the fix -- 5/5 correct matches, 0
false positives on the routine set.

## Max/min excursion tracking (capture efficiency)

Every open trade now tracks the highest and lowest premium seen at ANY
point while it's open (`max_ltp_seen` / `min_ltp_seen`), updated every
cycle -- not just the current tick checked against target/stop. This
answers a question the target/stop check alone can never answer: **how
often does a trade move most of the way to target and then give it back
before closing?**

At close, this becomes `max_favorable_pct` / `max_adverse_pct` (the best
and worst the trade ever showed, in %) and `capture_efficiency_pct` --
of the best favorable move a trade ever had, what fraction did the
actual exit capture. 100% means it closed at or beyond its peak (a clean
target hit). A low or negative number means a real pullback ate most or
all of an earlier favorable move before the trade closed on a stop or at
end of day -- exactly the "moved 80+ points, missed target by a few
points, booked a loss anyway" pattern this was built to make visible.
When that gap is large, the journal's `lesson` field calls it out
explicitly, e.g.:

> Hit stop (-31.0%). ... Reached as high as 176.0 (+76.0% from entry)
> before closing at 69.0 (-31.0%) -- only captured -41% of that
> favorable move.

Shows up in `main_live.py`'s live open-trades line ("best seen: ..."),
every journal entry, and the trade journal dashboard (a "Capture %"
column plus an "Avg capture of peak move" summary card). This is meant
to build up the evidence base for eventually tuning target/stop logic or
adding a trailing-stop rule -- once there's enough journal history to
see the pattern clearly, not before.

## Iron condor strategy (separate, parallel strategy)

`main_condor.py` is a **second, fully independent strategy** -- sell an
OTM call and an OTM put (premium 23-30 each, `config_condor.py`), buy
further-OTM protection on each side (an iron condor: defined max loss,
not a naked strangle), opened once per weekly cycle and held to expiry.
It runs as its own process, with its own state (`state/condor_position.json`),
journal (`logs/condor_journal.jsonl`), and risk rules
(`config_condor.py`) -- completely separate from the momentum scanner's
`state/open_trades.json` / `logs/trade_journal.jsonl`. Run it alongside
`main_live.py` in another terminal; they only share the data-fetching
layer (`resilient_source.py`), nothing else.

**Why separate, not a mode inside main_live.py**: this strategy sells
premium instead of buying it (time decay works FOR it), holds for a
week instead of intraday, and even hedged, ties up meaningfully more
capital per position than a single momentum trade. Mixing its state or
journal into the momentum scanner's would corrupt the win-rate/tag stats
you're building there.

**Opening a position starts as a staged, reviewable proposal, same
shape either way** (see "Changed: 2026-07-29 -- auto-approval" below for
why the default changed from always-manual):
1. `main_condor.py`, running on a 5-minute poll during market hours,
   picks a suitable expiry (`choose_expiry_to_open()` -- any day it's
   flat, not just right after the previous expiry), scans for a
   complete 4-leg condor, and risk-checks it (`condor_risk_checker.py`:
   minimum net credit, max capital-at-risk cap, only one position at a
   time). Every candidate that clears the risk check is always **staged**
   via `trade_staging.py` first, so it's visible in the same audit
   trail/dashboard either way.
2. With `config_condor.AUTO_APPROVE_NEW_POSITIONS = True` (the
   default), it's approved and opened immediately. With it set to
   `False`: review with `python3 approve_orders.py`, then run
   `python3 open_approved_condor.py` -- this opens EXACTLY the plan you
   reviewed (strikes/premiums/credit/max-loss), not a fresh re-scan that
   may have drifted since the market moved.

**While a position is open**, `main_condor.py` marks it to market every
poll and, if spot gets within `BREACH_WARNING_BUFFER_POINTS` (default 50)
of either short strike, **stages a breach warning** for review rather
than auto-closing -- a brief wick shouldn't force an automatic exit on a
position that might reverse by end of day, but a genuine breach left
unattended until expiry can turn a small loss into the full max loss.
That call is left to you, deliberately.

**At expiry**, the position is settled automatically (this part IS
mechanical -- expiry settlement isn't a judgment call): if a leg's quote
is unavailable at that point, it falls back to intrinsic value (the only
thing that's actually true at settlement) rather than guessing.

Config to review before running this for real: `config_condor.py`'s
`HEDGE_DISTANCE_POINTS` (this single number is your max-loss dial),
`MAX_CAPITAL_AT_RISK`, and `MIN_NET_CREDIT` are starting assumptions, not
researched optima -- tune them once you've seen real premium/strike data
for a few cycles.

## Directional credit spread strategy (a THIRD, separate strategy)

Prompted by looking at a third-party marketplace "credit spread
overnight" product and deciding to build an in-house equivalent instead
of paying to deploy something with no visible logic. Where that
evaluation actually landed: the vendor page exposed nothing beyond a
single unqualified "39.63% (3 month)" return figure -- no drawdown, no
trade count, no win rate, every performance tab gated behind login --
and sibling strategies from the same shop on the same page, same
instrument, same window ranged from -22% to +89%. That dispersion is the
signature of small-sample noise, not demonstrated edge, and it's exactly
what this project's own measurement tooling (costs, R-multiples, shadow
replay) exists to stop us from fooling ourselves with on our own trades.
Better to build a version we can actually audit and shadow-test.

`main_directional_spread.py` is a third independent strategy alongside
the momentum scanner and the iron condor, with its own state
(`state/directional_spread_position.json`), journal
(`logs/directional_spread_journal.jsonl`), and config
(`config_directional_spread.py`).

**How it differs from the iron condor, which it otherwise closely
resembles in structure**: the condor is market-NEUTRAL (sells both a
call spread and a put spread, wins if spot stays in a range), opens once
a week, and runs to expiry unless breached. This strategy is
DIRECTIONAL -- it sells only ONE side, chosen by `scanner.compute_market_bias()`
(the same top-down bullish/bearish/range read that gates the momentum
scanner's counter-bias candidates): bullish sells a **bull put spread**
(short PE, wins if spot doesn't fall through the short strike), bearish
sells a **bear call spread** (short CE, wins if spot doesn't rise
through it). A neutral or weak bias picks no side at all -- there's no
directional edge to sell against, so no trade. It can open on any day
the bias reads strongly enough (checked every 30s, matching the momentum
scanner's cadence, not just once a week like the condor), and rather
than running to expiry it's **actively managed**: closes automatically
at `PROFIT_TARGET_PCT_OF_MAX_PROFIT` (default 60%) of the max credit
captured, or `STOP_LOSS_PCT_OF_MAX_LOSS` (default 50%) of the max
possible loss -- standard credit-spread practice, since the bulk of
theta decay is captured well before expiry and holding for the last few
points disproportionately extends overnight gap-risk for little extra
reward. A breach warning (spot within `BREACH_WARNING_BUFFER_POINTS` of
the short strike) still stages for human review rather than
auto-closing, same reasoning as the condor's identical mechanism.

**Opening a position works the same way as the condor** (see "Changed:
2026-07-29 -- auto-approval"): every candidate is staged via
`trade_staging.py` first, then either auto-approved and opened
immediately (`config_directional_spread.AUTO_APPROVE_NEW_POSITIONS = True`,
the default) or left for manual review -- `approve_orders.py`, then
`open_approved_directional_spread.py` opens EXACTLY the plan you
reviewed. Expiry settlement falls back to intrinsic value when a leg's
quote is unavailable, same as the condor.

Verified end-to-end against 2026-07-29's actual recorded market data
(742 cycles): every bias score that day topped out at 1.5, never
clearing this strategy's `BIAS_STRONG_THRESHOLD` of 2.0, so it correctly
would have opened nothing all session -- a deliberately higher bar than
the momentum scanner's counter-bias penalty threshold, since selling
naked-side premium against a read deserves more confidence than merely
penalising a candidate that opposes it. 30 tests cover direction
selection, strike/hedge selection, plan pricing for both spread types,
all four risk gates, and the full position lifecycle (open ->
mark-to-market -> profit target / stop loss / breach warning / expiry
settlement with intrinsic-value fallback).

Config to review before running this for real, same caveat as the
condor's: `config_directional_spread.py`'s `HEDGE_DISTANCE_POINTS`,
`BIAS_STRONG_THRESHOLD`, `MAX_CAPITAL_AT_RISK`, and the profit-target/
stop-loss percentages are starting assumptions, not researched optima.

## Order execution (placeholder, not active)

This project still only prints recommendations -- nothing places an order.
`trade_staging.py` and `approve_orders.py` are a placeholder for **if you
ever add execution**: a "Trading as Git" style gate where every proposed
order is staged as a `PENDING` record, a human explicitly approves or
rejects it (`python3 approve_orders.py`), and only an `APPROVED` record
could ever be picked up by a (currently nonexistent) execution layer.
Nothing in either file calls a broker API. They are not wired into
`main_live.py` yet -- that's a deliberate future step, not something that
should silently change what the live loop does today.

## Data source fallback (Dhan -> NSE -> TradingView)

`main_live.py` now imports from `resilient_source.py` instead of talking
to Dhan directly:

1. **Dhan** (primary) — full chain + Greeks.
2. **NSE public API** (fallback) — full chain, OI/IV/PCR all work, but no
   Greeks (delta/theta/vega come back `None`).
3. **TradingView** (last resort) — TradingView has no public option-chain
   data at all, so this tier only backstops spot price and candles for
   price-action analysis. OI-based setups simply won't fire until Dhan or
   NSE recovers; the pipeline logs which tier is active each cycle
   (`snapshot.source`) rather than failing silently.

Each tier has a cooldown after a failure so a genuinely-down source
doesn't add latency/log-noise to every 30s poll — see
`FALLBACK_RETRY_COOLDOWN_SECONDS` in `config.py`.

## Added: 2026-08-02 -- profit-milestone tracking for the directional spread and condor

The momentum scanner has tracked R-multiple milestones since the
2026-07-27 session (`config.RR_MILESTONES`, `trade_tracker._update_excursion`
/ `rr_milestone_stats`) -- the dataset behind "is `DEFAULT_TARGET_RR = 2.0`
actually the right target?". The other two strategies had nothing
equivalent: `directional_spread_tracker.py` and `condor_tracker.py` marked
P&L every cycle but threw the excursion away, so there was no way to ask
whether `PROFIT_TARGET_PCT_OF_MAX_PROFIT = 60` is well-chosen, or whether
the condor (which has no active exit at all, held to expiry unless
manually closed on a breach) should get one.

Same shape as the momentum version, in this domain's own unit -- % of
`max_profit_inr`, since a credit spread/condor has no symmetric "R" the
way a fixed-stop long option does (max profit and max loss are related
through the hedge width, not equal by construction):

  - `PROFIT_MILESTONES_PCT = [10, 20, ..., 100]` added to both
    `config_directional_spread.py` and `config_condor.py`.
  - Both trackers now record, on every cycle a position is open, the
    running max/min mark-to-market P&L and which milestones have been
    touched and when (`_update_excursion`, called from both
    `update_position()` and `close_position()` so a milestone touched
    only on the closing cycle -- e.g. expiry settling right at max
    profit -- isn't missed). A cycle with no priceable P&L (a leg's quote
    missing) updates nothing, same "missing information is not a zero"
    rule as the 2026-07-30 condor MTM incident.
  - At close, `_excursion_summary()` adds `max_pct_of_max_profit`,
    `would_have_won_at` (every milestone this position actually reached),
    and `capture_efficiency_pct` (what fraction of its own best-ever mark
    the actual exit captured -- 100% means it closed at or beyond its
    peak).
  - `profit_milestone_stats()` / `summarize_profit_milestones()` in both
    trackers turn the journal into the same evidence-for-tuning view
    `rr_milestone_stats()` gives momentum. The condor's version has no
    `current_target_pct` to mark, since there is no active target to
    compare against yet -- this is explicitly the dataset for deciding
    whether to add one.
  - Per-cycle log line in `main_condor.py`/`main_directional_spread.py`
    now shows peak MTM and the highest milestone hit so far, mirroring
    `main_live.py`'s existing R-multiple line. Both trackers' stats are
    also on the dashboard (`dashboard_server.py`), alongside momentum's
    `rr_stats`.

No live decision changed. This is purely additive tracking -- the
condor's 60%/50% target and stop, and the fact that the condor still has
no active exit, are both untouched. `condor_tracker.py` had no dedicated
unit tests before this (only its expiry-selection and backtest-module
neighbours did); `tests/test_condor_tracker.py` is new and covers the
whole lifecycle, not just the milestone addition.

370 tests passing (348 + 22: 11 for the spread tracker's milestone
tracking, 12 for the condor tracker overall since it had no prior direct
coverage).

## Added: 2026-08-02 -- live order-flow feed (WebSocket), not yet wired into any strategy

Four new modules, the first non-REST data path in this project:

  - `instrument_master.py` — maps (strike, expiry, option type) to Dhan's
    numeric security ID from their public CSV. A prerequisite for
    anything addressing contracts by ID rather than by chain lookup,
    which also unblocks BACKLOG's lightweight `/marketfeed/ltp`
    fast-position-check item. Cached with an explicit age bound rather
    than "once a day": new weekly expiries appear in the master before
    they trade, and a stale cache silently missing a contract would look
    exactly like "that strike has no order flow".
  - `orderflow_packets.py` — binary decoder for the feed's packed
    packets.
  - `orderflow_feed.py` — the persistent WebSocket process. Separate
    process, communicating through a state file, for the same reason the
    three strategies are separate: a long-lived event-driven socket must
    not be interrupted by any trading loop's timing, and nothing may
    block on it being alive.
  - `orderflow.py` — read side. Every accessor is age-gated and returns
    None when unavailable, never a plausible default; a missing book is
    missing information, not a balanced book.

**Validated against live data, not just unit tests.** Connected during
market hours' close, subscribed 24 contracts, and cross-checked every
parsed field against the REST option chain: LTP and OI matched exactly on
every contract.

That cross-check caught a documentation error. Dhan's spec labels one
Quote/Full field "Day Close", but it is the PREVIOUS session's close —
it matched `previous_close_price` exactly on every contract, and its
values routinely sit outside the same packet's own day high/low, which is
impossible for a close of the session being reported. It is named
`prev_close` here, with the discrepancy documented at the field.

The unit tests caught a worse one during development. The first version
of the decoder hand-wrote its struct format with `avg_price` typed
`int32` and `volume` `float32` — transposed. Both are 4 bytes, so the
packet still measured exactly the documented 50 and a size assertion
passed clean while every value after LTP would have been garbage. The
field layouts are now tables of (name, type) shared by parser and tests,
so a wrong type fails at pack time instead of producing plausible
numbers. Same silent-corruption class as this project's one-bar timestamp
shift, flat `iv_percentile`, and absent OI buildup.

**What this does and does not provide.** The feed carries the order BOOK
— five levels of resting bid/ask size, plus exchange-wide total buy/sell
quantity. It does NOT carry a trade tape with aggressor flags, so classic
footprint / cumulative-delta order flow cannot be built from it. Book
imbalance measures intent to trade at a price; resting orders can be
pulled. The arguably larger win is real bid/ask: every backtest number in
this project is currently priced at LTP and therefore optimistic.

**Nothing consumes this yet** — no strategy reads the book and no scoring
uses it. See BACKLOG.md for the two open questions (measure real intraday
spreads; establish whether book imbalance predicts anything, via the same
forward-return method `component_study.py` used) that should be settled
before wiring it into a live decision.

Run it standalone (spread recording is on by default):
```bash
python3 orderflow_feed.py --strike-range 300
```

Then, after a session:
```bash
python3 spread_study.py
```

`orderflow_recorder.py` samples every contract's quoted spread every 30s
and writes `logs/orderflow/YYYYMMDD.jsonl.gz`, tagging each sample with
its market phase (pre_open / opening / regular / closing / post_close).
Phase is RECORDED rather than filtered at capture time on purpose: the
post-close reading is exactly what made the LTP assumption questionable,
and discarding it at capture would have thrown away the observation that
prompted the work. `spread_study.py` does the filtering, and refuses to
report a trading-cost figure at all until it has regular-session samples
— a blended average across phases would describe a market nobody trades
in. It reports per market phase, per premium level, and per strategy
inside that strategy's own configured premium band, since a chain-wide
average applies to none of them.

## Changed: 2026-08-02 -- directional spread's strike selection re-tuned (40-70/100)

`sweep_spread_config.py` re-ran the full 493-day, 2-year history once per
(premium band, hedge distance) combination -- with the risk gates that
were dead until earlier this same day now actually enforced
(MIN_NET_CREDIT, MAX_CAPITAL_AT_RISK, MAX_CONCURRENT_POSITIONS,
MAX_NEW_POSITIONS_PER_DAY, and the cross-day one_at_a_time state, all
fixed in this session -- see the entries above). Reused the same
full-re-run-per-cell reasoning as the momentum threshold sweep:
`run_all()` opens at most one position per day and takes the FIRST
candidate that clears every gate, so a wider premium band changes WHICH
candidate is found, not just whether it survives -- post-hoc filtering a
single run would give the wrong answer.

The old config (SHORT_PREMIUM_MIN/MAX = 30-60, HEDGE_DISTANCE_POINTS =
150) turned out to be the WORST cell in the entire 4x3 grid -- every
other premium band beat it at every hedge distance tested:

| premium | hedge | n | win% | total (2yr) | z | max DD | return/DD |
|---|---|---|---|---|---|---|---|
| 30-60 (old) | 150 (old) | 108 | 86.1% | Rs 64,236 | 3.49 | -Rs 10,332 | 6.22 |
| 65-100 | 200 | 124 | 81.5% | **Rs 133,390** (highest) | 4.54 | -Rs 14,859 | 8.98 |
| **40-70 (new)** | **100 (new)** | 125 | 85.6% | Rs 78,006 | 5.48 | **-Rs 5,099** (lowest) | **15.30** (best) |

All 12 cells were profitable, with z from 2.94 to 5.57 -- the Bonferroni
bar for 12 comparisons is ~2.9, so every cell clears it. This is a broad,
replicated finding across the grid, not a single lucky cell (contrast
with the iron condor sweep below, where nothing cleared even |z|=1).

40-70/100 was adopted over the grid's highest raw total (65-100/200)
for return-per-unit-of-drawdown instead: more than double the old
config's ratio, at roughly a third of the drawdown of the highest-total
cell. Full grid in `logs/sweep_spread_config.json`.

Reverting is a two-value edit in `config_directional_spread.py` (the old
values are in that file's own comment) if live results diverge from this
backtest -- same reasoning as `SCORING_MODE` below. Still an IN-SAMPLE
result: LTP fills only, no untouched data left to validate against.

## Not adopted: 2026-08-02 -- iron condor structural sweep found no credible config

The condor's baseline (SHORT_PREMIUM_MIN/MAX = 23-30, HEDGE_DISTANCE_POINTS
= 300) lost Rs 18,941 over 2 years with 64% of every skipped cycle a DATA
coverage gap (a leg existed but fell outside the ~500pt reconstructed
window), not a real rejection -- both swept parameters directly control
how far from spot the legs sit. `sweep_condor_config.py` tested a 4x3
grid (premium bands 23-90, hedge distances 150-300) with the same risk
gates now enforced.

Best cell: premium 70-90, hedge 200 -> +Rs 16,107, **z = 0.27**. Nothing
in the grid reached |z| = 1, let alone significance, and coverage-gap
stayed high throughout (42-84%). There IS a real directional trend --
every premium band above the baseline 23-30 beat it, consistently, at
every hedge distance -- so the current config is demonstrably not the
best available.

Pushed it further with two more rounds, narrowing hedges and raising
premium in the direction each round pointed: a 50-100 premium/100-200
hedge grid got to z=0.95 with coverage gap down to 25% (confirming
narrower hedges genuinely fix the coverage problem), and an 80-150
premium/50-100 hedge grid found the best cell overall -- premium
115-150, hedge 75-100 -> Rs 44,096, **z = 1.72**. Still short of a plain
95% single-test bar (1.96), let alone the ~3.2 Bonferroni bar 36
cumulative comparisons across all three rounds demand.

More importantly, that best-looking cell isn't a better-tuned condor --
win rate at premium 115-150 dropped to 37.5% (from 70% at baseline),
meaning the strikes are now close enough to spot that this is
structurally a near-the-money strangle, not the wide-OTM defined-risk
strategy config_condor.py is designed around. Optimizing one metric
shouldn't be what decides to change what a strategy fundamentally is.

CLOSED after 3 rounds / 36 total configs, not left open-ended: nothing
adopted, condor stays at its original baseline config. See BACKLOG.md's
2026-08-02 entry -- only reopen with historical data wider than the
ATM+/-10 reconstruction cap, not another sweep against the same data.
Full grids in `logs/sweep_condor_config*.json`.

## Changed: 2026-08-02 -- momentum scorer switched to `SCORING_MODE = "momentum_only"`

A 493-day, 2-year forward-return study (`component_study.py`, 527,528
candidates evaluated counterfactually against every candidate the
scanner ever flagged, not just the trades taken -- see that module's
docstring for why taken-trade analysis is selection-biased and produced
a spurious negative score/return correlation when tried) found:

  - Momentum ROC alignment was the only component to survive Bonferroni
    correction across all 41 tested (z > 30, robust across every
    moneyness band: +4.2% to +8.9% lift, aligned vs against).
  - It carried the SMALLEST weight in the legacy scorer, +/-0.25.
  - IV percentile (+/-1.0, four times momentum's weight) looked wrong
    pooled but reversed sign once stratified by distance from spot --
    right for far-OTM strikes, backwards for the near-to-mid-money
    strikes this scanner actually trades, because cross-sectional IV
    percentile largely encodes the volatility smile.
  - Support-level "supports this contract" credit was negative in every
    moneyness band tested; resistance, scored identically, was correct.

`compare_variants.py` backtested five re-weighted scorers against the
same 493 days with `shadow.py`'s daily-loss and total-exposure gates
actually enforced (see the entry below this one -- they were dead in
every backtest before today). Gross expectancy per trade, at matched
trade-count ranges:

| variant | gross R/trade | z | total (2yr, Rs 5L capital) | max drawdown |
|---|---|---|---|---|
| legacy (as it ran before today) | +0.006R | +0.09 | -Rs 51,599 | -Rs 108,780 |
| combined (momentum up, IV+support removed) | +0.241R | +4.06 | +Rs 118,689 | -Rs 94,250 |
| **momentum_only (adopted)** | +0.158R | **+5.28** | **+Rs 470,031** | -Rs 203,854 |

`momentum_only` won on total return, return/drawdown ratio (2.31 vs
1.26), and statistical confidence, so it is now the live default:
`scanner.scan()` overrides a candidate's final score to
`config.MOMENTUM_ONLY_ALIGNED_SCORE` / `_AGAINST_SCORE` / `_NEUTRAL_SCORE`
based purely on momentum alignment when `config.SCORING_MODE ==
"momentum_only"`. The full weighted score (IV, OI buildup, S/R levels,
RSI, PCR, volume, structure) is still computed and still recorded in
`reasons` -- `apply_learned_adjustment` and the decision log are
unaffected -- only the number that RANKS and CLEARS
`MIN_CONVICTION_SCORE_TO_TRACK` changed. `logic_version.py` fingerprints
`SCORING_MODE` and the three override constants, so results under the
two modes are never silently pooled, and switching back to `"legacy"` is
a one-line change (`tests/test_scoring_mode.py` covers both directions).

Verified the live code path and the backtest's `rescore` shim produce
IDENTICAL trades on a sample day (same strikes, same timestamps, same
entries) before adopting this -- the backtest evidence only applies to
what's actually running if the two cannot drift apart.

**This is an in-sample result and has not been forward-validated.** See
BACKLOG.md's entry on this for what that means and what to watch for.

## Fixed: 2026-07-31 -- Dhan 429s reaching main_live.py mid-trade, not just main_condor.py

The 2026-07-30 backlog entry on Dhan rate limiting (three processes
sharing one account's 1 request/3s limit with no cross-process
coordination) was tracked as a lower-urgency item until 2026-07-31,
when the same storm hit `main_live.py` directly while a real trade was
open: 48 of 72 failure-related log lines that day landed during that
one position, including the 5s fast-check itself failing outright
("Both dhan and nse sources are in cooldown"). Nothing broke only
because that trade never approached its stop/target during the gaps --
luck, not the system working as designed. A stop/target check silently
not running during a real approach is a risk to the P&L data this
whole project's measurement effort depends on being trustworthy.

`resilient_source.py`'s tier-cooldown bookkeeping (`_last_failure`) is
in-memory and per-process, so none of the three processes
(`main_live.py`: 30s + a 5s fast-check; `main_condor.py`: 5 min;
`main_directional_spread.py`: 30s) has any way to know the other two
exist, let alone that they share a single account-level rate limit.

New module `dhan_rate_limiter.py`: before every Dhan HTTP call, a
process calls `wait_for_slot()`, which checks a small shared state file
for the wall-clock time of the last Dhan request **by any process** and
sleeps out the remainder of `MIN_INTERVAL_SECONDS` (3.5s, slightly
above Dhan's documented limit) if needed. Coordination uses a lock file
created with `os.O_CREAT | os.O_EXCL` -- a standard, portable advisory
lock the OS guarantees is atomic, so two processes can never both
believe they hold it. A lock older than `STALE_LOCK_SECONDS` (10s) is
force-cleared rather than left to deadlock every process sharing the
account, on the same "any process here can be killed at any time"
assumption `supervisor.py` already makes. If the lock can't be acquired
within `MAX_ACQUIRE_WAIT_SECONDS` (8s), a process proceeds without it --
fails open, rather than ever blocking a trading loop indefinitely over
a coordination mechanism. Wired into all 4 of `dhan_source.py`'s
`requests.post()` call sites.

Also widened `main_directional_spread.py`'s `POLL_INTERVAL_SECONDS`
from 30 to 90: its entry signal is a bias score that doesn't move
within seconds the way the momentum scanner's setups do, so there's no
accuracy cost, and it cuts this process's share of shared Dhan request
volume to a third of what it was.

6 new tests in `tests/test_dhan_rate_limiter.py` (minimum spacing
enforcement, stale-lock clearing, fail-open under lock contention).
201 total passing.

This does not fix the NSE fallback tier being blocked by tightened bot
detection (separate backlog item, not being pursued -- see
`BACKLOG.md`).

## Fixed: 2026-07-30 -- condor MTM silently going "unavailable" mid-position

Live log with an open condor:

```
[10:57:14] Open condor: ...  MTM P&L: unavailable this cycle
[11:07:14] Open condor: ...  MTM P&L: Rs 114
[11:17:15] Open condor: ...  MTM P&L: unavailable this cycle
[11:22:16] Open condor: ...  MTM P&L: unavailable this cycle
```

No data-source error logged alongside the "unavailable" cycles -- the
chain fetch was succeeding. Root cause: `STRIKE_RANGE_POINTS` (default
800pts of CURRENT spot) is applied when the chain is built, in both
`dhan_source.py` and `nse_source.py`, unconditionally on every fetch.
The condor's hedge PE leg, opened 300pts (`HEDGE_DISTANCE_POINTS`)
below its short strike, was close enough to that 800pt line that normal
intraday drift pushed it in and out of the window from one cycle to the
next. Checked against the real log, the leg's distance from spot at
each timestamp lines up exactly with which cycles failed:

| Time | Spot | Hedge leg distance | Included? |
|---|---|---|---|
| 10:57:14 | 24350.7 | 800.7 | no -- unavailable |
| 11:07:14 | 24346.5 | 796.5 | yes -- Rs 114 |
| 11:17:15 | 24356.9 | 806.9 | no -- unavailable |

Same bug **class** as the 2026-07-22 incident above, a different
filter. That fix moved the *premium* band out of chain-build time
entirely, because a candidate-selection filter has no business touching
the raw chain at all. `STRIKE_RANGE_POINTS` is different: narrowing the
universe for scanning and IV-percentile purposes is a deliberate,
reasonable choice (`config.py`: "deep OTM strikes are usually
near-worthless and just add noise"), so it wasn't removed -- it just
needed the same protection `PREMIUM_MIN`/`PREMIUM_MAX` already has:
never drop a strike something is actively tracking.

`get_nifty_snapshot()` (in `dhan_source.py`, `nse_source.py`, and
`resilient_source.py`'s pass-through) now takes an optional
`must_include_strikes` set that bypasses the distance filter regardless
of how far a protected strike has drifted. `main_condor.py` passes its
open position's 4 legs, `main_directional_spread.py` its 2, and
`main_live.py` every open momentum trade's strike -- the last of these
wasn't reported broken (no error there either, `trade_tracker.py`
already tolerates a missing quote by skipping that trade for the
cycle), but it's the identical root cause with a worse consequence: a
skipped cycle means that trade's stop/target check silently didn't run
either. Lower probability there (an 800pt same-day move is a >3% day,
rare) but free to close while fixing the confirmed case.

11 new tests, including a full `get_nifty_snapshot()` reproduction
proving a protected strike is dropped without the fix and survives with
it, using the real incident's exact strikes and spot. 195 total passing.

## Fixed: 2026-07-29 -- condor never opened, "not the day after expiry" every cycle

`main_condor.py`'s `is_day_after_expiry()` could structurally never
return `True`. It compared today's date against
`get_nearest_expiry()`'s return value -- but that function only ever
returns an ACTIVE/UPCOMING expiry (Dhan's expirylist endpoint doesn't
list past ones), so that date is always today-or-future.
`days_since_expiry = today - expiry_date` was therefore always <= 0,
and the intended `1 <= days_since_expiry <= 3` window could never be
hit. The condor strategy logged "Not the day after expiry" every single
cycle, forever, and never staged a position.

There's no API that hands back the previously-settled expiry directly,
so it's now derived from state (`state/condor_expiry_tracking.json`):
whenever the value `get_nearest_expiry()` returns CHANGES from what was
last observed, that change is itself the signal that the old value
just settled. The old "current" becomes "previous" and stays frozen
there (not overwritten again) until the next rollover, which is what
keeps the 1-3 day grace window working across multiple days -- e.g. if
staging fails on the rollover day itself (missing a leg in the chain),
the next day's check still has the right previous expiry to retry
against. Never assumes a fixed 7-day cycle, so a holiday-shifted or
monthly-special expiry needs no special handling -- same philosophy as
`premarket.py`'s expiry handling elsewhere in this project. Also safe
after a long outage: a previous expiry reconstructed from weeks ago
naturally falls outside the 1-3 day window rather than misfiring.

Verified against the exact expiry-day transition: the check correctly
stays `False` on expiry day itself (that's the day *of* expiry, not
after) and flips `True` the very next day. 8 new tests covering first-run,
grace-window persistence, window closing after day 3, long-outage safety,
and holiday-shifted expiries.

## Changed: 2026-07-29 -- condor can open any day, and auto-approval

Two further changes, same day, on top of the fix above.

**1. Opening is no longer restricted to a narrow post-expiry window.**
The `is_day_after_expiry()` / grace-window approach above is gone
entirely, replaced by something simpler: `main_condor.choose_expiry_to_open()`
scans the full expiry list (`resilient_source.get_expiry_list()`, new --
Dhan/NSE only ever exposed the single nearest one before) and opens
against the first expiry with at least `MIN_DAYS_TO_EXPIRY_TO_OPEN` days
left (default 1). This means:
- The condor can open on **any day** it's flat, not just the 1-3 days
  right after the previous expiry -- selling a fresh weekly cycle on
  Monday and selling a shorter-dated position on Wednesday are both now
  reachable, not just the former.
- Running the tool **on expiry day itself** correctly rolls straight
  into the FOLLOWING week's expiry rather than trying to sell a contract
  with ~0 days of theta left.
- No state tracking needed any more -- every cycle just asks the expiry
  list directly, which is simpler than the previous fix's rollover
  detection and has no long-outage edge case to reason about.

Worth knowing: a position opened with only 1-2 days left on the nearest
expiry has a different risk/reward shape than a fresh 6-7 DTE cycle --
less premium available for the same hedge width, faster gamma ramp into
expiry. `MIN_DAYS_TO_EXPIRY_TO_OPEN` is the dial for how close is too
close; raise it if you'd rather only ever sell fresh weekly cycles.

**2. Auto-approval.** Both the condor and the new directional spread
strategy staged every candidate for manual review via `approve_orders.py`
-- deliberate, while the strike-selection logic was untested. With both
now covered by real tests and verified against real recorded data,
`config_condor.AUTO_APPROVE_NEW_POSITIONS` and
`config_directional_spread.AUTO_APPROVE_NEW_POSITIONS` (both default
`True`) let a risk-approved candidate open immediately instead of
waiting. The staged audit record is still written either way
(`trade_staging.stage_and_maybe_auto_open()`, shared by both
strategies) -- auto-approval only skips the manual `approve_orders.py`
step, it does not add order-execution capability that didn't exist
before. **Nothing in this project calls a broker API in either mode**:
"opening a position" has only ever meant writing that strategy's own
local tracked state (`condor_tracker.py` / `directional_spread_tracker.py`),
used for mark-to-market and the journal, same as the momentum scanner's
`trade_tracker.py` has always done automatically without any staging
step at all. Set either flag back to `False` to return to manual review.

## Market regime context: is today even a normal day?

Added 2026-07-30 after checking, for the first time, whether the sessions
this system had been evaluated on were representative. They were not:

| | Our 7 recorded sessions | Trailing 6 months |
|---|---|---|
| Median daily range | **0.61%** | **0.98%** |
| Days >= 1.0% range | **0** | 58 of 121 (48%) |
| Days >= 1.5% range | **0** | 24 of 121 (20%) |

Every single recorded session fell below the 6-month median, between the
1st and 45th percentile. 2026-07-28 (0.36%) matched the quietest day in
six months. A directional/momentum strategy had been judged exclusively
on the calmest sliver of conditions and **never once observed in the
regime it exists for** -- which makes every "no edge" reading so far
conditional on an unrepresentative sample.

That is far too important to be something reconstructed from six months
of history a week after the fact, so `market_regime.py` now reports it
live: today's range as a percentile of the trailing
`REGIME_LOOKBACK_DAYS` distribution, plus a quiet/normal/volatile label,
logged every cycle and recorded into every `decision_log.py` entry so
later analysis can segment results by regime directly.

It reuses the intraday candles `main_live.py` already fetches (no extra
call for today's part) and caches the trailing daily-range baseline once
per day in `state/regime_baseline.json`.

**The partial-day caveat is handled explicitly, not papered over**: an
in-progress session's range is necessarily incomplete -- at 09:30 it is
near zero and would score as the quietest day on record. So the reading
always carries how much of the session has actually elapsed and stays
flagged `PARTIAL` until it's nearly done:

```
Market regime: range 59.7 pts (0.25%)  |  p0 vs 121d history (median 0.98%)  |  QUIET
   [PARTIAL -- 12% of session elapsed, range can only grow from here]
```

An early percentile is a floor on the final value, not an estimate of
it. If the daily-history fetch fails the raw range is still reported,
just without a percentile -- losing the benchmark shouldn't lose the
observation.

Note this answers a *different* question from
`price_action.classify_trend()`, which reads trend direction from swing
structure. This is about magnitude: how much the market moved at all.
By efficiency ratio (net move / total path) all seven recorded sessions
scored as chop, so the two readings have never yet disagreed -- but they
would on a strongly trending day, which is precisely the case we haven't
seen.

## Execution realism: costs, fills, liquidity, and the bias gate

Four fixes from a pipeline audit. The first three all make recorded
performance look **worse** -- that is the point. They remove optimistic
bias that was making a negative-expectancy system read as nearly
break-even.

**1. Transaction costs (`costs.py`).** Every P&L figure was previously
GROSS. On a Rs 76.70 premium with a 1R stop of 23 points, a round trip
costs ~Rs 56 in statutory charges -- about 0.038R, against a measured
gross expectancy of -0.028R at the 2R target. Costs were larger than the
entire measured edge deficit. Closed trades now carry `costs_inr`,
`pnl_inr_net`, `cost_r` and `r_at_exit_net` **alongside** the gross
figures, which are kept so the difference stays visible.

Measured over the real 29-trade journal, mean round-trip cost is
**0.056R**, and it shifts every target's expectancy down by that amount:

```
 target     gross       net
   0.5R    -0.179    -0.235
     1R    -0.111    -0.168
     2R    -0.029    -0.085   <-- current target
   2.5R    -0.016    -0.072
```

The ordering is unchanged, so this does not alter the earlier conclusion
that lowering the target would be worse -- but every figure is now
honest. Rates in `config.py` are discount-broker assumptions; **replace
them with your own contract-note values.** Note STT applies to the SELL
side only, on premium.

**2. Side-correct fills.** `OptionQuote` gained `bid`/`ask`, and a long
position is now opened at the **ask** and closed at the **bid** rather
than marking both at LTP. LTP is the last *traded* price: it can be
stale and it sits on whichever side that trade happened to hit, so
pricing both legs there books profit that never existed. Falls back to
LTP per-quote where the source publishes no book (CSV/TradingView), and
records which basis was used on the plan (`entry_basis`).

Dhan's top-of-book field names aren't clearly documented, so several
plausible spellings are tried. **Verify on first run** that
`snapshot.chain[0].has_book` is True against a live session -- if it
isn't, none matched and fills are silently falling back to LTP.

**3. Liquidity screen.** `q.volume` was populated and never consulted;
there was no OI, volume, or spread filter at all. A tradeable premium is
not a tradeable contract. `MIN_OI_TO_TRADE`, `MIN_VOLUME_TO_TRADE` and
`MAX_SPREAD_PCT` now gate candidate selection -- at selection time only,
never at chain-build time (the 2026-07-22 lesson). A missing book skips
the spread check rather than rejecting the chain.

**4. Market-bias gating.** `compute_market_bias()` produced a top-down
bullish/bearish/neutral read that was logged and then **discarded** --
nothing filtered candidates by it, so the system could buy puts while
its own bias module read the tape the other way. `tag_bias_conflicts()`
noticed CE and PE both approved at one strike and responded by appending
a string; the trade still opened.

Counter-bias candidates are now penalised (default) or blocked, per
`BIAS_GATING_MODE`, and rejections surface as `REJECTED_BIAS_CONFLICT`
in the decision log. Default is `"penalise"` rather than `"block"` on
purpose: the bias read is itself unvalidated, and hard-blocking on an
unvalidated signal can halve the trade count for reasons nobody notices.

## Development / production split

Two checkouts, with different jobs and different rules:

| | PRODUCTION | DEVELOPMENT |
|---|---|---|
| Path | `D:\AI Projects\nifty-options-scanner` | `D:\AI Projects\option-scanner\nifty-options-scanner` |
| Runs | the live session | analysis and experiments |
| Owns | the real data (snapshots, journal, state) | a **copy** of it |
| Code | released only (`master`) | branches |
| Modified during a session | **never** | freely |

Each checkout declares its role in a `.workspace` file (gitignored, since
it is environment-specific). `python3 workspace.py` prints the role, git
state and data inventory, and warns if production is on a non-master
branch or has uncommitted changes.

**Code flows dev → prod, data flows prod → dev.** Never the reverse of
either.

### Why a copy of the data, not a shared path

Pointing development straight at production's data directory would avoid
duplication and staleness, and was rejected deliberately. Analysis code
changes fast and occasionally has bugs, and a single stray write into
production's trade journal is the worst outcome this system has. That
already happened once: `replay.py` appended 35 simulated trades to the
real journal before the guard existed. A one-way copy cannot corrupt
production no matter what development does; a few MB a day is a cheap
premium.

```bash
python3 sync_from_prod.py            # pull latest data into development
python3 sync_from_prod.py --dry-run  # see what would be copied
```

### Promoting a change to production

Never mid-session -- `supervisor.py` restarts `main_live.py` on failure,
so a file changed during market hours can be picked up on the next
restart, swapping decision logic underneath an open position.

1. Develop and test in development; `python3 -m pytest tests/ -q` green.
2. Where the change affects decisions, replay it against recorded history
   and diff (see the measurement section below).
3. Merge the branch to `master` in development.
4. **After 15:30**, in production: `git pull` (or `git merge master`),
   confirm `python3 workspace.py` shows `master` and a clean tree.
5. Next session starts on the new code, and its `logic_version` stamp
   changes accordingly, so results are never pooled across the change.

## How do we know if a change helped? (measurement methodology)

A fair question once the system starts changing regularly: if the logic
is edited every few sessions, what is any performance number actually
measuring?

**The uncomfortable arithmetic first.** With a per-trade R standard
deviation around 1.0 and roughly 4 trades a day, distinguishing a real
edge from zero at conventional confidence needs:

| Detect | Trades | At ~4/day |
|---|---|---|
| 0.10R edge | 784-1,129 | **196-282 trading days** |
| 0.05R edge | 3,136-4,516 | 784-1,129 trading days |

So forward-testing alone cannot answer the question: nobody leaves logic
untouched for 200 sessions, and shouldn't. Three problems compound it --
the instrument and the subject change together, every fix is fitted to
the most recent failure, and only ~4 of the 12-15 candidates evaluated
each day are ever measured at all.

**The fix is to stop welding data collection to decision logic.**

- `snapshot_recorder.py` records the full option chain + candles behind
  every cycle to `logs/snapshots/YYYYMMDD.jsonl.gz` (~1.5 MB/day
  gzipped). Market history now accumulates regardless of what the code
  does. Recording is best-effort and can never interrupt the live loop.
- `replay.py` re-runs `scan -> plan -> risk -> decision` over recorded
  cycles using whatever the logic currently is, and can diff two runs:

  ```bash
  python3 replay.py --day 2026-07-28 --save baseline.json
  # ...change something...
  python3 replay.py --day 2026-07-28 --compare baseline.json
  ```

  Output states plainly which trades are no longer opened, which are
  newly opened, and which kept the same entry but changed geometry.
- `logic_version.py` stamps every decision-log record and journal entry
  with a hash of the decision-relevant config plus the git SHA, so
  results from different versions can never be silently pooled. Purely
  operational settings (poll intervals, cache durations) are excluded on
  purpose -- they can't change a trade, so churning the version on them
  would fragment samples for nothing.
- `replay.py --forward-returns` evaluates the forward return of **every**
  candidate the scanner flagged, not just the ones traded. That is the
  only way to learn whether rejections are good rejections, and it turns
  ~4 observations a day into hundreds.

### What replay does NOT do

It is **not** a backtest and must never be reported as one. Replaying a
handful of recorded days while tweaking logic is textbook overfitting --
run enough variants and something will look good on two sessions.

What it legitimately buys: regression safety (did this change alter
decisions I didn't intend?) and cheap rejection (a variant that's worse
on recorded history is unlikely to be better live). Confirming an edge
still requires out-of-sample forward sessions. Replay makes rejection
fast; only forward data confirms.

Replay also reproduces the decision path, not execution -- fills are
modelled at recorded LTP, so replayed P&L carries the same optimistic
bias as the live journal until bid/ask and costs land. Treat it as an
upper bound.

### Two classes of change

Not every edit resets the evidence base, and conflating them causes
needless paralysis:

- **Correctness fixes** restore intended behaviour (the dead circuit
  breaker, the structure-score ratchet, duplicate journalling). These
  don't invalidate a strategy hypothesis -- they mean prior data was
  measuring a broken implementation, so that data should be *discarded*,
  not re-baselined. Nearly everything fixed on 2026-07-28 is this class,
  which is why changing a lot at once was defensible: repair, not tuning.
- **Strategy changes** alter the hypothesis (target R-multiple,
  conviction bar, new signals). These need the metric and required
  sample written down *before* the change, and a freeze while it's
  collected. Otherwise it's a search over variants, and the winner is
  whichever one best fits recent noise.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full pipeline diagram, why the
trade tracker sits on top of the scanner, and how the OI+price buildup
classification works.

## Trade journal dashboard

`dashboard/trade_journal_dashboard.html` is a single self-contained HTML
file — no build step, no server, no dependencies. Open it in any browser
and drag in your `logs/trade_journal.jsonl` to see:

- Win rate, average P&L, and cumulative P&L cards
- A cumulative P&L curve across closed trades
- Win rate broken down by `reason_tag` (mirrors the tag-adjustment logic
  in `trade_tracker.py`, including a flag for tags with under 3 samples)
- A sortable table of every trade

Your journal data never leaves the browser — it's read locally via the
File API, not uploaded anywhere.

## CI

`.github/workflows/ci.yml` runs on every push/PR: compiles all `.py`
files (catches syntax errors), lints with `ruff` (advisory), runs the
`tests/` suite (**not** advisory — a failure fails the build), and does
an import sanity check across all modules on Python 3.10–3.12.

```bash
python -m pytest tests/ -q
```

## Disclaimer

This is decision-support tooling for personal use — it prints recommendations
for manual review and **does not execute trades**. It is not financial advice.
Options trading carries significant risk of loss; use your own judgment and
consult a SEBI-registered advisor before trading.

## License

MIT
