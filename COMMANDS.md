# Commands Reference

Set once per terminal before running anything live:
```
set DHAN_CLIENT_ID=...
set DHAN_ACCESS_TOKEN=...
```
Dhan tokens are short-lived (~24h) — refresh before market open, not mid-session
(pulling/restarting mid-session has bitten this project before, see README).

## What's actually running (as of 2026-08-02)

Three independent strategies, each its own process, sharing only the Dhan
rate limiter. Run whichever ones you're trading today, each in its own
terminal.

| Strategy | Live config version | Notes |
|---|---|---|
| Momentum (`main_live.py`) | `SCORING_MODE = "momentum_only"` in `config.py` | Trades ~5x/day now (was ~1x/day under the old scorer). See README's 2026-08-02 entry. |
| Directional spread (`main_directional_spread.py`) | premium 40-70 / hedge 100 in `config_directional_spread.py` | Re-tuned 2026-08-02; old values are in that file's own comment if you need to revert. |
| Iron condor (`main_condor.py`) | unchanged | Sweep this same day found nothing worth adopting — see BACKLOG.md. |

Not yet forward-validated — both are in-sample backtest results. Watch
early live sessions against the documented expectations before trusting
the backtested totals (see README/BACKLOG 2026-08-02 entries for what to expect).


## Daily use

| Command | Purpose | When |
|---|---|---|
| `python3 supervisor.py` | Run the momentum scanner (auto-restarts on crash/freeze) | **Primary way to run momentum live, every trading day** |
| `python3 main_directional_spread.py` | Run the directional spread strategy loop | Separate terminal, any day bias is strong enough |
| `python3 premarket.py` | Generate today's pre-market brief | Before 9:15 AM |
| `python3 dashboard_server.py` | Local live dashboard → http://127.0.0.1:8787 | Optional, separate terminal |
| `python3 watchdog.py` | Warns if the scanner goes silent | Optional, separate terminal |

## Sentinel v1.1-dev (candidate, paper-tracking only — added 2026-08-15)

See `STRATEGY_VERSIONS.md` for the full Anchor/Sentinel registry and
why this is a separate process rather than a flag on the live one.
Identical signal pipeline to Anchor's momentum, plus a correlated-
cluster cap (200pt/30min). Runs alongside Anchor, never instead of it
— own state, own journal, own log, own decision log, tagged
`strategy_name: "Sentinel"` in every entry so the P&L dashboard
(`/pnl`) can compare the two on real data without pooling them.

**Wired into `automation/start_trading.ps1` as of 2026-08-16** — both
scripts below now start automatically every morning alongside the
other nine, no manual launch needed. Still fine to run either by hand
(e.g. for a standalone test) with the commands below.

| Command | Purpose | When |
|---|---|---|
| `python3 main_live_sentinel.py` | Sentinel, NIFTY | Runs automatically via `start_trading.ps1`; manual command for standalone testing |
| `python3 main_live_banknifty_sentinel.py` | Sentinel, Bank Nifty | Runs automatically via `start_trading.ps1`; manual command for standalone testing. **Cluster-cap values are NIFTY-backtested only, not yet verified for Bank Nifty** — see that file's own module docstring. |

## Order flow (optional, NIFTY added 2026-08-02, Bank Nifty added 2026-08-16)

Not wired into any strategy's decisions yet — read-only market data,
useful on its own for watching the live book and measuring real spreads.
`decision_log.jsonl` records `book_imbalance`/`total_quantity_imbalance`
for future research, never gates a trade decision. Two independent feed
processes, one per index — each its own WebSocket connection, state file,
and spread-recording directory; neither shares anything with the other.

| Command | Purpose | When |
|---|---|---|
| `python3 orderflow_feed.py --strike-range 300` | WebSocket feed, NIFTY: live bid/ask book + spread recording | Optional, separate terminal, any time market is open |
| `python3 orderflow_feed_banknifty.py --strike-range 600` | WebSocket feed, Bank Nifty: same, own state file. `--strike-range 600` is a first-pass estimate (double NIFTY's, matching Bank Nifty's wider strike spacing), not independently tuned | Optional, separate terminal, any time market is open |
| `python3 orderflow.py` | One-line health check of the NIFTY feed (LIVE/STALE, contracts receiving) | Anytime the feed is running |
| `python3 spread_study.py` | Analyse a session's recorded NIFTY spreads (by phase, by premium band) | After a session with the feed running |

## Reviewing / approving

| Command | Purpose | When |
|---|---|---|
| `python3 session_summary.py` | End-of-day digest -- hand this to a fresh Claude session, not raw logs | After market close |
| `python3 approve_orders.py` | Review & approve/reject anything staged | Whenever something is pending |
| Open `dashboard/trade_journal_dashboard.html` in a browser | Post-trade stats (win rate, capture %) | Anytime — drag in `logs/trade_journal.jsonl` |

## Iron condor strategy (separate, optional)

| Command | Purpose | When |
|---|---|---|
| `python3 main_condor.py` | Run the condor strategy loop | Separate terminal, any day flat (any day works now, not just after expiry) |
| `python3 open_approved_condor.py` | Actually open a condor once approved | After approving via `approve_orders.py` |

## Pure price-action strategy (separate, optional, added 2026-08-04)

Structure-only signal (no OI/IV/PCR) -- see `price_structure.py`'s
docstring for the entry rule. Runs BOTH timeframe pairs
(`daily_hourly` and `intraday`, `config_price_action.ACTIVE_PAIRS`) in
the same process, each with its own independent position. Backtest
evidence (2-year, 497 days) found only 19-24 distinct qualifying setups
and a 26% max drawdown on a Rs 20k account walk -- mildly positive, not
proven; see BACKLOG.md's 2026-08-04 entry before trusting its size.

| Command | Purpose | When |
|---|---|---|
| `python3 main_price_action.py` | Run the price-action strategy loop | Separate terminal, any time market is open |
| `python3 open_approved_price_action.py` | Actually open a position once approved | After approving via `approve_orders.py` (only needed if `AUTO_APPROVE_NEW_POSITIONS = False`) |

## Backtesting / research (offline, no live impact)

| Command | Purpose | When |
|---|---|---|
| `python3 historical_source.py --from YYYY-MM-DD --to YYYY-MM-DD` | Backfill historical option chains from Dhan's Expired Options Data | Building/extending the backtest dataset |
| `python3 shadow.py` | Backtest momentum over recorded history | After a config/scoring change |
| `python3 shadow_directional_spread.py` | Backtest directional spread (multi-day, expiry-bounded) | After a config change |
| `python3 shadow_condor.py` | Backtest iron condor | After a config change |
| `python3 shadow_price_action.py --pair both --rr-sweep 1.0,1.5,2.0,2.5,3.0,4.0` | Backtest price-action, both timeframe pairs, sweep target R:R | After a config change |
| `python3 sweep_threshold.py` / `sweep_spread_config.py` / `sweep_condor_config.py` | Grid-search a strategy's parameters against recorded history | Before adopting a new config — never adopt from a single run |

## Debugging / offline only

| Command | Purpose | When |
|---|---|---|
| `python3 main.py` | One-shot test run against sample CSV data | Offline testing, no API key needed |
| `python3 main_live.py` | Live scanner directly, **no auto-restart** | Debugging only — use `supervisor.py` otherwise |

Everything else in the repo is a supporting module, not something you run directly.
