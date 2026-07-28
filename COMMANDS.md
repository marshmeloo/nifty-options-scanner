# Commands Reference

Set once per terminal before running anything live:
```
set DHAN_CLIENT_ID=...
set DHAN_ACCESS_TOKEN=...
```

## Daily use

| Command | Purpose | When |
|---|---|---|
| `python3 supervisor.py` | Run the live scanner (auto-restarts on crash/freeze) | **Primary way to run live, every trading day** |
| `python3 premarket.py` | Generate today's pre-market brief | Before 9:15 AM |
| `python3 dashboard_server.py` | Local live dashboard → http://127.0.0.1:8787 | Optional, separate terminal |
| `python3 watchdog.py` | Warns if the scanner goes silent | Optional, separate terminal |

## Reviewing / approving

| Command | Purpose | When |
|---|---|---|
| `python3 approve_orders.py` | Review & approve/reject anything staged | Whenever something is pending |
| Open `dashboard/trade_journal_dashboard.html` in a browser | Post-trade stats (win rate, capture %) | Anytime — drag in `logs/trade_journal.jsonl` |

## Iron condor strategy (separate, optional)

| Command | Purpose | When |
|---|---|---|
| `python3 main_condor.py` | Run the condor strategy loop | Separate terminal, day after weekly expiry |
| `python3 open_approved_condor.py` | Actually open a condor once approved | After approving via `approve_orders.py` |

## Debugging / offline only

| Command | Purpose | When |
|---|---|---|
| `python3 main.py` | One-shot test run against sample CSV data | Offline testing, no API key needed |
| `python3 main_live.py` | Live scanner directly, **no auto-restart** | Debugging only — use `supervisor.py` otherwise |

Everything else in the repo is a supporting module, not something you run directly.
