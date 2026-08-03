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

## Order flow (optional, new 2026-08-02)

Not wired into any strategy's decisions yet — read-only market data,
useful on its own for watching the live book and measuring real spreads.

| Command | Purpose | When |
|---|---|---|
| `python3 orderflow_feed.py --strike-range 300` | WebSocket feed: live bid/ask book + spread recording | Optional, separate terminal, any time market is open |
| `python3 orderflow.py` | One-line health check of the feed (LIVE/STALE, contracts receiving) | Anytime the feed is running |
| `python3 spread_study.py` | Analyse a session's recorded spreads (by phase, by premium band) | After a session with the feed running |

## Reviewing / approving

| Command | Purpose | When |
|---|---|---|
| `python3 approve_orders.py` | Review & approve/reject anything staged | Whenever something is pending |
| Open `dashboard/trade_journal_dashboard.html` in a browser | Post-trade stats (win rate, capture %) | Anytime — drag in `logs/trade_journal.jsonl` |

## Iron condor strategy (separate, optional)

| Command | Purpose | When |
|---|---|---|
| `python3 main_condor.py` | Run the condor strategy loop | Separate terminal, any day flat (any day works now, not just after expiry) |
| `python3 open_approved_condor.py` | Actually open a condor once approved | After approving via `approve_orders.py` |

## Backtesting / research (offline, no live impact)

| Command | Purpose | When |
|---|---|---|
| `python3 historical_source.py --from YYYY-MM-DD --to YYYY-MM-DD` | Backfill historical option chains from Dhan's Expired Options Data | Building/extending the backtest dataset |
| `python3 shadow.py` | Backtest momentum over recorded history | After a config/scoring change |
| `python3 shadow_directional_spread.py` | Backtest directional spread (multi-day, expiry-bounded) | After a config change |
| `python3 shadow_condor.py` | Backtest iron condor | After a config change |
| `python3 sweep_threshold.py` / `sweep_spread_config.py` / `sweep_condor_config.py` | Grid-search a strategy's parameters against recorded history | Before adopting a new config — never adopt from a single run |

## Debugging / offline only

| Command | Purpose | When |
|---|---|---|
| `python3 main.py` | One-shot test run against sample CSV data | Offline testing, no API key needed |
| `python3 main_live.py` | Live scanner directly, **no auto-restart** | Debugging only — use `supervisor.py` otherwise |

Everything else in the repo is a supporting module, not something you run directly.
