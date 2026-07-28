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
| `data_source.py` | CSV-based snapshot loader (offline/testing) |
| `models.py` | Shared dataclasses (snapshot, setup, plan, verdict) |
| `config.py` | Every threshold and risk parameter — tune to your own setup |

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

**Opening a position is a two-step, human-approved process, on purpose**:
1. `main_condor.py`, running on a 5-minute poll during market hours,
   detects the first trading day after an expiry, scans for a complete
   4-leg condor, risk-checks it (`condor_risk_checker.py`: minimum net
   credit, max capital-at-risk cap, only one position at a time), and
   -- if approved -- **stages it** via `trade_staging.py`'s
   `stage_advisory()`, it does NOT open it automatically.
2. Review it: `python3 approve_orders.py`. Once approved, run
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
files (catches syntax errors), lints with `ruff`, and does an import
sanity check across all modules on Python 3.10–3.12.

## Disclaimer

This is decision-support tooling for personal use — it prints recommendations
for manual review and **does not execute trades**. It is not financial advice.
Options trading carries significant risk of loss; use your own judgment and
consult a SEBI-registered advisor before trading.

## License

MIT
