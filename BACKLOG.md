# Backlog — before going live with real capital

Things that are working and acceptable during the evaluation/testing
phase, but worth revisiting before real money is on the line.

## Fast position-check: lightweight LTP endpoint (added 2026-07-27)

`main_live.py`'s `check_open_trades_fast()` (runs every
`config.FAST_CHECK_INTERVAL_SECONDS`, 5s by default, to catch a
target/stop spike between full 30s scan cycles) currently re-fetches the
**full option chain** each time — simple, reuses fully-tested code, but
heavier than it needs to be at a fast cadence.

Dhan has a lighter endpoint built for exactly this: `POST
/v2/marketfeed/ltp`, which returns just the LTP for a specific list of
security IDs (see `dhanhq.co/docs/v2/market-quote/`). Switching to it
requires:
1. Downloading and parsing Dhan's instrument master file (maps every
   contract to its security ID) — a new data source we don't currently use.
2. Resolving each open trade's (strike, expiry, option_type) to its
   security ID via that master file.
3. Using those IDs in the `/marketfeed/ltp` call instead of the full
   `/optionchain` fetch.

Worth doing before relying on fast polling with real capital, both for
efficiency and to reduce load against Dhan's rate limits.

## Broker-side protective stop-loss

Discussed at length (see README's "silent freeze" incident notes): even
with `supervisor.py` auto-restarting on crash/freeze, there's a gap
between "the process dies" and "the supervisor notices" where an open
position has zero protection. The only way to close that gap completely
is a stop-loss order sitting on the exchange itself (a Dhan GTT order
placed at trade entry) — a deliberate, not-yet-made decision to start
placing real orders, since everything currently here is analytics/
tracking only.

## Order flow

Needs Dhan's WebSocket Live Market Feed (separate paid Data API
subscription, ₹499+tax/month) and a genuinely different process
architecture — not a REST-polling module like everything else here.
Scoped out for now; revisit once the above two items are settled.

## Parameter tuning (lower priority, not safety-critical)

Several thresholds across the codebase are explicitly documented inline
as "starting assumptions, not researched optima" — `config.py`'s
`SMART_MONEY_NEUTRAL_BAND`, `NEWS_RISK_ELEVATED_THRESHOLD`,
`VOLUME_PROFILE_BIN_POINTS`, and `config_condor.py`'s
`HEDGE_DISTANCE_POINTS` among them. Worth revisiting once enough real
sessions have accumulated to tune them against actual data rather than
guesses — see each config comment for the specific reasoning already
documented there.
