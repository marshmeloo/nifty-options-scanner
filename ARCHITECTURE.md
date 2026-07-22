# Architecture

This is a linear decision-support pipeline, run either once against a CSV
snapshot (`main.py`) or continuously against live data (`main_live.py`).
No component places an order — every stage produces a recommendation for
manual review.

```
                 ┌─────────────────────┐
                 │   dhan_source.py     │   Dhan API: option chain,
                 │   data_source.py     │   spot/VWAP/PCR snapshot,
                 │                      │   intraday candles (CSV in test mode)
                 └──────────┬───────────┘
                            │  Snapshot (models.py)
                            ▼
                 ┌─────────────────────┐
                 │     scanner.py       │   Scores every strike:
                 │  + price_action.py   │   OI buildup/unwinding, IV
                 │                      │   percentile, PCR, VWAP dev,
                 │                      │   order blocks, FVGs, S/R,
                 │                      │   sweeps, trend, RSI/ROC
                 └──────────┬───────────┘
                            │  Setup(s), ranked by score
                            ▼
                 ┌─────────────────────┐
                 │  plan_generator.py   │   Concrete entry / target /
                 │                      │   stop / lot size / invalidation
                 └──────────┬───────────┘
                            │  Plan
                            ▼
                 ┌─────────────────────┐
                 │   risk_checker.py    │   Per-trade risk %, total
                 │                      │   exposure, daily-loss circuit
                 │                      │   breaker -> APPROVE / REJECT
                 └──────────┬───────────┘
                            │  Verdict
                            ▼
                 ┌─────────────────────┐
                 │   trade_tracker.py   │   If approved & score clears
                 │                      │   the conviction bar: freeze
                 │                      │   the plan, track it to close,
                 │                      │   journal the outcome, and use
                 │                      │   recent outcomes to nudge
                 │                      │   future scoring by tag.
                 └──────────┬───────────┘
                            │
                            ▼
                 logs/trade_journal.jsonl  (one line per closed trade)
```

## Why a tracker sits on top of the scanner

The scanner re-evaluates the *entire* chain every cycle — correct for
**finding** setups, wrong for **following** one. Without `trade_tracker.py`,
"highest-scoring option this cycle" silently becomes a new plan every
poll interval even when it's really the same setup drifting. The tracker
enforces:

- A conviction bar to open a trade (`MIN_CONVICTION_SCORE_TO_TRACK`) well
  above the scanner's watchlist threshold.
- A cap on new trades per day (`MAX_NEW_TRADES_PER_DAY` — currently set
  very high / effectively uncapped, since the system is in a training/
  evaluation phase and more trades means faster sample-size growth).
- Frozen entry/target/stop once a trade is opened — never silently
  recalculated mid-trade.
- A plain-language "lesson" appended to the journal on every close.

## The tag-adjustment loop (not machine learning)

`trade_tracker.py` looks up how a candidate's `reason_tags` (e.g.
`long_buildup`, `fvg`, `support`) have performed over the last
`JOURNAL_LOOKBACK_FOR_LEARNING` journal entries, and nudges the score up
or down accordingly — but only once a tag has at least
`MIN_TAG_SAMPLES_FOR_ADJUSTMENT` outcomes to be trustworthy. This is a
rule-based win-rate lookup, not a trained model: "keep a spreadsheet of
what worked and lean on it a little."

## Data flow: OI + price into a directional read

Raw OI% change alone doesn't say whether buyers or writers are behind a
move — combining it with premium direction does. `dhan_source.py`
classifies every strike into one of four cases (see `_classify_buildup`),
scored in `config.py`:

| Price | OI | Classification | Read on this contract |
|---|---|---|---|
| up | up | Long buildup | Bullish |
| up | down | Short covering | Bullish |
| down | up | Short buildup | Bearish |
| down | down | Long unwinding | Bearish |

## Live loop timing

`main_live.py` polls every `POLL_INTERVAL_SECONDS` (30s) between market
open and close (9:15–15:30 IST). OI and IV don't move meaningfully faster
than that, so polling faster would just add noise and API load. Every
session's output is also written to `logs/nifty_scan_YYYYMMDD.log`.

## Chain-wide OI analytics vs. per-strike buildup

`dhan_source._classify_buildup` answers "is *this contract's* OI/price
move bullish or bearish." `oi_analytics.py` sits alongside it and answers
a chain-wide question instead: where does aggregate OI say price is
likely to gravitate (Max Pain) or struggle (call/put walls), and is fresh
OI this session skewed to the call or put side (net delta OI). Both feed
off the same `chain: list[OptionQuote]`, computed once per snapshot and
attached at `snapshot.oi_analysis` regardless of which source produced
the snapshot.

## Data source tiering

`resilient_source.py` sits between `main_live.py` and the three concrete
sources (`dhan_source`, `nse_source`, `tradingview_source`). It tries the
primary tier every cycle rather than latching onto a fallback
permanently, since Dhan/NSE issues are often transient (rate limit
window, expired token). A short per-tier cooldown avoids retrying a
source that just failed on every single 30s cycle. TradingView is
structurally different from the other two — it has no option chain at
all — so it can only ever backstop spot/candles, not
`get_nifty_snapshot()`; the module is explicit about that boundary rather
than faking a chain from it.

## Extending this

- **New data source**: add a module alongside `dhan_source.py`/
  `data_source.py` that returns a `models.Snapshot`; nothing downstream
  needs to change.
- **New signal**: add it to `scanner.py` or `price_action.py`, give it a
  `reason_tag`, and the tracker's win-rate adjustment picks it up
  automatically once it has enough samples.
- **New risk rule**: add it to `risk_checker.py`; it only needs a `Plan`
  and current exposure/loss state.
