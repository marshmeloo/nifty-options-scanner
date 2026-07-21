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
