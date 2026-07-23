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

```bash
export DHAN_CLIENT_ID="your-client-id"
export DHAN_ACCESS_TOKEN="your-jwt-access-token"
python3 main_live.py
```

Windows:
```cmd
set DHAN_CLIENT_ID=...
set DHAN_ACCESS_TOKEN=...
python3 main_live.py
```

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
