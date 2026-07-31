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

## Data-source failures during main_condor.py (observed live, 2026-07-30)

Real log from a live `main_condor.py` session with an open position:

```
[10:57:14] (dhan) NIFTY spot 24350.7
  Open condor: short CE 24500.0 / short PE 23850.0  MTM P&L: unavailable this cycle
  [data source] dhan failed, falling back: 429 Client Error: Too Many Requests for url: https://api.dhan.co/v2/optionchain
  [data source] nse failed, falling back: 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  Error this cycle (will retry next cycle): 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
```

Recurred repeatedly through the session, both tiers failing together on
several cycles (`MTM P&L: unavailable this cycle`), meaning the open
condor went unmarked and unmonitored for those cycles. Not a false
action (condor_tracker.update_position simply skips a cycle it has no
snapshot for -- confirmed safe, no bad write), but real monitoring
blind-spots on a position with live capital risk, and worth fixing
before relying on this unattended.

Two independent causes, already diagnosed:

**1. Dhan 429 (rate limit).** Dhan's option-chain limit is documented
as 1 request/3s **per account/token**, not per process. Three processes
now poll the same Dhan credentials independently with no cross-process
coordination: `main_live.py` (30s + a 5s fast-check), `main_condor.py`
(5 min), and `main_directional_spread.py` (30s, added 2026-07-29).
`resilient_source.py`'s tier-cooldown bookkeeping (`_last_failure`) is
in-memory and per-process, so it can't see what the other two processes
are doing. Adding the third poller at the same cadence as the momentum
scanner's own frequent polling is the likely tipping point.

  Fix directions, not yet built:
  - Simplest: widen `main_directional_spread.py`'s
    `POLL_INTERVAL_SECONDS` (currently 30, matching the momentum
    scanner) -- it doesn't need reaction speed matching intraday price
    action, since its entry signal is a bias score that doesn't change
    within seconds.
  - Real fix: a small file-based, cross-process request-spacing
    mechanism (a shared "timestamp of last Dhan request" state file
    that any process checks/updates atomically before firing, sleeping
    if needed) so the combined rate across all three processes
    respects Dhan's limit regardless of how many are running.

**2. NSE 404 (fallback tier blocked).** Tested the exact endpoint
directly: the cookie warm-up GET to the NSE homepage itself now returns
403 with zero cookies set, before the API call is even reached. The
"404" body served on the API call is a bot-challenge page, not a
genuine NSE 404. NSE's anti-bot detection has tightened beyond what the
current `requests`-based approach (User-Agent + cookie warm-up) can
satisfy -- likely TLS/JA3 fingerprinting or similar, which `requests`
can't replicate. `nse_source.py`'s own docstring already flagged this
endpoint as unofficial and liable to start blocking without notice.

  Not attempting a code fix that tries harder to look like a browser --
  that starts crossing into bot-detection evasion, which is out of
  scope regardless of urgency. If this tier needs to be reliable, the
  real fix is a licensed/paid data feed, not a better disguise for an
  unofficial endpoint. Until then: when BOTH tiers fail together on an
  open position, that's a real blind spot worth a human glancing at the
  log for, especially over an unattended overnight hold.

## Parameter tuning (lower priority, not safety-critical)

Several thresholds across the codebase are explicitly documented inline
as "starting assumptions, not researched optima" — `config.py`'s
`SMART_MONEY_NEUTRAL_BAND`, `NEWS_RISK_ELEVATED_THRESHOLD`,
`VOLUME_PROFILE_BIN_POINTS`, and `config_condor.py`'s
`HEDGE_DISTANCE_POINTS` among them. Worth revisiting once enough real
sessions have accumulated to tune them against actual data rather than
guesses — see each config comment for the specific reasoning already
documented there.
