# Backlog — before going live with real capital

Things that are working and acceptable during the evaluation/testing
phase, but worth revisiting before real money is on the line.

## CLOSED, NOT ADOPTED: "Hero-Zero" -- deliberately picking the cheapest option is WORSE than picking a random one nearby, and expiry day confers no measurable edge (added 2026-08-26)

Follow-through on "SCOPED, NOT BUILT: Hero-Zero" --
`research/hero_zero_study.py`, full 2020-08..2026-07 reconstruction
(1,485 days, 3,693 candidate legs). Feasibility probe first (see the
script's own docstring): Rs1-5 candidates cluster toward the edge of
the ATM+/-10 fetch window, the same zone the 2026-08-04 corruption
study flagged, so selection was capped at 300pts from spot to stay in
the measurably cleaner band -- at the cost of dropping the very
deepest candidates and undersampling high-IV expiry days where nothing
inside 300pts is cheap enough. Stated as a real limitation, not fixed.

DESIGN: at 10:25 each day, for CE and PE independently, compare (a)
"hero_zero" -- the farthest-OTM strike inside 300pts whose LTP sits in
Rs1-5, against (b) "random" -- any strike in the same 300pt band,
any premium, same day. Run on EVERY trading day, not just expiry days,
splitting by whether that contract expires that same day (DTE=0) or
not (DTE 1-5) -- free from the reconstruction's own weekly-cycle
structure, no extra machinery needed. Real Indian options costs
(costs.py: flat per-order brokerage + STT + exchange + GST) applied to
every leg at 1 lot.

RESULT -- every comparison points the SAME direction, and it is the
OPPOSITE of the idea's premise:

    group                 n   meanRet%  win%   >=5x   >=10x  lost80%+  netP&L/lot   t
    expiry, hero_zero    514    -52.75%  1.6%  4.47%   2.72%    97.7%     -Rs107     -3.07
    expiry, random       598    -35.66%  9.7%  8.19%   2.51%    86.8%     -Rs128     -1.15
    non-expiry, hero_zero 209   -26.97% 15.3%  0.96%   0.00%    12.9%     -Rs100     -9.85
    non-expiry, random  2,372    -2.93% 34.2%  0.63%   0.08%     2.4%     -Rs156     -2.69

  - **Is expiry day special?** No -- hero_zero on expiry vs non-expiry:
    edge -25.78pp, z=-1.36. Point estimate goes the WRONG way (expiry
    day is worse, not better) and isn't even significant.
  - **Does deliberately picking the cheapest candidate help?** No --
    the opposite. vs random at the same distance, expiry day: edge
    -17.09pp (z=-0.79, not significant); non-expiry day: edge -24.04pp
    (z=-4.94, clearly significant). The cheap-filtered pick LOSES to a
    same-band random pick everywhere it was tested, worst where there
    was enough data to tell (non-expiry).
  - Win rate on the actual hero_zero/expiry combination -- the
    strategy exactly as described -- is **1.6%**: 8 wins in 514
    legs. 97.7% of legs lost 80% or more of the premium paid.
  - All four groups are net-negative after realistic costs; two of
    four (expiry_hero_zero, nonexpiry_hero_zero, nonexpiry_random)
    clear their own t-bar for "significantly negative," not merely
    unprofitable by chance.

WHY THE FILTER BACKFIRES, PLAUSIBLY: a strike whose premium has
ALREADY decayed to Rs1-5 by mid-morning has already lost most of the
gamma/vega sensitivity a tail spike needs to work through -- it isn't
"about to explode," it's already mostly dead. A random pick in the
same distance band sometimes catches a strike that still has real
premium (and therefore real sensitivity) left, which is why random
beats the deliberate cheap-pick everywhere. The retail intuition
("small known loss, occasional huge win") has the causality backwards:
picking for smallness of loss selects against the very property that
would produce the win.

CAVEATS carried forward, not resolved: the 300pt cap means this
doesn't speak to the very deepest lottery tickets some retail traders
chase beyond that distance (though those sit in the flagged-corrupt
zone, so testing them cleanly needs a data fix first, see 2026-08-04's
entry); tail-event percentages (>=5x, >=10x) are thin at ~500 legs per
group, so their precision is limited even though the headline
comparisons (mean return, win rate, net P&L) are not.

VERDICT: closed, not adopted. The idea as scoped does not clear this
project's bar (real edge vs. a control, positive after costs) on
either of its two testable claims.

## CLOSED, NOT ADOPTED: ORB on stocks in play -- the entry rule loses to a coin flip; the RVOL filter is real but it's the SAME decliner signal already flagged elsewhere (added 2026-08-26)

Follow-through on "RESEARCHED, NOT BUILT: ORB on stocks in play" --
`research/orb_stocks_in_play_study.py`, run on the full 208-symbol,
~9-year backfill (2017-08..2026-08, 2,219 RVOL-eligible trading days).
OR=15min only (matches the RVOL selection window; see the script's own
docstring for why the full 4-length grid wasn't run). Two selection
sizes (top10, top20 by cross-sectional RVOL rank each day), each with
its own random-N control, x 4 real entry rules + 3 benchmarks.

REAL, ROBUST FINDING: the RVOL FILTER ITSELF adds a genuine edge.
Same entry rule, RVOL-ranked selection vs random selection at the same
N: +0.030 to +0.044R per trade, z = +3.56 to +5.32 (3 of 4 entries;
`or_direction` shows none). This is a direct, positive confirmation of
the literature's central claim ("the filter IS the strategy") on this
dataset -- worth keeping in mind for anything else that wants a
volume-based stock universe filter.

BUT: ORB's actual ENTRY RULE does not clear its own bar. Every real
entry (breakout, breakout_or_direction, close_confirm, or_direction)
UNDERPERFORMS a coin-flip entry with the identical opening-range stop
geometry, within the SAME RVOL-selected population -- top10/top20 vs
random z from -2.17 to -6.21, all in the wrong direction. On the
random-N selection the real entries are merely indistinguishable from
random (z -3.15 to +0.04), not better. Waiting for a break of the
opening range, in any of the four published forms, is not adding
signal here -- if anything the entries fight whatever the real edge is.

WHAT THE REAL EDGE ACTUALLY IS: ALWAYS_SHORT (short every RVOL-selected
name at the open, stop at the OR high, no entry logic at all) is the
single best performer in every selection -- mean R +0.22 to +0.31,
dwarfing every directional entry rule. That is a persistent short bias
in high-RVOL Indian F&O names, i.e. the SAME "pullback on high-RVOL
DECLINERS" signal the sibling stocks-in-play study already found and
flagged as cost-marginal, not a new discovery specific to ORB.

COSTS: break-even slippage for the entry rules and ALWAYS_SHORT runs
~2-8bps (fixed Rs5,000 risk/trade, real Indian intraday-equity
statutory rates from stock_costs.py). Measured real spreads
(stock_spread_recorder.py, only 4 trading days recorded so far):
universe median ~3.3bps, selected high-RVOL names sometimes much lower
(one example: DIXON at 0.69bps). Not a clean kill the way the QQQ
literature replication was (break-even far below the realistic
spread) -- closer, worth re-checking once the spread recorder has more
days -- but the entry-rule question is already closed regardless of
the cost answer, since the entry loses to random before costs are even
applied.

OOS DECAY, another caution flag: every positive number here weakens
substantially out-of-sample (roughly halves or worse; top10 RANDOM's
OOS mean R is ~0, down from +0.18 in-sample). Consistent with a real
but decaying historical bias, not a stable structural edge.

METHODOLOGY NOTE: orb.py's RANDOM entry seeds on `day` alone, which is
fine for the single-instrument index study it was built for but gives
every stock the SAME coin-flip direction on a shared day in this
multi-symbol context. Caught before trusting the first run (RANDOM's
own mean R was inflated); fixed by seeding on `symbol:day` while still
recording the true calendar day for the OOS split.

VERDICT: closed. Does not add a new validated strategy -- confirms the
RVOL filter's own value (useful elsewhere) and reconfirms the existing
decliner-pullback signal through ORB's stop/cost lens, while directly
refuting "waiting for an opening-range breakout has signal" on this
dataset. Anyone revisiting the short-bias finding should treat it as
the same thread as the stocks-in-play pullback study, not a new one.

## Order-flow imbalance: real but weak, thin, and horizon-inconsistent signal -- NOT ready to gate on (added 2026-08-26)

Follow-up to "Order flow: wired for reliability, book_imbalance recorded
but NOT gated on" -- that entry deliberately left the predictive
question open until there was real recorded data to test, "the same
forward-return treatment component_study.py applied to the momentum
scorer." `research/order_flow_predictive_study.py` is that treatment.

METHOD: can't replay this historically the way component_study.py does
(book_imbalance comes from Dhan's live WebSocket depth, not anything in
the reconstructed chains) -- so this replays decision_log.jsonl's own
live candidate history instead. Every logged candidate already carries
book_imbalance, total_quantity_imbalance, and its own entry price;
forward return of THAT SAME CONTRACT is found by matching the same
(strike, option_type) key in a candidate list >= horizon minutes later.

DATA IS THINNER THAN IT LOOKED: decision_log.py has recorded the field
since 2026-08-07, but readings are only actually non-null starting
2026-08-17 -- root cause not chased down (tangential to this question).
Effective usable window is ~10 trading days (2026-08-17 -> 2026-08-26),
not the ~3 weeks the field's presence alone suggested.

RESULT (30min horizon, n=13,187 candidate rows):

    signal                      Q5-vs-Q1 z   pearson r (t)   shape
    book_imbalance (top-5)          +2.85    +0.0304 (+3.49)  clean, monotonic Q1->Q5
    total_quantity_imbalance        +3.11    +0.0352 (+4.04)  Q4 spikes above Q5, not monotonic

Both nominally significant, both directionally sane (more buy pressure
-> more positive forward return of that contract). But:

  - AT 15min HORIZON, book_imbalance is NOT significant (z=+0.82,
    non-monotonic) -- backwards from what a real order-flow signal
    should do (strongest close to the reading, decaying, not the
    reverse). total_quantity_imbalance holds up better at 15min
    (z=+4.77) but that inconsistency between the two signals is itself
    a flag.
  - SPLIT-HALF CHECK (first ~4 days vs last ~6 days of the 10-day
    window): book_imbalance's Q5-vs-Q1 lift is POSITIVE in both halves
    (directionally consistent) but only clears significance in the
    second, larger half (z=1.45 vs z=2.58) -- could be a real effect
    that needed more days to separate from noise, or could just be an
    artifact of unequal sample sizes. Ten trading days can't tell them
    apart.
  - Rows within a cycle are NOT independent (many candidates share the
    same underlying move) -- same simplification component_study.py
    also makes, but worth flagging here since the effect sizes are
    small (pearson r ~ 0.03-0.04) and pseudo-replication inflates z
    more than it inflates r.

STATUS: real, non-zero, directionally consistent signal -- but thin
(10 days), horizon-inconsistent, and not yet the kind of clean,
monotonic, stable result this project has required before adopting
anything else (cluster cap, direction-chase cooldown, profit-target
exit). NOT wired into scoring or a gate. Let the feed keep recording
and re-run this study in a few more weeks once there's 4-6x the data;
don't decide off 10 days the same way the expiry-day rule shouldn't
have been decided off one trade.

## TODO: correct config.py / STRATEGY_VERSIONS.md's breakeven-arm citation -- not verifiable, and partly contradicted (added 2026-08-26)

`config.BREAKEVEN_ARM_R`'s comment and `STRATEGY_VERSIONS.md` both cite a
2026-08-15 backtest: net P&L +Rs16.6L -> +Rs39.2L (STT-only) / +Rs14.3L
-> +Rs36.8L ("real spread-inclusive"), drawdown 44.8% -> 15.8%, "never
made a single one of 73 months worse." The script that produced those
numbers was never committed -- only the finished HTML report survives
(`research/nifty_momentum_breakeven_0.5R.html`).

`research/breakeven_arm_study.py` (committed 2026-08-25, rerunnable)
reproduces the same comparison from scratch and does not match it:

  - The "spread-inclusive" figure cannot be honestly reproduced from
    this data AT ALL -- reconstructed historical quotes carry no real
    bid/ask book (`has_book` is False on every quote, checked directly),
    so `config.USE_BID_ASK_FILLS` has zero effect on any backtest run
    against this dataset. The old 36.8L figure should be compared
    against nothing this project can currently produce.
  - Even against the fair comparison (STT-only, +Rs39.2L), the
    no-rule baseline matches almost exactly (Rs16.57L reproduced vs
    Rs16.6L documented) but the rule's benefit does not: +Rs1.9L
    reproduced vs the documented +Rs22.6L.
  - The "never a worse month" claim does NOT hold in the rebuild: 31 of
    72 months are worse with the rule, not zero.

Not urgent enough to have blocked deploying `main_live_onetrade.py`
this session, since the rule is still net-positive and cuts drawdown in
the reproduction too -- but the specific number and the "never worse"
guarantee currently in both docs are unverified at best, partially
false at worst. Fix: point both docs at
`research/breakeven_arm_study.py`'s own numbers instead, with a note on
why the old figure can't be reconciled (see above). Deferred at the
user's request 2026-08-26 -- do this later, not now.

## SCOPED, NOT BUILT: "Hero-Zero" expiry-day deep-OTM lottery trade (added 2026-08-24)

Raised for later, not built yet -- logging the shape of the question
so it doesn't need re-deriving.

### The idea

A popular Indian retail expiry-day play: buy deep OTM NIFTY/Bank Nifty
options (often both a far call and a far put together) while they are
trading near-worthless (Rs 1-5) on the weekly expiry day itself. Most
days they decay to zero -- the defined, small, known loss. Occasionally
a late move (news, a squeeze, gamma suddenly mattering as spot nears
that strike in the final hours) sends one leg from a couple of rupees
to tens of rupees, a large percentage move on a tiny base. No stop-loss
is typically used, since there is nothing left to stop out of once the
premium is already near zero.

### Why it's a real, testable question rather than a lottery-ticket dismissal

This project already has 3+ years of NIFTY and Bank Nifty options data
and expiry-day awareness (the existing conviction-bar discussion covers
some of the same "options behave differently on expiry" ground). A
proper test would need:

  - Historical premiums for strikes far enough OTM to be genuinely
    cheap (Rs 1-5 range) at some fixed point on expiry morning/midday --
    NOT the deepest strike available (that's arbitrary), but a rule
    tied to actual moneyness/premium level the same way this project's
    other selection rules are defined explicitly rather than eyeballed.
  - How often, and by how much, such a leg spikes before expiry close --
    the tail-event frequency and payoff size is the entire strategy,
    same as `stock_strategies.py`'s "runner" exit exists specifically to
    harvest a fat right tail rather than cap it.
  - A control: repeated small-premium purchases have a structurally
    negative expected value from theta alone absent a real tail --
    the "random deep-OTM buy" control this project has used everywhere
    else (index ORB, stocks-in-play) is the right comparison here too,
    not a raw win-rate.
  - Real costs at this premium level: brokerage is often a larger
    fraction of a Rs 2-3 premium than of a normal trade, which could
    dominate the result the way spread dominated stocks-in-play.

Not started. Comes after the stocks-in-play spread measurement finishes
(that one already has live data collection running) and the Hero-Zero
backtest, if pursued, should get the same rigor: real controls,
Bonferroni correction across any parameter sweep, out-of-sample check.

## Stocks in play: FIRST REAL RESULT -- pullback on high-RVOL decliners beats its controls, but is cost-marginal (added 2026-08-20)

Backfill finished: 208 F&O stocks, 155,234 symbol-days, 2023-08..2026-08,
zero failed fetches. Two studies run.

### 1. The distribution test -- "win large fast, lose small" is NOT supported as stated

152,527 stock-days, forward return from the 09:30 selection point to
the close, bucketed by opening relative volume:

    RVOL     n         mean      skew   win%   W/L    >+2%    <-2%
    <1     104,610   -0.042%    +0.36   46.7   1.05   7.40%   7.36%
    1-2     33,012   +0.012%    +0.48   47.8   1.10  10.21%   9.27%
    2-3      7,410   -0.018%    +0.23   47.4   1.07  12.32%  11.69%
    3-5      4,210   -0.030%    +0.40   46.2   1.11  13.52%  13.66%
    5+       3,285   -0.170%    +0.48   44.2   1.04  15.40%  18.39%

The skew IS positive throughout (+0.23 to +0.48), so the intuition
about a fat right tail has some basis. But the rest does not survive:
the mean return is ~zero or NEGATIVE and gets monotonically WORSE with
higher RVOL; the win/loss size ratio is 1.04-1.11, nowhere near "win
large, lose small"; and at RVOL 5+ the DOWNSIDE tail is fatter than the
upside (18.4% vs 15.4%). High relative volume by itself buys volatility
in both directions, not favourable asymmetry.

The one real asymmetry is directional: high-RVOL DECLINERS keep
declining (mean -0.213% after selection) while high-RVOL gainers go
nowhere (+0.017%). Same intraday short bias the index ORB study found
independently, which is mild cross-validation of both.

### 2. The strategy sweep -- one cell is genuinely alive

108 cells (RVOL threshold x direction x entry x exit), statutory costs
deducted, slippage left as the free variable. Headline metric is
BREAK-EVEN SLIPPAGE: the per-leg bps at which a cell nets zero.

Every real rule against its own controls, RVOL>=3 losers, fixed_r exit:

    pullback      n=1,317  meanR +0.1290  t=+7.33  BE 17.39 bps
    orb           n=1,574  meanR +0.0950  t=+5.74  BE 11.83 bps
    momentum      n=1,890  meanR +0.0676  t=+4.42  BE  7.58 bps
    always_short  n=1,890  meanR +0.0676  t=+4.42  BE  7.58 bps  CONTROL
    always_long   n=1,890  meanR -0.0687  t=-4.52  BE  0.00 bps  CONTROL
    random        n=1,890  meanR +0.0128  t=+0.84  BE  0.00 bps  CONTROL

Three things worth stating plainly:

  - **`momentum` is IDENTICAL to the always_short control**, in every
    bucket, every time. That is structural, not a bug: "enter in the
    opening move's direction" inside a direction-filtered bucket just
    IS "always short". So the momentum rule contributes exactly nothing
    beyond the selection. Anyone reporting it as a strategy would be
    reporting a control.
  - **`random` is ~0 everywhere** (BE 0.00-3.38), so the stop-plus-exit
    payoff geometry is not manufacturing the result -- which is the
    trap that killed index ORB.
  - **`pullback` beats every control in all six buckets**, typically by
    2-4x on break-even slippage, with t-stats of +6.9 to +9.3 against a
    Bonferroni bar of 3.31 for 54 non-control cells.

Break-even slippage also RISES monotonically with the RVOL threshold on
the losers side (9.56 -> 14.09 -> 17.39 bps at 1.2/2.0/3.0), which is
the dose-response you would want from a real effect rather than noise.

### 3. Out-of-sample -- holds, but the level swings hard

RVOL>=3 losers, fixed_r, three independent ~1-year periods:

    entry           Y1 2023-08..    Y2 2024-08..    Y3 2025-08..
    pullback        R+0.180 BE 13.1  R+0.026 BE 10.6  R+0.189 BE 26.2
    orb             R+0.149 BE 13.3  R+0.009 BE  4.4  R+0.137 BE 18.3
    always_short    R+0.076 BE  3.5  R+0.009 BE  0.4  R+0.119 BE 17.2
    random          R+0.014 BE  0.0  R+0.004 BE  0.0  R+0.020 BE  0.0

Positive in all three periods and ahead of controls in all three. But
Y2 is nearly flat (R +0.026), so this is regime-dependent, and Y3's
strength partly reflects a strong intraday short bias that lifts the
control too (always_short BE 17.2 that year).

### HONEST VERDICT: promising, NOT proven, and the cost gate is the issue

This is materially stronger than the index ORB null -- a real,
control-beating, multi-period effect with dose-response. It is not a
green light:

  - **The binding constraint is the WORST year, BE 10.6 bps.** Indian
    F&O stock half-spreads run roughly 1-5 bps on liquid names and
    5-15 bps on less liquid ones -- and an RVOL>=3 screen
    PREFERENTIALLY surfaces the unusual-activity, less-liquid end. So
    the worst year sits inside the plausible slippage range. That is
    the same failure mode that killed the published US version, and it
    has not been cleared here, only narrowed.
  - **Survivorship bias is real and unquantified** -- today's F&O list
    applied to history.
  - Spreads have not been MEASURED for these names; the backfill holds
    OHLCV only. Everything above is judged against plausible ranges,
    not data.
  - Position sizing, fill feasibility on 5-20 concurrent names, and
    what instrument would express this (stock, future, or notoriously
    illiquid stock options) are all untouched.

NEXT STEP if pursued, and it should be this one and not more parameter
sweeping: get real bid/ask for the selected names and replace the
break-even-slippage judgement call with a measurement. If measured
spreads exceed ~10 bps on the names this selects, the track closes like
index ORB did.

Code: `research/stocks_in_play.py` (selection + distribution),
`research/stock_strategies.py` (entry/exit variants + controls),
`research/stock_costs.py` (equity cost model + break-even slippage),
`research/stocks_in_play_sweep.py` (the sweep). Results in
`logs/stocks_in_play_study.json`, `logs/stocks_in_play_sweep.json`.
NOTHING ADOPTED -- no live code touched, this is not an equity strategy
in production.

## Platform research: TradeFinder's intraday feature set, and what is worth borrowing (added 2026-08-19)

Competitive scan of tradefinder.in (Indian retail intraday platform,
live since 2022-06) plus reference screenshots of a third-party
signal system. Intraday only -- swing features (Swing Spectrum,
Reversal Radar, Channel BO, 10/50-day BO) deliberately ignored.

### What their intraday product actually is

Their entire intraday stack is relative-volume + price momentum stock
selection, refined over a 3-year changelog. That is the SAME thesis as
this project's stocks-in-play track, which is mild validation of the
direction -- a company has sold it for three years and kept investing
in the core ranking metric.

Intraday-relevant features, from the changelog:

  - **R. Factor** -- proprietary composite momentum rank. Core engine
    rebuilt twice (2024-12, 2025-01) and later extended to rank SECTORS
    (2025-10) rather than just stocks.
  - **Intraday Boost** -- live momentum stock finder; filters added
    2025-01, a "Signal" direction section added 2025-07.
  - **Breakout Beacon** (2025-06) -- momentum surge + "big players
    piling in".
  - **5 & 10-minute momentum spikes** (2022-11, refined 2024-05) --
    sharp volume increase AND significant price move, together.
  - **Top Level / Low Level** (2022-12, refined 2024-11) -- stocks
    trading near the day's high/low.
  - **Contraction BO** (2022-09) -- breakout from a tight range.
  - **LOM (Loss of Momentum)**, short and long term (2022-09) --
    momentum EXHAUSTION, aimed at reversals.
  - **Day H/L Reversal** (2022-10) -- at day extremes but turning.
  - **Sector Scope / Sector Filters** (2024-03, 2025-10) -- sector
    contribution and heatmap, R-factor-weighted, to catch "where smart
    money is flowing early".
  - **Index Movers** (2024-03) -- which constituents are pushing or
    dragging an index.
  - Options-side: Option Apex (candle-by-candle position buildup),
    OI Clock, Index Alpha (backtested NIFTY/BankNifty option-buying
    signals).

### Honest read on it as EVIDENCE

Near zero. The changelog says "after thorough backtesting" repeatedly
and never once publishes a number -- no sample, no period, no
expectancy, no drawdown. That is marketing copy, not evidence, and it
is exactly the standard this project refuses for its own changes. Treat
the feature list as a well-informed source of HYPOTHESES to test, never
as validation that any of it works.

The accompanying YouTube references are weaker still: all three from
one channel (IntraSurgical, 387 subscribers, 108-306 views, one of them
a 15-second Short) with a Telegram link in the description, i.e. a
signals-service funnel. Titles confirm the topic is ORB/intraday but
carry no independent weight.

### Reference screenshots of a working signal system

More useful than the videos, because they show concrete PARAMETER
choices someone runs live:

  - Candidate filter at **RVOL >= 1.2** -- notably LOW next to the
    US literature's 2.0+ and the "top 20 by relative volume" of the
    Zarattini paper. Argues for sweeping a RANGE of RVOL thresholds
    rather than assuming a high bar.
  - Signals timestamped **09:20-09:25**, i.e. selection inside the
    first 10 minutes. This project's stocks_in_play currently uses a
    15-minute window; worth testing 5 and 10 too, which also matches
    TradeFinder's own 5/10-minute spike windows.
  - Trade structure **E / SL / T1 / T2 / T3 where T3 = "2R close"** --
    multi-target with a runner, not a single fixed target. Directly
    relevant to the "win large, lose small" hypothesis: a runner is
    how a fat right tail is actually harvested.
  - Separate LONG and SHORT books, both populated the same morning.
  - One candidate showed **RVOL 45.09**, far outside anything a
    20-day-baseline RVOL normally produces -- their RVOL is likely
    computed against a different baseline (longer window, or including
    pre-open). A reminder that "RVOL" is not a standard quantity and
    ours must be defined explicitly, as research/stocks_in_play.py does.

### What is worth borrowing, ranked

  1. **Sector context.** This project has NOTHING for sector rotation,
     and it is the one idea here that is both absent and plausibly
     load-bearing: a stock moving with its whole sector is a different
     bet from one moving alone. Cheap to add to the stocks-in-play
     study as a per-day feature once the backfill lands.
  2. **Shorter selection windows (5/10 min)** plus a swept RVOL
     threshold rather than a single assumed one.
  3. **Multi-target with a runner (T1/T2/2R-close)** as the exit
     family to test, since it is the construction that matches the
     hypothesis being tested.
  4. **Near-day-high/low** as a cheap additional filter.
  5. **Index Movers** -- interesting bridge between the existing index
     strategies and the stock work, but no clear use yet.

NOT worth borrowing: the options-side features (Option Apex, OI Clock)
duplicate what oi_analytics.py and the orderflow feed already do here.

Nothing adopted. This is a hypothesis list for the stocks-in-play
track, to be tested on the backfilled data like anything else.

## RESEARCHED, NOT BUILT: ORB on "stocks in play" (high relative-volume Indian stocks) (added 2026-08-19)

Raised after index ORB came back null: does ORB work on individual
momentum stocks selected by volume/gainer screens, rather than on the
index? Researched, feasibility-probed, not built. **Verdict: the one ORB
variant genuinely worth testing, but gated on a cost test that the
published evidence suggests it will probably fail.**

### Why this is a better question than the one already answered

Not a random re-roll of a settled result. The flagship US study
(Zarattini, Barbon & Aziz 2024) found UNFILTERED ORB across 7,000+
stocks LOST to buy-and-hold -- and that filtering to the top-20 stocks
each day by opening relative volume turned it into 1,637% total return,
41.6% IRR, Sharpe 2.81, near-zero beta. The filter IS the strategy.

When the index ORB study here closed as null (README 2026-08-19), the
stated reason was precisely that this filter has no index analogue --
"you cannot pick today's NIFTY out of a universe of NIFTYs." A stock
universe restores exactly the missing ingredient. So this asks a
different question, not the same one again.

### Feasibility: PROVEN, probed directly

  - Dhan's instrument master carries 9,846 NSE EQUITY rows and 1,256
    FUTSTK, giving a ~220-name F&O stock universe.
  - Historical 5-minute intraday candles ARE available per stock
    (RELIANCE probed at 2022-06-15, 2024-03-14, 2026-08-18 -- 75 bars
    each, correctly starting 09:15 once the 09:14 fromDate fix is used).
  - Multi-day range fetching works for equities as it does for the index
    (one call returned a full month, 1,725 bars / 23 days), so a 3-year
    backfill over 220 stocks is ~7,920 calls, roughly 7.7 hours of
    background API time -- large but a one-off.
  - NSE's own top-gainers / volume-spurts PAGES are NOT usable as the
    selection source: they are live snapshots with no history, and NSE
    blocks automated fetches (see nse_source.py's own docstring). The
    selection must be RECONSTRUCTED from stock data as it would have
    looked that morning -- which is the correct approach anyway, since
    it is the only way to guarantee no look-ahead.

### The evidence against, which is strong and specific

An independent replication of the sibling QQQ paper reproduced the
mechanics almost exactly (1,775 vs 1,795 trades, Sharpe 1.06 vs 1.12)
and then destroyed the practical claim:

  - **Break-even at ~2.2 cents/share of slippage.** The entire edge
    lives INSIDE QQQ's bid-ask spread; $138,639 of published profit
    became $4,860 under realistic costs.
  - **76% of the filtered strategy's profit came from 2022 alone** --
    a volatility-regime artifact, losing money in 2017, 2020 and early
    2023.
  - **Bootstrap Sharpe CI [0.05, 1.41] vs buy-and-hold [-0.03, 1.47]**
    -- overlapping; no robust portfolio-level edge despite significant
    per-trade statistics.

That replication targets the INDEX-ETF paper, not the stocks-in-play
one, so it is not a direct refutation. But the lesson transfers in the
WRONG direction: QQQ is among the most liquid instruments on earth, and
the edge still died inside its spread. Indian single stocks -- including
the mid-caps a relative-volume screen will preferentially surface,
because unusual volume IS the selection criterion -- have materially
wider spreads and worse impact costs than QQQ.

### Fit with this system: this would be a new instrument class

Checked: the live system trades index derivatives ONLY (OPTIDX /
NSE_FNO / IDX_I throughout). `instrument_master.py` is the single file
that even mentions equities, and only incidentally. There is no equity
data source, no equity universe handling, no per-stock scanning, and no
execution path for stocks.

Also unresolved and not a detail: WHAT would actually be traded.
Stock options (OPTSTK) exist but liquidity outside the largest names is
poor -- and buying options on a wide-spread underlying compounds the
exact cost problem that killed the replication. Stock futures or the
stock itself mean a different margin/capital model this project has
never touched. Plus capital fragments across 5-20 positions/day instead
of one index position.

### Recommendation: test it, but cost-gate FIRST

Worth pursuing, because it is the single documented condition under
which ORB works and the data is in hand. But structure it so the known
killer is measured first, not last:

  1. Backfill ~100-220 stocks, 3 years (background, ~8h).
  2. Reconstruct the daily "stocks in play" selection from opening
     relative volume, strictly from information available by 09:20.
  3. Measure GROSS edge on the selected names. If there is no large
     gross edge, stop -- costs only ever subtract.
  4. Apply a REALISTIC Indian single-stock cost model (spread by
     liquidity band, not a flat number -- this project already has
     spread_cost_study.py's per-premium-band approach to copy) and
     require the edge to survive by a wide margin, not marginally.
  5. Only then consider what instrument could actually express it.

Hard bar, set in advance: if the strategy's break-even slippage is of
the same order as the typical spread of the stocks it selects -- which
is what happened to the QQQ replication -- it is not tradeable here and
should be closed like index ORB was. Do NOT proceed to instrument
selection or live wiring on a marginal gross result.

Sources: Zarattini/Barbon/Aziz "A Profitable Day Trading Strategy For
The U.S. Equity Market" (SSRN 4729284); independent replication
github.com/giovannibrusco/zarattini-2023-orb-qqq.

## Bank Nifty cluster cap: 200pt is far too narrow -- ~500pt cuts drawdown 67% for 17% of profit (added 2026-08-19)

The live cluster cap (200pt / 30min, Sentinel-only) was backtested
against NIFTY's history ONLY -- main_live_banknifty_sentinel.py says so
in its own docstring -- then applied unchanged to Bank Nifty. Two
a-priori reasons that is wrong, both pointing the same way:

  - **Proportion.** 200pt is 0.83% of NIFTY at ~24,000 but only 0.35% of
    Bank Nifty at ~57,000. Proportional equivalence predicts ~475pt.
  - **Strike spacing.** NIFTY strikes are 50pt apart, Bank Nifty's 100pt,
    so a 200pt band reaches 4 strikes either side on NIFTY but only 2 on
    Bank Nifty. Structurally it can only THIN a Bank Nifty cluster,
    never collapse one.

Live evidence across three sessions: 2026-08-10 (8 CE trades, 8 strikes,
Rs -5,153), 2026-08-17 (9-strike cluster), 2026-08-19 (5 CE trades inside
115 seconds, Rs -11,436 -- two of them 300pt apart, straight through the
200pt band).

SWEPT band {none, 200, 300, 400, 500, 600, 800} at the live 30min window,
Bank Nifty's own 1,244-day history and live config (lot 30, premium
300-800), 5 independent ~1-year periods (same split as the 2026-08-13
BN momentum validation), real costs:

    band     trades       net Rs   profit kept   worst-period DD   DD cut   periods+
    none      8,483   +3,090,771        100.0%           309,466     0.0%      5/5
    200       6,417   +2,912,493         94.2%           172,508    44.3%      5/5
    300       5,736   +2,662,930         86.2%           142,339    54.0%      5/5
    400       5,229   +2,507,792         81.1%           121,443    60.8%      5/5
    500       4,708   +2,558,738         82.8%           102,199    67.0%      5/5
    600       4,380   +2,508,330         81.2%            73,160    76.4%      5/5
    800       4,217   +2,500,823         80.9%            69,093    77.7%      5/5

WHY NET PROFIT IS NOT THE RANKING METRIC: a cluster cap can only REMOVE
trades, so against a +Rs 3M backtest every cap cuts profit and ranking by
net would mechanically pick "no cap" and answer nothing. The cap exists
to cut DRAWDOWN. The bar is NIFTY's own Sentinel adoption: ~14% of profit
given up for a ~62% smaller max drawdown.

READ:
  - Drawdown falls MONOTONICALLY with band width in the aggregate, and
    falls in EVERY ONE of the 5 periods individually (e.g. Y4, the worst:
    309k -> 173k -> 121k -> 102k -> 69k). This is a structural effect,
    not a lucky cell.
  - All 5 periods stay positive at every band -- widening does not break
    profitability anywhere.
  - Profit retention PLATEAUS at ~81-83% from 400pt onward, so past 400
    the drawdown reduction is close to free.
  - The current 200 achieves only a 44.3% drawdown cut -- well short of
    the NIFTY Sentinel trade-off it was copied from.
  - Knee is around 500-600. 800 buys only 1.3pp more DD cut than 600.

INDEPENDENT CORROBORATION worth weighting: the a-priori proportional
prediction (~475pt, computed before running anything) lands inside the
empirically-best zone. The result is not a grid winner picked after the
fact -- theory and measurement agree, which is the strongest form this
kind of evidence takes here.

CAVEAT, stated rather than buried: 500 vs 600 is within noise on several
periods (Y2 DD 70k vs 73k, Y3 36k vs 42k -- 500 is actually better on
both). 600's aggregate advantage comes mostly from fixing the single
worst period (Y4, 102k -> 69k), and picking a band BECAUSE it fixes the
worst period is exactly where overfitting starts. The honest statement
is "anything in 400-800 is roughly equivalent, 200 is too narrow" -- the
broad plateau is itself the robust finding, not any single cell.

RECOMMENDATION: 500pt, being the round number nearest the a-priori
~475pt prediction rather than the empirical argmax. NOT APPLIED --
this is Sentinel-only territory and the change is not made here.
Anchor's Bank Nifty process has CLUSTER_CAP_ENABLED=False entirely and
is frozen; that even 200pt would have cut drawdown 44% for 5.8% of
profit is a separate and larger question, requiring live evidence under
this project's own promotion policy.

Full grid in `logs/sweep_banknifty_cluster_cap.json`
(`sweep_banknifty_cluster_cap.py`).

## SCOPED, NOT BUILT: NIFTY ATM-IV-rank as a replacement for the India-VIX condor gate (added 2026-08-19)

Follow-on from the IV-rank sweep below, which closed threshold tuning
and named this as the only remaining idea worth pursuing. This entry is
a SCOPE with a feasibility probe attached -- no implementation, no
decision taken.

### Why bother: India VIX measures the wrong thing

India VIX is a 30-day forward vol index built from a strip of OTM
options across the near AND next expiry, per NSE's methodology. The
condor sells WEEKLY options at specific strikes. So the gate currently
in production is filtering on a different tenor and a different part of
the surface from the thing actually being sold. That is not obviously
wrong -- vol regimes are correlated -- but it is a proxy, and the
question is whether the direct measure does better.

### Feasibility: PROVEN, the data exists

Probed directly rather than assumed:
  - Reconstructed history carries per-strike IV across the whole range
    (checked 2022-09, 2024-03, 2026-07: 38-41 of 42 strikes have IV).
    So an ATM-IV series is computable back to 2021 and the filter is
    BACKTESTABLE, not a "collect data for a year first" build.
  - `shadow_condor.CondorPolicy` already has the `min_iv_rank` hook and
    a pluggable history (`vix_history`), so the backtest wiring is a
    generalisation, not new machinery.
  - `india_vix_source.iv_rank_and_percentile` is the rank computation
    to mirror.

### It is genuinely different information, measured

305 paired days, 2021-08..2026-08, ATM straddle IV vs India VIX close:

    Pearson  corr = +0.683
    Spearman corr = +0.735   (rank agreement -- what matters for a rank gate)
    Gate verdict at rank>=15: AGREE 76.4%, DISAGREE 23.6%

Correlated but not redundant: roughly one day in four the two gates
would make opposite calls. That is the case for building it.

### The hard part, quantified: a raw ATM-IV rank is a day-of-week indicator

Median ATM IV by days-to-expiry on the weekly contract:

    DTE=6  13.00      DTE=3  15.41      DTE=1  16.94
    DTE=5  11.57      DTE=2  15.78      DTE=0  24.12
    DTE=4   9.76

An 85% swing from DTE=6 to DTE=0 driven purely by position in the
expiry cycle, not by whether vol is actually rich. India VIX has no
such problem because its 30-day tenor is constant by construction. A
naive `rank(ATM IV)` would therefore be substantially measuring "how
close to expiry are we", and would fire or block for reasons that have
nothing to do with volatility regime. **This is the whole engineering
problem; everything else is plumbing.**

Mitigating fact: condor entries cluster in a narrow DTE band rather
than spreading across the week (backtest 2024-08..2025-12: 47 of 48
entries at DTE 1-2), so the confound at the DECISION point is much
smaller than the table above suggests.

### Design options for the DTE problem

  A. **DTE-conditional rank** -- rank today's ATM IV against history at
     the SAME DTE. Directly removes the confound, cheap to implement.
     Cost: splits history ~7 ways; with entries concentrated at DTE 1-2
     the relevant buckets stay well populated, so this is likely
     adequate.
  B. **Constant-maturity interpolation** -- reconstruct a fixed-tenor
     (7d or 30d) ATM IV by interpolating across two expiries, i.e. what
     India VIX does. Most faithful, most expensive: needs the NEXT
     expiry's chain historically (`expiryCode=2` on the rolling
     endpoint) and a fresh backfill.
  C. **Normalise by the DTE median** -- divide by the historical median
     at that DTE. Cheapest, removes most of the effect, least
     principled.

Recommend starting at A, with C as a sanity cross-check. B only if A
shows promise and the residual DTE effect still looks material.

### What is NOT a head start (checked)

`state/iv_history.json` looks like an existing ATM-IV history and is
not one. `dhan_source` appends `atm_straddle_iv` on EVERY snapshot
fetch and truncates at `IV_HISTORY_WINDOW = 252`, so at ~75 cycles/day
it holds about three days of intraday readings, not the 252 TRADING
DAYS the name implies. It is also **written but never read** -- nothing
consumes it for any decision (it is a vestige of the abandoned
"rank each strike against ATM IV history" approach that the module
docstring describes replacing with cross-sectional percentile). A real
build needs daily sampling and a real 252-day window; this store should
be either fixed or deleted as part of the work rather than mistaken for
existing infrastructure.

### Validation bar (non-negotiable, same as the gate it would replace)

Must beat India-VIX-rank>=15 on ITS OWN terms: 4/4 independent periods
positive, and total net above +Rs 39,913 on the same 4 periods with
real p90 costs. Thresholds must be fixed pre-specified values, never
derived from the sample. A cell that wins on total but is 3/4 on
periods is NOT an improvement -- consistency was the entire reason 15
was adopted over higher-netting alternatives.

### Honest risks

  - **Most likely outcome is "no better".** Spearman 0.735 means the two
    agree on the big moves; the 23.6% disagreement may be concentrated
    in genuinely ambiguous days where neither call is reliably right.
  - Multiple comparisons: this adds a second regime-filter family to a
    strategy whose config has already been swept hard (36 condor
    configs, 12 PT x IV cells, 7 IV thresholds). The Bonferroni bar
    across everything tried on this strategy is getting demanding, and
    a marginal win should be read as noise.
  - The DTE fix itself is a modelling choice with free parameters
    (bucket width, window length) -- more knobs, more overfitting
    surface, on a strategy that has not yet earned its keep live.
  - Backtest-vs-live entry-DTE divergence noticed while probing this
    (backtest opens at DTE 1-2, the 3 real live entries are at DTE 5-6).
    Tiny live sample, could be nothing, but it should be understood
    BEFORE tuning an entry filter against backtest DTE behaviour that
    live may not reproduce.

### Recommendation

Worth doing, but as a measurement, not a fix -- with a real chance the
answer is "India VIX was fine". The DTE divergence in the last risk
above should be resolved first, since it questions whether the backtest
opens positions the way live does, which affects any entry-filter work.

## Condor IV-rank gate: swept BELOW 15 for the first time -- 15 confirmed a real optimum, keep it (added 2026-08-19)

Prompted by a direct observation: the condor had not opened a position
since 2026-08-13. Not a fault -- India VIX sat at 11.3-11.5 all week (IV
rank 11-12), below `config_condor.MIN_IV_RANK_TO_OPEN = 15`, so it
correctly declined to sell cheap premium every cycle. But "never trades"
is not a strategy either, so the threshold was worth re-examining.

THE GAP: the sweep that chose 15 (2026-08-12 entry below) tested
min_iv_rank in {15, 20, 25, 30}. **15 was the LOWEST value tried.** It
won a grid it sat on the edge of, which is materially weaker evidence
than beating neighbours on both sides -- the untested question was
whether something BELOW 15 trades more without giving back the edge.

Swept it, same 4 independent periods, PT=50% fixed (the original found
the PT level secondary to the gate itself), real p90 costs:

    gate        trades       net Rs   periods_positive
    none           252      -42,626   2/4
    iv>=5          196      -82,095   2/4
    iv>=8          187      -71,422   2/4
    iv>=10         167      -45,285   2/4
    iv>=12         156       -9,557   2/4
    iv>=15         132      +39,913   4/4   <- current live value
    iv>=20         101      +20,197   2/4

RESULT: lowering the gate is decisively worse, and degrades roughly
monotonically as it drops (12 -> -9.6k, 10 -> -45k, 8 -> -71k, 5 ->
-82k). 15 remains the ONLY cell positive in all four periods and still
has the highest total net of anything tried, now with real values on
BOTH sides of it rather than just above. The grid-edge concern from
2026-08-12 is resolved, in favour of keeping 15.

Note `iv>=5` is worse than no gate at all (-82k vs -43k). Not a
contradiction: with one-at-a-time position limits, skipping a day
changes which later positions you are free to hold, so a gate can move
you into a worse set of trades rather than simply removing bad ones.

NOTHING CHANGED. The honest consequence is the one already being
observed: this strategy is DESIGNED to sit out low-volatility regimes,
and if VIX stays in the low 11s it will keep not trading -- for weeks,
potentially. That is the gate working, not failing. Accepting long flat
stretches is the cost of the only condor configuration that has ever
been positive across all four periods.

CAVEAT worth keeping visible: at iv>=15 the 2022-08..2023-12 period is
only +Rs 716 -- technically positive, but thin enough that the "4/4
periods" headline rests on a near-coin-flip in one of them. Same
finding as 2026-08-12, unchanged by this sweep.

Full grid in `logs/sweep_condor_iv_rank.json` (`sweep_condor_iv_rank.py`).
CLOSED for threshold tuning -- reopen only on a genuinely different
idea (e.g. a NIFTY ATM-IV-rank measure rather than India VIX, or a
regime filter that is not volatility-level based), not another grid.

## Dhan request demand is ~3.4x the rate limit -- RESOLVED 2026-08-18 (limiter hardened 2026-08-17, demand itself cut 2026-08-18)

The limiter's thundering-herd bug was fixed 2026-08-17 (see below). The
underlying CAPACITY problem -- momentum's fast check re-fetching a full
option chain every 5s while a trade was open -- is now fixed too, via
option 1 from this entry's original list: the fast check moved off
`/optionchain` entirely.

ORIGINAL PROBLEM (2026-08-17 session, 11 live processes sharing one Dhan
account): the fast check only ran while its process held a position, so
demand scaled with how many momentum processes were in a trade
simultaneously -- 0 -> ~10 req/min (fine), 1 -> ~22, 2 -> ~34, 4 -> ~58,
against a ~17.1 req/min chain-fetch budget. The account went over the
limit precisely WHILE HOLDING POSITIONS -- exactly when a missed
stop/target check matters most. Measured that session: 2,209
lock-acquire timeouts, 53 real 429s on NIFTY alone, 171 blind cycles (79
of them inside the 36-minute window NIFTY held 4 trades). As a stopgap
the same day, Sentinel's two processes dropped their fast check
entirely, which cut the worst case to ~34 req/min but was still over
budget, and cost Sentinel up to POLL_INTERVAL_SECONDS of exit-detection
latency versus Anchor -- a real, recorded weakening of the Anchor-vs-
Sentinel comparison (STRATEGY_VERSIONS.md's whole point).

FIX (2026-08-18): `check_open_trades_fast()` in all four momentum
processes now calls `dhan_source.get_fast_check_snapshot()` instead of
the full chain fetch:
  1. `orderflow.py`'s already-live WebSocket book, first -- ZERO Dhan
     REST cost (it's a persistent WebSocket connection, not a polled
     endpoint, so it doesn't count against either rate-limiter budget
     at all), and has the real bid/ask AND ltp for any strike it's
     subscribed to, which in steady state is every open position (both
     orderflow_feed.py and orderflow_feed_banknifty.py subscribe to
     every strike a live process is tracking).
  2. `POST /v2/marketfeed/ltp` (new: `dhan_source.get_ltp_batch()`),
     ONE batched request per process per cycle, only for whatever
     orderflow didn't have fresh data for. This is Dhan's own
     documented separate quota -- 1 request/second for up to 1000
     instruments -- not the same budget as `/optionchain`. Verified
     live pre-market on 2026-08-18 (real security IDs, real response
     parsing, see dhan_source.get_fast_check_snapshot's own tests) --
     structurally correct; noted for the record that the two endpoints
     returned visibly different LTPs for the same strike during that
     pre-market check (no trades yet that session), which reads as
     ordinary pre-market staleness between two independently-cached
     Dhan endpoints, not a parsing bug -- both numbers were internally
     consistent with which security ID they came from.
  `dhan_rate_limiter.py` was generalized to coordinate two independent
  budgets (`wait_for_slot()` now takes optional `lock_path`/`state_path`,
  defaulting to the original chain files; `wait_for_ltp_slot()` is the
  new LTP-budget equivalent) so an LTP call never queues behind the
  chain endpoint's slower interval for no reason.

NEW DEMAND PICTURE. The fast check no longer touches the chain budget
AT ALL (steady state: zero REST calls, orderflow covers it for free).
Chain-budget demand is now POSITION-INDEPENDENT -- it no longer scales
with how many momentum processes hold trades:

    momentum full cycles   4 processes x 30s   =  8.0 req/min  (unchanged)
    directional spread     2 processes x 90s   =  1.3 req/min  (unchanged)
    condor                 1 process x 300s    =  0.2 req/min  (unchanged)
    price action           2 processes x 300s  =  0.4 req/min  (unchanged)
                        chain budget, ANY state =  9.9 req/min
    limiter budget at MIN_INTERVAL_SECONDS=3.5 = 17.1 req/min  (~1.7x headroom, always)

LTP-fallback demand (only strikes orderflow misses; genuinely worst
case, assuming orderflow provides nothing at all, which should not
happen in steady state):

    4 processes x 1 batched call / 5s          = 48.0 req/min
    limiter budget at LTP_MIN_INTERVAL_SECONDS = 50.0 req/min  (1.2s/call)
    Dhan's documented limit (1/sec)             = 60.0 req/min

Both budgets now have headroom in the worst case, not just the idle
case -- the structural fix this entry originally asked for.

Sentinel's fast check was RE-ENABLED 2026-08-18 on this same cheap
mechanism (main_live_sentinel.py / main_live_banknifty_sentinel.py),
removing the exit-latency confound the 2026-08-17 stopgap introduced.
Anchor and Sentinel now differ ONLY in the cluster cap again, which is
the actual comparison this candidate exists to run.

## Rate limiter abandoned rate limiting under contention -- thundering herd (added 2026-08-17)

`wait_for_slot()`'s lock-acquire timeout used to be a bare `return`:
"could not acquire lock in time, proceeding without it this cycle." That
abandoned rate limiting entirely at exactly the moment it mattered most.
With demand above budget (see above) every process eventually times out,
so they all fired simultaneously -- a queueing problem turned into a
stampede, 2,209 times in one session.

Fixed: on timeout it now falls back to best-effort spacing WITHOUT the
lock (`_respect_interval_unlocked`), re-reading `last_request_at` in a
jittered loop so waiters queue behind each other instead of all waking
and firing together. Bounded by `UNLOCKED_SPACING_MAX_WAIT_SECONDS` so a
trading loop can never be held indefinitely.

Measured with 11 concurrent waiters, old vs new: requests firing
near-simultaneously dropped from 7/10 to 2/10, total span 0.37s -> 0.90s.
A first attempt at this fix (sleep once, then fire) was barely better
than the old behaviour -- every waiter read the same timestamp and woke
together -- which a concurrency test caught before it shipped; the
re-check loop is what actually queues them.

Reduces 429s. Does NOT create capacity -- see the entry above.

## Telegram "On the radar": reversed the silent-when-flat design, on explicit request (added 2026-08-16)

Follow-up to the Telegram notifications entry directly below. User
asked to see "the highest or latest strike price... 1st in row in
momentum," clarified (after I initially misread it as sort order for
OPEN trades) to mean the scanner's top-SCORED CANDIDATE each cycle --
"on the radar," not a live/open position.

Added `_radar_line()`/`_radar_section()`: shows each index's `candidates[0]`
(main_live.py already sorts by raw score descending before logging, so
the first entry IS the top one -- no re-sort needed) with raw score,
adjusted score if different, the conviction bar, and why it did/didn't
open. Explicitly no score floor, per direct instruction ("only #1",
declined the score-cutoff option offered).

REAL BEHAVIOR CHANGE, confirmed with the user before pushing (showed
actual rendered sample output first): `build_full_message()` now sends
whenever there's EITHER an open position OR a candidate this cycle,
combined into one message. Since the scanner almost always produces
some top candidate during market hours, this means the notifier no
longer stays silent on a flat day -- it was originally built that way
deliberately ("a ping every 15 minutes on a flat day would just be
noise"), and this change reverses that specifically for this reason,
by explicit request. `build_snapshot_message()` (positions only, still
silent when flat) is kept as its own function for any future use that
wants the old behavior.

44 new/updated tests.

## Telegram notifications: positions, pre-market summary, bias shifts (added 2026-08-16)

Built because positions weren't visible away from the screen. Three
message types from one process (`position_notifier.py`, wired into
`automation/start_trading.ps1`): a position snapshot every 15 min
(silent when flat), a condensed pre-market summary once at startup, and
an intraday bias-shift alert whenever NIFTY's or Bank Nifty's
`market_bias` label actually changes.

WORTH KNOWING for the bias-shift feature specifically: checked real
recorded `decision_log_banknifty.jsonl` data before building this and
found NIFTY's bias flipping `bearish -> neutral/range -> bearish` twice
within 90 seconds on 2026-08-14, from PCR oscillating right at the
bearish threshold (`compute_market_bias()`'s score boundary is an exact
+-1.0 cutoff, no hysteresis). The alert only fires on a change from the
IMMEDIATELY PRIOR check (persisted in `state/position_notifier_bias.json`),
sampled on the same 15-minute cadence as position snapshots -- not
continuously -- so most flicker like that never even gets sampled. Not
a full fix for the underlying noise (`compute_market_bias()` itself
still has no hysteresis/debounce), just a mitigation at the
notification layer. If this still turns out noisy in practice, the
real fix belongs in `compute_market_bias()` itself (e.g. a small buffer
around the +-1.0 threshold), not another layer of notifier-side
patching.

`premarket.py` now saves its brief as JSON (`logs/premarket_brief_<date>.json`)
alongside the existing markdown, so `position_notifier.py` can reuse the
real computed brief instead of re-running `build_brief()` a second time
(duplicate Dhan/NSE/global-cues calls) or parsing markdown back apart.

Uses emoji throughout (🟢/🔴/⚪, 📈/📉/➖) -- an explicit user request,
not this project's usual style elsewhere; kept scoped to
`position_notifier.py`'s own messages.

50 tests across `position_notifier.py`, `telegram_notifier.py`, and the
premarket JSON output.

## Bank Nifty order flow: own feed process built, closes the permanent book_imbalance gap (added 2026-08-16)

Follow-up to the "decision_log staleness" entry directly below: while
investigating it, found `orderflow_feed.py` had only ever subscribed to
NIFTY strikes (confirmed via its live state file: tracked strikes
24100-24650, never anything near Bank Nifty's ~50000+ range) -- no
`--underlying`/`--symbol` argument existed at all. That meant Bank
Nifty's `book_imbalance`/`total_quantity_imbalance` were structurally
guaranteed None forever, a coverage gap no amount of waiting would fix
(unlike NIFTY's own gap, which was about decision_log not growing).

Built `orderflow_feed_banknifty.py` as a thin wrapper around the same
`OrderFlowFeed` class (it already took `state_path` as a constructor
argument -- nothing about the class itself was NIFTY-specific), pointed
at `dhan_source.BANKNIFTY_UNDERLYING_SCRIP/_SEG`, its own state file
(`state/orderflow_banknifty.json`), and its own spread-recording
directory (`logs/orderflow_banknifty/`, via a per-process
`orderflow_recorder.RECORD_DIR` override -- same pattern
`trade_tracker.JOURNAL_PATH` already uses for every NIFTY/Bank Nifty
pair). Also had to parameterize two modules that WERE genuinely
NIFTY-hardcoded (unlike `dhan_source.get_nifty_snapshot`, which already
took `underlying_scrip`/`underlying_seg`):

  - `instrument_master.py`: `UNDERLYING = "NIFTY"` and a single
    `instrument_master_nifty.json` cache were module constants, not
    parameters. Every function now takes `underlying` (default "NIFTY",
    so nothing about NIFTY's own behaviour changed), with a separate
    cache file per underlying. Careful check: "BANKNIFTY" contains
    "NIFTY" as a substring, so the underlying filter uses an exact match
    against `UNDERLYING_SYMBOL`, not a substring test -- verified with a
    test that puts both symbols in the same fake CSV and confirms no
    cross-contamination.
  - `main_live_banknifty.py` / `main_live_banknifty_sentinel.py`: added
    `orderflow.STATE_PATH = .../orderflow_banknifty.json` alongside their
    existing `tt.JOURNAL_PATH`-style per-process overrides --
    `decision_log._candidate_record()` calls `orderflow.book_imbalance()`
    with no explicit path, so without this it would keep silently
    defaulting to NIFTY's own feed file. Observability only (recorded for
    future research, never gates a decision -- see decision_log.py's own
    comment on that field); confirmed via the same isolation check used
    for Sentinel: importing main_live.py alone still resolves
    `orderflow.STATE_PATH` to NIFTY's default, `config.STRATEGY_NAME`
    stays "Anchor".

Added to `automation/start_trading.ps1` (`orderflow_feed_banknifty.py
--strike-range 600`) alongside NIFTY's own `--strike-range 300`. That
600 is a first-pass estimate -- double NIFTY's, matching Bank Nifty's
100pt vs 50pt strike spacing -- not independently tuned against real
Bank Nifty book data yet.

STILL NOT DONE: `orderflow.py`'s CLI health check and `spread_study.py`'s
analysis both still only ever look at NIFTY's own state/recording paths
(their own module defaults). Bank Nifty's spread data will accumulate in
`logs/orderflow_banknifty/` starting whenever this process is first run,
but nothing yet reads it back for analysis the way `spread_study.py`
already does for NIFTY -- worth doing once there's a few days of Bank
Nifty data to look at.

## decision_log.jsonl silently stopped growing for 2+ weeks -- watchdog extended, root cause only partly confirmed (added 2026-08-16)

Investigating "do we have enough order-flow data to analyse book_imbalance"
found `logs/decision_log.jsonl` (NIFTY) had recorded ZERO new cycles since
2026-07-29T15:29:50, despite `main_live.py` clearly continuing to scan and
trade normally through at least 2026-08-14 (confirmed against real,
multi-hundred-KB `nifty_scan_*.log` growth on every day in between, with
every single cycle reaching the log line immediately before the
`decision_log.log_cycle()` call). dev's own copy shows the same pattern,
stopping 2026-08-05. Consequence: `book_imbalance`/`total_quantity_imbalance`
(added 2026-08-07, see the "Order flow" entry below) have literally never
been captured for NIFTY -- not "too small a sample yet," zero samples,
because the file meant to hold them never grew past a point that predates
the feature.

CONFIRMED, not the cause: a bug in `decision_log.log_cycle()` or
`_candidate_record()` -- directly reproduced by calling `log_cycle()` with
realistic `MarketSnapshot`/`Setup`/`TradePlan`/`RiskVerdict` objects against
the real file; it appended correctly, no exception, every time. Also ruled
out: a silent early-return in `run_once()` (the scan logs show `setups` is
never empty on any of these days) and the per-cycle blanket exception
handler swallowing it (every "Error this cycle" message across
2026-08-03..14 traces to unrelated NSE/Dhan data-source hiccups, none to
decision_log or orderflow).

STRONGLY IMPLICATED, not fully proven: `logs/decision_log.jsonl` was
accidentally committed to git early on (`ebdae60`, 2026-07-28), re-added a
few times as it grew, and finally untracked in `08b2f4d` ("Untrack the
decision log, and fix a clean-tree false positive") at **2026-07-29
12:38** -- about 3 hours before the last real cycle this file ever
recorded (15:29:50 the same day). A `git rm --cached` never touches the
working-tree file's bytes, so this doesn't fully explain why appends never
resumed on any later day, but the exact timing match on the boundary date
is hard to read as coincidence. Not chased further: it needs either
git-reflog archaeology or catching it live, and the practical fix below
matters more than nailing the exact historical trigger.

FIX: extended `watchdog.py` (previously only checked `nifty_scan_*.log`'s
mtime, which stayed healthy through this entire incident since it's a
different write earlier in the same cycle) with `decision_log_check_once()`
-- reads just the last line of both `decision_log.jsonl` and
`decision_log_banknifty.jsonl` (efficient tail-read, doesn't load the
15-25MB files) and warns loudly if the last recorded cycle is older than
`STALE_THRESHOLD_SECONDS` (180s) during market hours. Whatever the root
cause turns out to be, this converts a silent multi-week gap into a warning
within minutes. `tests/test_watchdog.py` (10 tests) covers the tail-read
(including a bug the tests themselves caught: the first version broke on a
record spanning more than one 64KB read chunk) and the staleness logic for
both indices independently.

NEXT STEP: run `watchdog.py` during Monday's live session and confirm
`decision_log.jsonl` is actually growing again -- if the untrack-adjacent
timing really was the trigger, appends should resume cleanly now that git
no longer touches the file at all. If it's STILL stale despite the write
path testing clean in isolation, that's the strongest possible clue left
(something specific to the live process's real execution environment, not
the code) and worth live-debugging directly rather than guessing further.

## Bank Nifty directional spread: validated (4/5 independent years), added to start_trading.ps1 (added 2026-08-13)

Third of the three Bank Nifty variants tested this session (momentum
validated and live, condor tested and closed as net-negative -- see
those entries). Checked the placeholder config (250-450 premium, 350pt
hedge) against real chain data first: 20/20 sampled days found a valid
short leg, 65% also found the hedge within the reconstructed data's
coverage window -- a far less severe version of the condor's coverage
problem, since this strategy's hedge sits much closer to the short
strike.

Full 5-year baseline (real p90 costs): n=154, win=57.1%, net
+Rs 132,462, but top-3 trades = 47.3% of total -- flagged before
trusting it. Split into 5 independent ~1-year periods:

    period                  n    win%   net Rs
    Y1 2021-08..2022-07     36   52.8%  +1,610
    Y2 2022-08..2023-07     37   62.2%  +42,489
    Y3 2023-08..2024-07     30   66.7%  +71,721
    Y4 2024-08..2025-07     24   41.7%  -5,189
    Y5 2025-08..2026-08     27   59.3%  +21,831

4/5 periods positive. Dug into the concentration: the single best trade
in the whole run (2024-05-29, +Rs 36,705, inside Y3) is 28% of the total
-- removing it entirely still leaves +Rs 95,757 net and 4/5 years
positive, so the edge does not depend on that one trade, though Y3
without it is meaningfully less impressive (+35,016 vs +71,721).

Y4's negative result traced to just 4 stop-loss trades (-Rs 15,630) out
of 24 -- looked like a miscalibrated STOP_LOSS_PCT_OF_MAX_LOSS (50%,
inherited unchanged from NIFTY). Swept 30/40/50/60/70%/disabled across
all 5 periods to check:

    SL       total Rs    periods positive
    30%      +87,320     4/5
    40%      +129,450    4/5
    50%      +132,462    4/5   (current, best total)
    60%      +105,147    3/5
    70%      +127,355    3/5
    disabled +118,725    3/5

Y4 stays negative at EVERY setting including disabled -- real period
variance in that year's directional calls, not a stop-loss calibration
problem. The inherited 50% was already near-optimal; no change made.

DECISION: kept the placeholder config as-is (already validated), added
main_directional_spread_banknifty.py to automation/start_trading.ps1.
Standing caveat: LTP-fallback-fill optimism bias, same as every backtest
against this reconstructed data. Meaningfully weaker evidence than
momentum's clean 5/5-period result, but a real, survives-losing-its-
best-trade edge -- not remotely like the condor's outright failure.

## Bank Nifty condor: net negative even with the profit-target exit that fixed NIFTY's -- closed, not adopted (added 2026-08-13)

The placeholder calibration from the 2026-08-12 plumbing entry
(SHORT_PREMIUM_MIN/MAX=150/250, HEDGE_DISTANCE_POINTS=1000) turned out
badly miscalibrated, not just imprecise: a direct chain inspection
showed the EDGE of the reconstructed data's own ATM+/-10-offset window
(the hard Dhan API cap, ~+/-1000pts at Bank Nifty's 100pt spacing) still
carries Rs 2-640+ of premium depending on where in the monthly cycle you
look -- nowhere near the 150-250 band, so it found almost no candidates
(2 opens in a 90-day probe).

Grid-probed real combinations against actual chain data and found a
workable one: SHORT_PREMIUM_MIN/MAX=100/400, HEDGE_DISTANCE_POINTS=400.
Ran the full 5-year baseline (real p90 costs, MIN_IV_RANK_TO_OPEN=None
since India VIX measures NIFTY's own IV, not Bank Nifty's):

    policy      n     win%   net Rs        months_neg
    baseline    149   42.3%  -133,835      38/60
    PT=40%      180   52.8%  -118,479      34/60
    PT=50%      170   50.0%   -86,094      36/60
    PT=60%      165   47.3%  -109,800      38/60

PT=50% is the best cell (same direction as NIFTY's own finding -- higher
win rate, smaller loss) but never flips the sign. For NIFTY, the
profit-target ALONE didn't fix it either -- it needed pairing with the
IV-rank entry gate to go net positive, and there is no valid IV-rank
equivalent here (India VIX doesn't measure Bank Nifty's own volatility;
a real fix would need a Bank-Nifty-specific ATM-IV-rank measure built
from scratch, not borrowed).

ALSO a real structural headwind unique to Bank Nifty, not present for
NIFTY's condor: severe hedge-coverage gap (22,291 coverage_gap skips vs
only 149 real opens in the baseline) -- the reconstructed data's hard
ATM+/-10 offset cap bites much harder here because Bank Nifty's monthly
options carry meaningful premium far more points from spot than NIFTY's
weekly ones do, so a real day's tradeable condor frequently has a hedge
the data source simply cannot see. This likely also constrains what the
LIVE process (main_condor_banknifty.py) can actually trade, not just
the backtest.

CLOSED, not left open-ended, same reasoning as the original NIFTY condor
sweep-config closure: this isn't a case for more parameter tuning, the
missing piece is a genuinely new build (Bank-Nifty ATM-IV regime filter)
with uncertain payoff, not a config sweep. Not adopted, not pushed to
prod, main_condor_banknifty.py's placeholder config updated to the
workable-but-still-losing 100-400/400 calibration so future work starts
from a real number instead of the original unreachable one. Revisit only
with a genuinely new idea (the ATM-IV filter, or a wider-coverage data
source), not another config grid.

## Bank Nifty momentum: validated across 5 independent years, added to start_trading.ps1 (added 2026-08-13)

Follow-up to the entry below, now that the 5-year backfill and OI-buildup
repair finished. Swept 10 PREMIUM_MIN/MAX bands against the full 1,244-day
history (real costs via costs.py, config.NIFTY_LOT_SIZE=30 throughout):

    band                    n      win%   net Rs
    NIFTY band (control)    3,369  24.3%  +575,844
    current placeholder     8,483   8.3%  +3,090,771   (300-800)
    200-600/800/1000        9,461   9.3%  +3,278,667   (best)
    400-600/800/1000        7,032   7.5%  +2,812,955

Every Bank-Nifty-calibrated band beat the NIFTY band by 5-6x. But a
first walk-forward split (IS 2021-08..2025-07, OOS 2025-08..2026-08)
revealed a real problem before adopting anything: 98% of the ENTIRE
13-month OOS profit came from just the most recent 8 months
(2026-01..2026-08, +Rs 7,82,580) -- the genuinely fresh first 5 months of
OOS (2025-08..2025-12) were flat (+Rs 14,138). Same "was this just a
favourable regime" trap the condor's own OOS hit earlier this session,
here more extreme.

Re-tested properly: split into 5 INDEPENDENT ~1-year periods instead of
one IS/OOS split.

    period                    net Rs (300-800)   net Rs (200-800)
    Y1 2021-08..2022-07       +492,826           +598,141
    Y2 2022-08..2023-07       +464,788           +476,408
    Y3 2023-08..2024-07       +748,201           +796,718
    Y4 2024-08..2025-07       +588,238           +604,373
    Y5 2025-08..2026-08       +796,718           +803,026

5/5 periods positive for both bands, magnitudes within a ~1.7x range of
each other (not one period dominating) -- Y5 (containing the earlier
"hot stretch") is not meaningfully ahead of Y3, an entirely different,
older period. Trade-level concentration also checked and clean: top-3
trades = 2-8% of total. This is what actually justified adopting it,
not the earlier single OOS split.

STANDING CAVEATS: the reconstructed historical data has no bid/ask
(shadow.py falls back to LTP fills -- the same OPTIMISTIC bias every
backtest against historical_source.py data carries). Only ~8-9% of
trades hit the exact 2R target; most profit comes from EOD_CLOSE trades
averaging positive rather than genuine target hits -- a different
character from NIFTY's own momentum, and one with NO live track record
yet, since this is the first time this strategy has ever traded Bank
Nifty at all.

DECISION: kept PREMIUM_MIN/MAX=300/800 (the original placeholder --
close enough to the swept-best 200-800 that re-tuning wasn't worth the
extra overfitting risk of picking the literal best cell). Added
main_live_banknifty.py to automation/start_trading.ps1. Condor and
directional spread's Bank Nifty variants are still backtest-only,
unswept -- see the entry below.

## Bank Nifty: plumbing built for all three strategies, NOT validated, NOT in automation yet (added 2026-08-12)

Direct follow-up to the 2026-08-04 "Run every strategy on Bank Nifty"
entry below. `historical_source.py` turned out to already be fully
Bank-Nifty-parameterized (BANKNIFTY_SECURITY_ID, BANKNIFTY_EXPIRY_FLAG,
BANKNIFTY_DATA_START -- all confirmed by direct probe back on
2026-08-04, found while starting this work rather than rebuilt).

CRITICAL FACT VERIFIED LIVE 2026-08-12 (not assumed): Bank Nifty has
been MONTHLY-expiry-only since SEBI's Nov 2024 rules capped weekly
expiries to one benchmark index per exchange -- NSE kept NIFTY weekly,
moved Bank Nifty to monthly.

    NIFTY:     2026-08-18, 08-25, 09-01, 09-08, ...  (weekly)
    BANKNIFTY: 2026-08-25, 09-29, 10-27, 12-29, ...  (monthly)

This matters enormously differently per strategy: momentum is same-day
only, so cadence barely matters -- closest to a direct port. Condor and
directional spread are NIFTY's weekly theta-decay strategies; a
monthly-only Bank Nifty version is not a re-tuned copy, it is
structurally different (~12 cycles/year instead of ~52, much wider
expected move per cycle). Also confirmed fresh (not assumed): lot size
still 30, strike spacing 100pt near the money.

Built, following the existing main_price_action_banknifty.py precedent
(separate process, separate state/journal/log, calls dhan_source
directly with BANKNIFTY_UNDERLYING_SCRIP/_SEG since resilient_source.py
is hardcoded to NIFTY with no Bank Nifty path at all -- Dhan-only, no
NSE/TradingView fallback):

  - `main_live_banknifty.py` -- momentum. Drops the NIFTY-vs-Bank-Nifty
    divergence signal entirely (doesn't apply to a Bank Nifty variant of
    itself). PREMIUM_MIN/MAX=300/800 and LOT_SIZE=30 carried over from
    price_action_banknifty's own real 2026-08-06 sweep; STRIKE_RANGE_POINTS
    proportionally scaled (2000, vs Nifty's 800/24000 spot). NOT
    independently swept for momentum specifically.
  - `main_condor_banknifty.py` -- condor. SHORT_PREMIUM_MIN/MAX=150/250,
    HEDGE_DISTANCE_POINTS=1000 are proportionally-reasoned PLACEHOLDERS,
    not swept. MIN_IV_RANK_TO_OPEN explicitly left None: India VIX
    measures NIFTY's own IV specifically, not Bank Nifty's -- reusing it
    here would gate on the wrong instrument's volatility. Would need a
    Bank-Nifty-specific IV-rank measure built from its own ATM IV
    history, not borrowed.
  - `main_directional_spread_banknifty.py` -- directional spread.
    SHORT_PREMIUM_MIN/MAX=250/450, HEDGE_DISTANCE_POINTS=350, same
    placeholder status.
  - All three verified import-clean and correctly path-isolated (own
    state/journal/log/snapshot files, never touching Nifty's); full
    568-test suite still passes with no cross-contamination. No
    dedicated unit tests for these entry-point files themselves, same
    precedent as main_live.py/main_condor.py/main_price_action_banknifty.py
    -- config is patched at MODULE IMPORT TIME (safe only because each
    runs as its own OS process), which would corrupt shared config for
    any other test running in the same process if one were ever added.

Bank Nifty historical backfill (2021-08-04 onward, ~5 years, monthly
rolling series) was launched in the background the same session --
long-running (~2.5 hours estimated from a 30-day-chunk timing probe),
still in progress as of this entry.

NONE OF THIS IS IN start_trading.ps1 AND NONE OF IT SHOULD BE YET. Every
placeholder number above needs a real sweep against Bank Nifty's own
backfilled history -- same bar as everything else in this file: real
p90 costs, walk-forward, outlier-concentration check -- before any of
these three processes is trusted even as a backtest verdict, let alone
run live.

## Condor: full 12-cell PT x IV-rank sweep across all 4 real periods -- one combo is 4/4-period positive, still held back (added 2026-08-12)

Direct follow-up to the correction below (only 2/4 periods had been
tested for the combined fix). Before adopting anything, swept the full
grid properly: profit_target_pct in {40, 50, 60}, min_iv_rank in
{15, 20, 25, 30} (12 cells), each across all 4 independent periods,
real p90 costs throughout.

    PT   IVrank>=   total_n   total_net    periods_positive
    base    --         187     -31,527           2/4
    50      15         132     +45,227           4/4  <- only 4/4 combo
    50      20         103     +24,559           2/4  (prior "best evidence" entry)
    50      25          75     +32,472           3/4
    60      15         121     +23,577           3/4

PT=50% + IV rank>=15 is the ONLY cell out of 12 positive in every one
of the 4 periods, and has the highest total net of the whole grid --
materially stronger and more consistent than the previously-reported
PT=50/IVrank>=20 combo. The IV rank>=15 column is doing most of the
work (2/4, 4/4, 3/4 across the 3 PT values at that threshold); IVrank
20/25/30 columns are all more mixed (2-3/4). The exact PT level looks
secondary to the IV-rank gate itself.

Outlier/monthly checks on the winner (n=132, net +Rs 47,490 when run
as one continuous backtest vs the +45,227 sum-of-4-periods -- small
boundary-effect difference, not material):
  - top-3 trades = 19.7% of total, top-5 = 32.1% of total -- borderline
    on the ~20% top-3 bar used elsewhere this session, getting more
    concentrated at top-5. Not dominated by a couple of flukes, but
    worth watching.
  - 23 of 37 months positive (62%). Two real bad months remain even
    under this combo: 2025-03 (-Rs 20,368) and 2024-05 (-Rs 12,815).
  - Worst single trade: -Rs 16,661.

HONEST READ: strongest, most consistent condor evidence found this
session -- genuinely robust across all 4 real periods, unlike every
prior candidate fix. But it is still "best cell out of 12 tried" (some
inherent look-ahead-adjacent risk in picking a grid winner, even though
neighbouring IVrank>=15 cells are also decent), and real drawdown
months persist. On average helps a lot; does not remove real risk.

DECISION (explicit, from the user): hold as backtest-only for now, do
not wire into condor_tracker.py or main_condor.py. Revisit later with
more data rather than acting on this immediately.

## CORRECTION to the entry below: only 2 of the available 4 years were tested. Extended, and the combined-fix story is weaker than reported (added 2026-08-11)

The combined-fix entry below this one only covered 2024-08..2026-07.
That window was never a deliberate choice -- it was inherited from
whatever range the original saved bt_condor.json happened to use, and
carried forward through every subsequent test without checking whether
more data existed. It did: snapshot_recorder has 1,491 real days,
2020-08-03..2026-08-05 -- more than half the available history was
never tested. Caught only because it was asked directly, not found
proactively -- should have been checked before presenting the
2-period result as strong.

Extended to all 4 independent stretches the real data supports (VIX
history starts 2021-08-11, so ~2022-08-17 is the earliest date an IV
rank is computable):

    period               baseline        combined rule
    2022-08..2023-12     -17,279         -12,413   (STILL NEGATIVE)
    2024-01..2024-07     +17,969            -709   (rule made it WORSE)
    2024-08..2025-12     -35,482         +23,432
    2026-01..2026-07      +3,265         +14,249
    TRUE FULL HISTORY    -31,527 (187)   +24,559 (103)

The aggregate across 4 years is still positive, and the combined rule
beats baseline in every single period. That part holds. But 2 of the 4
independent periods are STILL NET NEGATIVE with the rule active, and
one of them (early 2024) the rule actively makes worse than doing
nothing. "Both tested periods positive" (the prior entry's headline)
was true only because two of the four real periods were never tested.

REVISED, HONEST READ: there is a real, positive net effect across the
full available history -- not nothing. But "reliably profitable" or
"resolves the regime-luck concern" overstated it. The more accurate
description: helps on average, does not work in every stretch, and
2022-23 specifically stays a loser even with both fixes active. Same
overfitting-risk caveat as before (hand-picked PT 50% / rank>=20, not
swept) still applies, now with less supporting evidence than
previously stated, not more.

STILL not wired into the live tracker. This finding argues for MORE
caution before doing so, not less, relative to the prior entry's tone.

## Condor combined (profit-target + IV-rank): both periods independently positive -- best evidence yet, still not adopted (added 2026-08-11)

## Condor combined (profit-target + IV-rank): both periods independently positive -- best evidence yet, still not adopted (added 2026-08-11)

Direct follow-up to the two entries below, which each independently
raised the same concern: both the profit-target rule and the IV-rank
gate improved the FULL period, but each left the 2024-25 (IS) half
still net negative, with 2026 (OOS) carrying the whole improvement --
raising a real possibility that 2026 was simply a more favourable
condor regime generally, not that either fix was structural. Tested
the obvious next thing: run both together.

    period          baseline       PT 50% only    IV rank>=20 only   COMBINED
    IS (2024-25)    -35,482        -15,786        -10,457            +23,432
    OOS (2026)       +3,265        +15,090         +19,862           +14,249
    FULL PERIOD     -32,217           -696          +9,405           +37,681

BOTH periods are independently positive for the first time. That
directly weakens the regime-luck concern -- the strategy now works in
the exact period that previously did NOT support "this only works
because of a favourable 2026." Full period: 55 trades, 81.8% win,
+Rs 37,681 net (real p90 costs) vs baseline's -Rs 32,217.

Checked for the same outlier-concentration failure mode that killed
every commodity/MA-crossover result earlier this session: monthly
breakdown across 18 months, top-3-by-magnitude months sum to only 20%
of the total (Rs 7,567 of Rs 37,681) -- removing them still leaves
+Rs 30,114. 13 of 18 months are individually positive. Not a
concentration artifact.

REMAINING CAVEATS, stated plainly:
  - 55 trades over ~2 years is still a thin sample by this project's
    own standard (the validated directional spread has 137).
  - This exact combination (PT 50%, IV rank>=20) was hand-picked from
    each rule's own best individual full-period candidate, not swept
    across nearby values (PT 40/60%, IV rank 15/25, etc.) the way the
    directional spread's strike config was. Cannot yet rule out that a
    slightly different pair of numbers does meaningfully better or
    worse -- i.e. the SAME overfitting risk already flagged for the
    condor's strike-config sweep applies to this combination too.
  - Still backtest-only. Neither rule is wired into the live tracker;
    condor_tracker.py still has no profit-target exit, no stop-loss,
    and no IV check. This is the strongest case yet for actually
    building it live, but that is a real change to a currently-running
    strategy and needs an explicit decision, not a silent default.

## Condor IV-regime entry gate: strongest single lever found so far -- same "OOS carries it" caveat as the profit-target rule (added 2026-08-11)

## Condor IV-regime entry gate: strongest single lever found so far -- same "OOS carries it" caveat as the profit-target rule (added 2026-08-11)

Researched why iron condor selling is supposed to work at all in real
trading (the volatility risk premium -- CBOE's PUT/BXM indices show
option-writing strategies with lower vol and competitive returns vs
equities over 32 years). The dominant real-world entry filter for it,
per published research: iron condors entered when IV rank AND IV
percentile both exceed 50 measured 56.8% win rate vs 48.2% without any
filter, on 595 tracked symbols. condor_scanner.py has NO IV check at
all -- sells the same way every week regardless of volatility level.

Built india_vix_source.py: real India VIX daily history (free Yahoo
endpoint, same one global_cues.py already uses for the single live
point, extended to pull 5 years -- 1,229 real bars, 2021-08-11 to
2026-08-11) plus the two standard measures, IV RANK and IV PERCENTILE,
each computed only from days STRICTLY BEFORE the one being scored (no
look-ahead). Confirmed 2026-08-06's computed value (12.16) exactly
matches that day's own premarket brief.

FIRST PASS (retrospective correlation, NOT used to pick a threshold):
joined our real 87 condor trades against real VIX history. Above-
median entry days: n=43, +Rs 17,227 net. Below-median: n=43,
-Rs 52,166 net. A large, real difference -- but the median (21.2) was
computed FROM this same sample, which a live decision could never have
known in advance. Not adopted on this basis alone.

PROPER TEST: added CondorPolicy.min_iv_rank/min_iv_percentile/
vix_history to shadow_condor.py (fails CLOSED on missing/insufficient
VIX data -- an unevaluable filter is not evidence of a favourable
regime), then tested only FIXED, pre-specified thresholds -- literature's
own number (30/50) and simple round numbers (20, 50) -- never a
threshold derived from this sample. Full 493-day period, current live
config, real p90 costs:

    filter                          n     win%    NET
    baseline (no filter, live)      87    70.1%   -32,217
    min IV rank >= 20                48    77.1%   +9,405
    min IV percentile >= 50          40    77.5%   +8,778
    literature's rank>30 AND pct>50  22    77.3%   -4,389

Simple round-number thresholds work; the literature's precise dual
threshold does not transfer to NIFTY -- same lesson as everywhere else
this session: published numbers from a different market don't carry
over without recalibration.

Walk-forward on rank>=20:

    period          baseline net    rank>=20 net
    IS (2024-25)       -35,482         -10,457
    OOS (2026)          +3,265         +19,862

Replicates in both periods independently (+Rs 25k IS, +Rs 17k OOS) --
real, not a fluke of one window.

CAVEAT, stated directly: this is the SAME shape as the profit-target
finding below -- helps in both periods, but IS alone stays net
negative either way, and OOS (2026) is doing the heavy lifting both
times. Two independent fixes both showing this exact pattern raises a
real possibility not yet ruled out: 2026 may simply have been a more
favourable regime for condors generally (lower realized vol, less
whipsaw), and both "fixes" are partly riding that tailwind rather than
correcting something structural. NOT distinguished yet from a genuine
edge. Next test: profit-target + IV-rank filter COMBINED -- likely the
strongest number yet, and also the one most at risk of compounding the
same regime-luck explanation rather than ruling it out.

Not wired into the live tracker. Backtest-only, same as the
profit-target rule -- a real change to a currently-running strategy
needs an explicit decision, not a silent default.

## Condor profit-target exit: real, consistent improvement -- NOT a full fix (added 2026-08-11)

## Condor profit-target exit: real, consistent improvement -- NOT a full fix (added 2026-08-11)

Confirmed the live condor has no stop-loss and no profit-target exit at
all -- condor_tracker.py only stages a breach warning for human review,
held to expiry otherwise (see the entry below). Researched general
iron-condor practice (tastytrade, thousands of real trades): the two
standard risk controls are a 50% profit-target close and a ~2x-credit
stop-loss, justified by gamma accelerating sharply inside the final
~21 days. Our condor structurally has no 45-DTE alternative (NIFTY is
weekly-only) -- every position already opens deep inside that danger
zone (MIN_DAYS_TO_EXPIRY_TO_OPEN=1, real entries ~6 days out) and holds
straight through it with neither control. Built both as OPTIONAL
shadow_condor.py backtest parameters (CondorPolicy.profit_target_pct /
stop_loss_credit_multiple) -- NOT wired into the live tracker -- to
measure before ever proposing that.

STOP-LOSS: measured inert for the current live config (23-30 band,
300pt hedge). Checked directly: the worst intraweek mark-to-market dip
across all 87 real trades was -Rs 2,278, which never even reaches a
1x-credit stop (-Rs 2,444). For this config, losses come from the move
AT settlement, not gradual intraweek deterioration -- a stop watching
the book each cycle structurally cannot catch that. Not the same
conclusion for every possible config; not re-tested against the wider
config grid.

PROFIT TARGET: real and replicates, but incomplete. Full 493-day
period, current live config, real p90 costs:

    rule              n     win%    GROSS         NET
    baseline (live)   87    70.1%   -18,941      -32,217
    PT 50%           107    78.5%   +24,732         -696
    PT 25%           155    85.2%   +11,534      -28,366

25% target has a HIGHER win rate than 50% but a WORSE net result --
closing too early means more round trips (8 leg-crossings vs. 4 for an
expiry settlement) against a smaller profit per trade. Higher win rate
is not the same as better, same lesson as every other result this
session.

Walk-forward (fit nothing here -- 50% is the researched round number,
not swept on this data -- but still split IS/OOS to check it
replicates):

    rule       IS (2024-25) net    OOS (2026) net
    baseline        -35,482             +3,265
    PT 50%          -15,786            +15,090

The improvement holds in BOTH periods independently (+Rs ~20k IS,
+Rs ~12k OOS) -- not a fluke of one window. But the older 18 months
STAY NET NEGATIVE even with the rule; only the recent ~7 months turn
solidly positive. The full-period "near breakeven" figure is being
pulled toward zero by that one strong recent stretch, not a uniform
fix. Read it as: profit-target genuinely helps, meaningfully, but does
NOT on its own turn this into a strategy with a validated edge across
the whole tested history -- a materially different, weaker claim than
the directional spread's 12-of-12 walk-forward result.

NOT wired into the live tracker. That would be a real behavior change
to a currently-running position and needs an explicit decision, not a
silent default.

## Iron condor: FAILS both tests the directional spread passed -- running live on negative evidence (added 2026-08-07)

## Direction-chase cooldown: shipped, measured before adopting (added 2026-08-10)

2026-08-10: momentum opened 8 CE trades, 8 different strikes (24550
through 24900), one after another as each got stopped out and the
chain shifted -- Rs -5,153 for the day. The existing re-entry gate
(REENTRY_PRICE_TOLERANCE_PCT, trade_tracker.is_repeat_of_stopped_plan)
never fired once: it's keyed by (strike, option_type), so no two of
those eight ever collided on its own terms. It was built for a
DIFFERENT pathology (2026-07-28: re-ran the same failed plan at the
same strike, same price) and was never meant to catch this one.

Measured against the real trade journal before adding anything (44
trades, 2026-07-21..2026-08-10): 93% of trades (41/44) belonged to a
same-day, same-direction cluster (2+ trades, same option_type, each
opened within 30min of the prior same-direction trade's CLOSE).
Actual cluster P&L: Rs -12,476, 14.6% win rate. A rule that blocks
continuation ONLY after a same-direction LOSS (matching
REENTRY_PRICE_TOLERANCE_PCT's own "only a LOSS arms this" philosophy --
a WIN isn't evidence the read was wrong) would have kept Rs +518
instead. Checked it wasn't just 2026-08-10 driving the result: excluding
that day entirely, actual was Rs -7,324 and the rule would have given
Rs +1,735 -- same direction, same order of magnitude, so the pattern
predates today's incident.

SHIPPED: config.DIRECTION_CHASE_COOLDOWN_MINUTES = 30 (matches the
measured clustering window, not separately tuned).
trade_tracker.is_direction_chase()/_record_direction_cooldown() --
direction-scoped (option_type only, any strike), loss-gated only,
state resets daily alongside the existing per-strike gate. Wired into
try_open_new_trade (the actual block), decision_log.py
(REJECTED_DIRECTION_CHASE, so it shows up in the audit trail like every
other rejection reason), and main_live.py's "no new trade" log
explanation.

CAVEAT: 44 trades is a thin sample and this is a within-sample
counterfactual (replaying the same trades, not a fresh backtest) -- it
shows the pattern has been bad SO FAR, not that the specific 30-minute
window is proven forward. Watch it the same way every other threshold
in this project has been watched before being trusted further.

## Iron condor: FAILS both tests the directional spread passed -- running live on negative evidence (added 2026-08-07)

Ran the condor through the identical two tests. It fails both, clearly.

TEST 1 -- REAL COSTS, current live config (23-30 band / 300pt hedge),
full 493-day period. Gross was already -Rs 18,941; costs make it worse:

    scenario    n    win%     GROSS        NET      taxes   spread
    median     87   70.1%   -18,941    -29,034      8,677    1,416
    p75        87   70.1%   -18,941    -30,811      8,677    3,193
    p90        87   70.1%   -18,941    -32,217      8,677    4,599

TEST 2 -- WALK-FORWARD (fit 2024-08..2025-12, test on 2026), NET @ p90:

    combo         IS net     OOS net    OOS RoR
    15-25/200    -52,556     -21,180     -8.24%
    15-25/300    -34,218     +12,066      4.39%
    15-25/400    +26,553     -15,501    -11.12%
    23-30/200     -8,274     -21,370     -7.97%
    23-30/300    -35,482      +3,265      1.04%
    23-30/400    +36,025      -8,407      -4.13%   <- IS winner
    30-45/200    -21,613     -15,774     -6.15%
    30-45/300    -26,183     -34,698     -9.82%
    30-45/400    -61,615     +12,184      4.10%
    40-60/200    -35,049      -8,268     -3.39%
    40-60/300     -9,747     -28,021     -7.62%
    40-60/400    -64,125     -17,873     -4.33%

  - only 3 of 12 combos profitable out-of-sample (spread: 12 of 12)
  - the in-sample winner (23-30/400, +Rs 36,025 IS) went NEGATIVE
    out-of-sample (-Rs 8,407) -- the textbook overfitting signature
  - ZERO combos positive in BOTH periods. Not one. Every combo that
    looked good in one period lost in the other, i.e. the sign itself
    is noise.

WHY THE COST MODEL HAD TO BE BAND-AWARE FOR THIS: the condor's hedges
sit 300pts OTM and price in single digits (verified against a real live
position: hedges at Rs 5.65 / Rs 8.40) where measured spread is 0.823%
median / 5.714% p90, vs its shorts' ~0.30%. spread_cost_study gained
MEASURED_SPREAD_BY_BAND + multi_leg_costs() so each leg is costed at
its own real band; a flat rate would have understated this materially.
Note the condor is HELPED by never auto-closing (condor_tracker only
stages breach warnings) -- every position settles at expiry, so it
pays 4 leg-crossings, not 8. Even with that advantage it loses.

STATUS: main_condor.py is still in the daily automation with an open
paper position. This is now the only strategy here running live
against measured negative evidence at every config tested. Flagged for
a decision -- not silently disabled, since that is the user's call.

RESOLVED 2026-08-26: decision made -- pull it. Removed main_condor.py
from automation/start_trading.ps1's $scripts array (no open position
existed at removal time -- state/condor_position.json was already
null). condor_*.py source, config_condor.py, and the research/*condor*
backtests are untouched, only the live automation entry is gone, same
treatment Bank Nifty condor already got. Bank Nifty condor was never
added to $scripts in the first place, so this closes out the only
condor variant that WAS live.

## Directional spread: PASSES walk-forward validation -- edge is real, config choice was noise (added 2026-08-07)

Follow-up to the cost study below. That entry's caveat was that the
live strike config (40-70/100) had been swept over the FULL history, so
its result couldn't separate real edge from sweep-fitting. Tested
properly: re-ran the whole 12-combo sweep on 2024-08..2025-12 ONLY,
then measured every combo on 2026 -- data the selection never saw. All
figures NET (real costs, p90 measured spread).

    combo          IS net      OOS net        OOS return-on-risk
    30-60/100     +31,117      +12,145              6.35%
    30-60/150     +36,091      +15,632              5.81%
    30-60/200     +35,367      +19,158              6.08%
    40-70/100     +47,228      +14,123              7.87%   <- currently live
    40-70/150     +67,038      +17,996              6.46%
    40-70/200     +79,157      +14,548              4.22%
    50-85/100     +40,813      +15,653              8.72%
    50-85/150     +75,127      +22,301              7.83%
    50-85/200     +91,138      +17,211              4.94%   <- IS winner
    65-100/100    +45,271      +20,826             12.00%
    65-100/150    +73,180      +27,643             10.27%
    65-100/200    +87,065      +29,142              7.82%

FINDING 1 -- THE EDGE IS REAL. All 12 combos profitable out-of-sample,
net of real costs. Overfitting collapses out-of-sample; this doesn't.
First strategy in this project to clear that bar (compare: every
commodity result and every MA-crossover variant died on a far weaker
check).

FINDING 2 -- THE CONFIG CHOICE WAS NOISE. The in-sample winner
(50-85/200) ranked 7th of 12 out-of-sample. Picking the best config
in-sample gave a middling one going forward. Harmlessly so, since
everything worked, but it confirms config selection here is not
signal.

FINDING 3 -- RAW RUPEES MISLEAD, BECAUSE THE COMBOS DON'T RISK THE SAME
AMOUNT. A 200pt hedge is a 200pt wing, i.e. roughly double the max loss
per trade of a 100pt one. The raw-rupee OOS "winner" (65-100/200,
+Rs 29,142) risked Rs 372,762 to earn it -- 7.82% return on risk,
6th of 12 -- while 65-100/100 earned Rs 20,826 on Rs 173,570 risked
(12.00%, best). Any future comparison across hedge distances must be
risk-adjusted or it is measuring position size, not edge.

WHAT REPLICATES ACROSS BOTH PERIODS (and what doesn't):
  - PREMIUM BAND: replicates cleanly. The 30-60 band is the WORST in
    both periods (IS avg ~6.3% RoR, OOS ~6.1%); 65-100 the best or
    near-best in both (IS ~12.3%, OOS ~10.0%). Trustworthy.
  - HEDGE DISTANCE: does NOT cleanly replicate. Narrower is better in
    3 of 4 bands OOS but only 2 of 4 IS, and the periods disagree on
    which bands. Weak evidence -- do not act on it.

NOT CHANGING THE LIVE CONFIG on this. 40-70/100 sits mid-pack and is
positive in both periods (11.60% RoR in-sample, 7.87% out). Switching
to whatever topped the OOS table would repeat the exact error Finding 2
just diagnosed, on a sample now looked at repeatedly. Also unmeasured
here: a higher premium band means a short strike closer to ATM, which
gets TESTED more often -- return-on-risk against max_loss says nothing
about that path risk. The one defensible read is that the lowest band
(30-60) is consistently worst, and the config already moved off it on
2026-08-02.

## Directional spread: SURVIVES real costs -- the first strategy here that does (added 2026-08-07)

shadow_directional_spread.py applies NO cost model -- its pnl_inr is
fully gross (no brokerage, STT, exchange/GST, and no bid-ask crossing,
since historical reconstruction prices every leg at LTP). So its
headline +Rs 78,006 was an upper bound, not a result. Now measured
properly.

SPREAD COST IS MEASURED, NOT ASSUMED. spread_study.py against 5 days of
real recorded order books (2026-08-03..07, 3,849 sampled cycles) inside
this strategy's own Rs 40-70 short-premium band:
median 0.259%, p75 0.311%, p90 0.340% -- notably TIGHTER than the
0.2-0.6% the older momentum cost-sensitivity analysis had assumed.

spread_cost_study.py charges both statutory/broker costs and the
measured spread, per leg on GROSS premium (not net credit -- both legs
cross their own book; charging the net would understate ~2.4x at these
leg prices), 4 leg-crossings when traded out and 2 when settled at
expiry (decided per trade by its own exit_reason, not assumed):

    scenario   spread%     n    win%     NET total   net ex-biggest
    gross          --    125   85.6%      +78,006             --
    median      0.259    125   85.6%      +62,380        +60,161
    p75         0.311    125   85.6%      +61,719        +59,502
    p90         0.340    125   85.6%      +61,351        +59,135

Even at the p90 spread it stays clearly positive, and it still survives
removal of its own biggest winner (+Rs 59,135) -- the check that killed
every commodity result and every MA-crossover variant.

NET (p90) robustness detail:
  - positive in all three calendar years: 2024 +3,200 / 2025 +44,028 /
    2026 +14,123
  - max drawdown only -Rs 5,589 against +Rs 61,351 final
  - 113 of 125 trades sit in the POST-SEBI-lot-change regime
    (+Rs 56,917), so this is not an artifact of a dead market structure
  - avg win +Rs 973 vs avg loss -Rs 2,378 at an 85.6% win rate --
    the classic credit-spread shape, with the win rate high enough to
    carry it

WORTH KNOWING: taxes/brokerage (Rs 12,336) dominate spread cost
(Rs 3,291-4,320) by roughly 3x. The bid-ask crossing everyone worries
about is NOT the main cost drag here; statutory + brokerage is. Total
cost drag is ~21% of gross.

STILL NOT VALIDATED, and the reason this isn't "adopt it": the
strike-selection config was SWEPT on this same data (commit dbb4904,
"Adopt sweep winner for directional spread strike selection 40-70/100"),
so some in-sample optimism is baked in and this result cannot separate
real edge from sweep-fitting. NEXT STEP: walk-forward validation -- fit
on 2024-25, test untouched on 2026 -- before any sizing discussion.
Live paper-trading so far is a much smaller sample than this backtest.

## Order flow: wired for reliability, book_imbalance recorded but NOT gated on (added 2026-08-07)

orderflow_feed.py has been running live for a while but nothing ever
read its output -- confirmed by grepping the whole codebase, only
orderflow_recorder.py's session-spread logging (used by spread_study.py,
an offline analysis tool) actually consumed it. Two changes:

1. dhan_source.py's live snapshot builder now prefers orderflow.py's
   real WebSocket book (bid/ask/bid_qty/ask_qty) over the REST chain's
   own best-effort field-name guessing (_BID_KEYS/_ASK_KEYS), per
   field, falling back to the REST-guessed value whenever orderflow
   has nothing for that exact contract. This improves the RELIABILITY
   of things already in use (fills via OptionQuote.buy_price/
   sell_price, config.MAX_SPREAD_PCT's liquidity screen via
   spread_pct) -- not a new trading rule, no adopt-after-evidence bar
   to clear.

2. decision_log.py now records orderflow.book_imbalance() and
   total_quantity_imbalance() on every logged candidate (opened or
   not), alongside everything else already logged there. Deliberately
   NOT gating anything on it yet -- consistent with every other signal
   this project has adopted (SCORING_MODE=momentum_only after a
   493-day study, tie-break measured and left unchanged, etc.), this
   needs to accumulate real readings against real outcomes before
   there's any evidence it predicts something. Revisit once enough
   history exists in decision_log.jsonl to actually measure it.

## Condor "reverse the trade" idea: tested properly, makes it WORSE not better (added 2026-08-06)

Live dashboard question: the condor's own backtest (87 trades,
2024-08-07 to 2026-07-27) shows 70.1% win rate but net -Rs 18,941 --
big infrequent losses outweighing small frequent wins. Asked whether
buying the same structure instead of selling it ("less loss, more
profit" shape) would flip that to a win.

A quick sign-flip of the credit side's P&L suggested yes (+Rs 18,941,
cost-blind). That was MISLEADING -- it used the condor's net_credit
(short premiums MINUS hedge cost) as if it were symmetric with a real
buyer's debit, but a genuine 2-leg long strangle has to pay the FULL
gross premium of both short strikes, a bigger number than net_credit.

Built shadow_long_strangle.py properly instead: same strike-selection
call as the condor (condor_scanner.find_condor_legs, same
SHORT_PREMIUM_MIN/MAX + liquidity screen), bought instead of sold, no
hedge legs (risk already capped at premium paid), walked forward
through the same day/expiry machinery as shadow_condor.py, same LTP-
fill assumptions, run over the IDENTICAL 488-day window as the
condor's own backtest for a fair comparison:

    strategy          n     win%    total
    long strangle    101    18.8%   -Rs 65,845
    condor (sold)     87    70.1%   -Rs 21,684  (fresh run, matches the
                                                  original bt_condor.json's
                                                  -Rs 18,941 closely)

BOTH lose money in this window -- but reversing to buying is WORSE,
not better. An 18.8% win rate means the underlying essentially never
moved far enough, fast enough, to justify what buying those options
actually cost; premium selling collects theta on the far more common
"nothing much happened" days, which is exactly why the condor's win
rate is so much higher even though its own calibration also isn't
profitable yet.

CONCLUSION: the condor's problem is NOT its credit-vs-debit direction.
Reversing it is not a fix -- it makes the result worse. If the condor
needs improving, that work is in entry timing / strike selection /
IV-regime gating, not the trade's directional structure. Not adopted;
NOT pursued further as a "buy instead of sell" idea specifically.

## Gold/Silver trend-following (Supertrend / MA crossover): every result but one is a single-trade artifact (added 2026-08-06)

Followed up on the zone strategy's failure (entry below) by building
genuine trend-following strategies instead: Supertrend and moving-
average crossover, both "always in the market, flip on reversal, no
fixed target" systems (commodity_trend_strategy.py). RSI deliberately
NOT used as the primary signal -- it's a mean-reversion oscillator and
would hit the exact same failure mode as the zone strategy in a real
trend (stays "overbought" for months); exposed only as an optional
filter, unused so far.

Swept Supertrend (period 7/10/14 x multiplier 2/3/4) and MA crossover
(10/30, 20/50, 20/100, 50/200) on both metals' full clean 5-year daily
series. Several combos looked excellent on the surface (Silver MA
50/200: n=3, +Rs 136,938; Silver Supertrend p14/m2: n=67,
+Rs 210,627). BEFORE trusting any of it, checked outlier concentration
(the same check applied to every backtest this session) -- every one of
these was 95-116% concentrated in its single biggest trade, meaning
the REST of that strategy's trades net LOST money. Confirmed by
re-running each series with its single biggest trade removed:

    strategy                  with outlier    without outlier
    Gold MA 20/100             +Rs 75,446        +Rs 1,067   (flat)
    Silver MA 50/200          +Rs 136,938        -Rs 7,641   (flips to loss)
    Silver MA 20/50           +Rs 110,169       -Rs 52,749   (flips to loss)
    Gold Supertrend p7/m4      +Rs 25,719       -Rs 24,465   (flips to loss)
    Silver Supertrend p14/m2  +Rs 210,627       +Rs 72,393   (SURVIVES, n=66)
    Silver Supertrend p10/m2  +Rs 205,709       +Rs 68,471   (SURVIVES, n=66)

The one common outlier across most of these is the same event: silver's
Nov-2025-to-Jan-2026 squeeze, Rs 153,691 -> Rs 291,925 (+90%) in 81
days -- the single most extreme move in the whole 5-year dataset. Every
strategy that happened to be long through it looks great; that says
"this was long during the outlier," not "this strategy has edge."

ONLY Silver Supertrend (both period/multiplier combos tested) stays
net POSITIVE across a real sample (66-67 trades) even with that one
trade removed (+0.024-0.025R expectancy) -- thin, but the one result
here that isn't purely a single-event artifact. Gold Supertrend and
every MA-crossover combo do NOT survive the same check.

NOT ADOPTED as a live candidate yet -- this is one in-sample backtest
result on a genuinely thin universe (2 symbols, ~5 years, one
dominant historical event). Silver Supertrend is the only credible
candidate worth further work (forward/paper testing, not more
in-sample parameter tuning on the same data -- the same discipline
applied to every other strategy this session before trusting it).

FOLLOW-UP: does EMA instead of SMA fix the crossover strategy's
outlier-dependence? commodity_trend_strategy.ema_series() added,
backtest_ma_crossover(ma_type="ema") wired through. Swept the same
4 fast/slow pairs on both metals -- no. Every single EMA combo also
flips to a loss once its biggest trade is removed:

    Gold EMA 20/100:    +Rs 79,057  -> -Rs 3,471
    Silver EMA 20/100:  +Rs 94,421  -> -Rs 28,100
    (same pattern across every other combo tested)

Switching the MA type doesn't touch the underlying issue -- MA
crossover (SMA or EMA) has no demonstrated edge on this data once the
one lucky trend catch is excluded. Only Silver Supertrend still
survives the same check.

## Gold/Silver futures: price-action zone strategy essentially never fires, structural mismatch not a calibration problem (added 2026-08-06)

Tried backtesting the same zone+break price-action strategy directly on
the futures (shadow_commodity_futures.py, weekly zones / daily trigger
-- see the entry below this one for why futures instead of options).
Zero trades at NIFTY's tuned SR_CLUSTER_TOLERANCE_PCT=0.15%. Traced why:
every weekly swing point across gold's entire 5-year series landed at a
UNIQUE price -- not one pair of swing highs (or lows) fell within 0.15%
of each other, so zero support/resistance zones ever formed (the rule
requires >=2 touches within tolerance to call something a zone at all).

Widened the clustering tolerance step by step to test whether this was
a calibration problem: 0.5% and 1.0% still found zero qualifying
zones; even at 2-3% (13-20x NIFTY's tuned value) gold produced exactly
ONE trade across 1207 daily bars, and silver 2-3. That is not a sweepable
parameter -- it is confirmation that the mismatch is structural, not a
tolerance-tuning problem. This rule is built on "price reaches a zone
it has touched before, reacts, then breaks the reaction" -- gold and
silver spent this entire window in an unusually persistent one-way bull
run (Gold Rs 47,068 -> Rs 145,234/10g, Silver Rs 63,573 -> Rs 227,584/kg),
which structurally has almost none of the "return to the same level
multiple times" behaviour this strategy depends on.

NOT ADOPTED, and not swept further -- sweeping a strategy that produces
1-3 trades total over 5 years would just be curve-fitting to noise, the
same overfitting risk already documented twice this session. A
genuinely different strategy shape (trend-following/breakout rather
than mean-reversion-to-zone) is the honest next step if this asset
class is worth pursuing, not more parameter tuning on this one.

## Gold/Silver futures history: pulled, mostly clean, but has real rollover-seam duplicate dates (added 2026-08-06)

Backfilled 5 years of MCX daily futures candles for GOLD and SILVER via
commodity_source.py (logs/commodities/gold_daily.json,
logs/commodities/silver_daily.json) -- 1221 and 1202 bars respectively,
2021-09-01 to 2026-08-05. Much simpler pull than Nifty/Bank Nifty's
options: Dhan's /charts/historical, queried against the CURRENTLY
listed contract's own security id with exchangeSegment=MCX_COMM,
already returns a genuine continuous/rolled series -- confirmed by
sampling the price trajectory (Gold Rs 47,068 -> Rs 145,234/10g,
Silver Rs 63,573 -> Rs 227,584/kg, both smooth multi-year rallies, no
periodic rollover-shaped jumps). No ATM-offset reconstruction needed.

NOT perfectly clean, though: 23 duplicate calendar dates in Gold's
series, 4 in Silver's (out of 1200+ bars, ~1-2%). Some duplicates are
byte-identical repeats (harmless). Others are NOT -- e.g. Gold
2023-05-05 has two different OHLC sets for the same date
(O=62054/H=62056/L=60663/C=61074 vs O=61566/H=61629/L=60250/C=60628),
clustered right around early May 2023 across several consecutive
dates for both metals. Most likely a contract-rollover seam: Dhan's
stitched continuous series briefly carrying both the expiring and the
newly-front contract's price for the same calendar date. NOT
deduplicated yet -- picking one of two genuinely different price sets
without understanding which is "real" would just substitute one
assumption for another. Any backtest against this data should either
exclude these ~27 dates or treat them explicitly, not silently take
whichever bar happens to load first.

## Bank Nifty price-action backtest: first run not trustworthy, contamination far more pervasive than the near-ATM measurement suggested (added 2026-08-06)

First real backtest of the price-action strategy against Bank Nifty data
(5 years, 2021-08-04 to 2026-08-04, 1 lot, lot size 30, premium band
Rs 300-800 calibrated to Bank Nifty's real prices -- NIFTY's tuned
40-200 band would find zero Bank Nifty candidates ever, checked directly
before running).

RAW RESULT:

    pair            n    win%    expectancy    total P&L
    intraday        67   52.2%   +0.75R        +Rs 85,131
    daily_hourly    59   32.2%   -0.53R        -Rs 90,830

NOT ADOPTED AS A VERDICT. The entry below this one measured Bank Nifty's
near-ATM (within 300pts) contamination rate at 0.246% of readings --
small. But checking the DAYS actually used by these trades (not just
near-ATM readings in general) found something much worse: the
consistency checker (`historical_consistency.check_day`) flags 49 of 66
trading days used by the intraday pair (74%) and 35 of 42 days used by
daily_hourly (83%), frequently with corruption within 100-300pts of
spot -- squarely inside the range these strikes trade in. Confirmed one
used day (2023-12-11) had an outright implausible reconstructed spot
(~10,011 against a real level of ~47,200).

Checked precisely whether this actually touches the number: for the top
5 P&L-driving trades in each pair, none had their OWN exact strike
directly hit by a flagged issue on their entry day -- the biggest
winners/losers aren't provably fabricated. But with the majority of days
flagged, that's reassurance about 10 specific trades out of 126, not
about the aggregate. Unlike the NIFTY and near-ATM Bank Nifty entries
below (both "not fixed, low enough to proceed"), this rate is too high
to treat +Rs85K / -Rs90K as a signal on the strategy -- it's evidence
the mechanics run end-to-end, nothing more yet.

NEXT STEP -- DONE: re-ran excluding trades whose entry day has a flagged
issue within 300pts of spot (the same inner band the near-ATM
contamination rate below was measured over).

    pair            subset      n     win%    expectancy    total P&L
    intraday        all         67    52.2%   +0.75R        +Rs 85,131
    intraday        clean       38    55.3%   +1.38R        +Rs 102,106
    intraday        cut(dirty)  29    48.3%   -0.09R        -Rs 16,975
    daily_hourly    all         59    32.2%   -0.53R        -Rs 90,830
    daily_hourly    clean       52    34.6%   -0.23R        -Rs 41,845
    daily_hourly    cut(dirty)  7     14.3%   -2.74R        -Rs 48,985

READ: the intraday pair's positive result is NOT a contamination
artifact -- the clean subset is stronger (+1.38R over 38 trades), and
the days near flagged corruption were roughly breakeven, not the
source of the gain. The daily_hourly pair's badness WAS partly
contamination -- 7 dirty-day trades average -2.74R and account for over
half its total loss -- but it's still net negative on the clean 52
trades alone, so this pair genuinely doesn't work here, not just a data
artifact.

Still a first-pass premium band (Rs 300-800), not swept/optimized --
treat the intraday clean-subset number as promising evidence to
calibrate further, not a final verdict.

## Bank Nifty historical data: same corruption mechanism as NIFTY, more frequent (added 2026-08-06)

Investigated why Bank Nifty's near-ATM contamination (0.246% within
300pts / 3 strikes) is non-zero, unlike NIFTY's clean 0.000% in the
equivalent inner band -- see the NIFTY corruption entry below for the
original finding this follows up on.

ROOT CAUSE, same bug as NIFTY, confirmed by direct inspection: Dhan's
`ATM+N` offset labelling doesn't cleanly track which real strike each
offset points to as spot crosses a strike boundary. NIFTY showed this as
two adjacent strikes sharing identical OI at the SAME instant. Bank
Nifty showed a second variant on top of that -- a value MIGRATING to the
adjacent strike ACROSS TIME:

    10:10  47600CE OI=12075   47700CE OI=12075   (same instant, adjacent strikes)
    10:15  47500CE OI=11445
    10:20  47600CE OI=11445                       (same value, one strike up, 5 min later)

This second instance lines up exactly with spot jumping 47650->47786
(136pts) in that one 5-minute bar -- a rapid crossing of the 47700
strike line, which is the trigger condition for the offset->strike
mapping to misfire.

WHY MORE FREQUENT FOR BANK NIFTY: its absolute price level is roughly
2x NIFTY's over this period (~47,000-58,000 vs ~24,000-25,000), so its
typical point move per 5-min bar is proportionally larger even at
similar underlying volatility -- meaning it crosses its (wider, 100pt)
strike boundaries more often per unit time than NIFTY crosses its
(narrower, 50pt) ones. More boundary crossings -> more chances for the
mapping to misfire.

NATURE OF THE ERROR: the OI values themselves are real numbers that
existed at some point -- they are momentarily mis-attributed to the
wrong strike for one bar, not fabricated. LTP stayed correct in every
case checked.

NOT FIXED, same reasoning as the NIFTY entry: still low overall
(~1 in 400 near-ATM readings), concentrated in single-bar blips, and
does not change which strategies are exposed -- see that entry's
per-strategy distance table; Bank Nifty's own future strategy legs would
need the same distance check once one exists.

## Score tie-break: made explicit, measured, NOT changed (added 2026-08-06)

Found analysing the 2026-08-05 session: all four momentum trades opened
deep-OTM PEs hugging the premium floor (11.30, 13.80, 15.90, 19.25).

MECHANISM (confirmed, reproduced against the recorded snapshot): momentum
ROC is a CHAIN-WIDE read, so under SCORING_MODE="momentum_only" every
qualifying strike on a side scores identically -- 14 PE strikes all tied
at exactly 6.0 that cycle. Near-ATM 24550/24600 had far stronger OI
buildup (+343%, +203% vs the winner's +111%) and cheap rather than rich
IV (6th/3rd vs 78th percentile), and none of it could break the tie
because none of it feeds the score. The winner fell to Python's stable
sort preserving CHAIN ORDER, which resolves ASYMMETRICALLY by side:
ascending strike = "cheapest, deepest OTM" for a PE but "most expensive
under PREMIUM_MAX" for a CE.

The asymmetry is REAL and PERSISTENT, not a one-day artifact -- over the
full 6 years, chain_order's median entry is Rs 82.2 for CEs but Rs 30.8
for PEs (2.7x).

MEASURED, 1,491 days / ~10,800 trades per mode:

    mode                gross R    spread R    net R    med entry
    chain_order         +0.1227     0.0118    +0.1108      51.0
    nearest_atm         +0.1171     0.0105    +0.1067      67.4
    highest_oi_change   +0.1079     0.0110    +0.0969      61.8

  Significance vs chain_order: nearest_atm z=-0.26, highest_oi_change
  z=-0.70. NEITHER is significant (bar is 1.96). All three modes are
  statistically indistinguishable.

A spread-cost hypothesis was tested and REJECTED. The 6-year backtest
fills at LTP with no bid/ask, and costs.round_trip deliberately omits
spread (it assumes bid/ask fills already carry it), so spread is charged
nowhere. Real spread measured from a live session (2026-08-05, 37,888
quotes with a live book) is >2x wider on deep-OTM than near-ATM:

    under Rs 20  0.60%     Rs 40-70   0.28%     Rs 120+  0.27%
    Rs 20-40     0.34%     Rs 70-120  0.24%

The expectation was that charging this would penalise chain_order's
deep-OTM bias and flip the ordering. It did not: chain_order's aggregate
median entry is Rs 51, not Rs 11, because the CE/PE asymmetry partly
CANCELS (deep-OTM PEs pay wide spreads, near-ATM CEs pay tight ones).
The between-mode spread difference (0.0013R) is smaller than the
already-insignificant gross difference (0.0056R).

DECISION: default stays "chain_order". There is no evidence to justify
changing it, and the 493-day study that adopted momentum_only ran with
this behaviour. What changed is that the tie-break is now EXPLICIT,
configurable and tested rather than an accident of list order -- and we
now know it does not measurably affect P&L.

OPEN HYPOTHESIS, deliberately NOT adopted: per-side tie-breaks looked
better in both directions (nearest_atm on CEs +0.1401R vs +0.1210R;
chain_order on PEs +0.1242R vs +0.0975R). Both gaps are within noise at
n~5,000, and picking the best of a 2x2 grid on the same data it was
measured on is exactly the in-sample overfitting this project's own
condor entry warns about. Would need out-of-sample or live confirmation
before being taken seriously.

## Historical data corruption is real but confined to far-OTM strikes (added 2026-08-04)

Root-caused after the consistency checker was fixed to flag only
non-persisting spikes (see `historical_consistency.py`). Findings, all
measured rather than assumed:

  - The corruption is genuine. At flagged bars, ADJACENT STRIKES CARRY
    IDENTICAL OI -- e.g. 2024-10-23 11:10, strikes 24050 and 24100 both
    read exactly 33,450 despite different LTP and IV. Two distinct
    contracts cannot share open interest. IV also frequently reads 0.00
    on exactly those bars.
  - It concentrates at the EDGE of the ATM+/-10 offset window that
    `historical_source.py` fetches: 79% of spikes sit within 4 strikes
    of the window edge, and none at all deeper than 8 strikes in.
    Consistent with the offset->strike mapping slipping as spot moves
    across a rounding boundary, which the edge offsets cross most often.
  - Contamination by distance from spot (50 sampled days per dataset):

        band          2024-08..2026-08     2020-08..2024-07
        0-150pts          0.000%               0.000%
        150-250pts        0.000%               0.133%
        250-350pts        0.013%               0.526%
        350+pts           0.293%               1.059%

WHAT THIS MEANS PER STRATEGY (using real live leg distances):
  - Momentum and price-action both select near-the-money inside a
    premium band; their strikes sit within ~150pts of spot, where
    contamination measured ZERO in both datasets. Backtest conclusions
    for these two stand.
  - Directional spread's live legs sat 13pts and 87pts from spot --
    also inside the clean band.
  - IRON CONDOR IS THE EXPOSED ONE. Its live legs sat 37 / 337 / 613 /
    913 pts from spot: three of four are in the contaminated zone, the
    hedges worst of all. Condor backtest results over reconstructed
    history should be treated as unreliable, and this compounds the
    coverage gap already documented for it (its wings routinely fall
    outside the ATM+/-10 window entirely). Worth remembering that 36
    swept condor configs never produced a tradeable edge -- these
    results were partly built on the dirtiest slice of the data.

NOT FIXED, deliberately: the reconstruction is not rewritten to repair
these bars. The clean-band evidence says it isn't needed for three of
four strategies, and the condor has no adopted config to protect. If
the condor is ever revisited seriously, fix the data first -- a
same-strike-adjacent-duplicate detector could repair or drop the
affected bars rather than pass them through silently.

## Verify whether Dhan exposes a real settlement price (added 2026-08-04)

2026-08-04 incident: condor and directional spread both had their own
contract expiry that day and both sat unsettled past close, traced to
two compounding issues -- see `main_condor.py`'s `EXPIRY_SETTLEMENT_CUTOFF`
comment and `tests/test_expiry_settlement_timing.py` for the full
evidence trail. Fixed for now: settlement fires at 15:40 (NSE's real
F&O close, confirmed against a published schedule) instead of waiting
for the nominal 15:30 `market_is_open()` transition, using the last
available LTP at that point.

That "last available LTP" is still a proxy, not verified to be the best
one. Checked directly: option premiums in the 15:15-15:40 window swing
hard (one strike moved 85% in 11 minutes on 2026-08-04), so whichever
single LTP snapshot happens to land closest to 15:40 could still be an
outlier rather than a true closing/settlement price. Dhan's option-chain
response was not inspected field-by-field for a dedicated settlement-
price field distinct from `last_price` -- attempted 2026-08-04 evening
but the live `/v2/optionchain/expirylist` endpoint only responds during
market hours, so this returned a 401 unrelated to the token itself.

Next live session: probe the raw Dhan option-chain JSON response
(`dhan_source._fetch_raw_chain`) for any field beyond `last_price` that
might carry a real exchange-published settlement value, and switch
expiry settlement to use it if one exists.

## Forward-validate the pure price-action live runner (added 2026-08-04, updated same day)

`main_price_action.py` was built 2026-08-04, ahead of this project's own
usual bar ("build once the backtest edge holds without an outlier, or a
larger sample confirms it" -- see the original version of this entry
below the strikethrough). Built at the user's explicit request despite
that caveat, not because the 2-year backtest cleared it (19-24 distinct
setups, 26% drawdown, one outlier trade for ~half the P&L).

UPDATE 2026-08-04, same day: the backfill was extended to the real
6-year depth (2020-08-03..2026-08-04, 1,490 days -- see the historical-
data-corruption entry above) and the sweep re-run. Full results, both
pairs, 1 lot, R:R 1.0-4.0:

    daily_hourly: n=140-225 per cell, win 32.9-50.2%, expectancy
      +0.06 to +0.44R, total Rs 1,459-38,065 (rises with R:R)
    intraday:     n=88-108 per cell, win 44.3-68.5%, expectancy
      +0.62 to +0.94R, total Rs 63,087-96,167 (rises with R:R)

Investigated the "outlier-dependent" concern directly rather than
re-assert it: intraday@1:3's 9 weekend-gap trades (opened Friday
afternoon, resolved Monday) account for 62% of that cell's total. But
checked whether those fills were real or backtest artifacts by looking
at price action for the 30 minutes AFTER each recorded fill -- 7 of 9
PERSISTED (the price held near the fill level, including the actual
2024-08-05 NIFTY selloff), only 2 of 9 showed any reversion, and both
of those were small. Conclusion: carrying a stop/target position across
a weekend is not a bug in either pair (neither has a same-day-exit rule
in the code, and none should be added -- an options market that doesn't
trade over the weekend legitimately resolves at the next available
price, gap and all). The 6-year numbers are trusted as reported, not
provisional on stripping outliers.

Both pairs positive at every R:R tested, both consistent in shape with
the earlier 2-year read (intraday stronger per-trade, daily_hourly more
frequent). Sample size grew roughly proportionally with the 3x longer
window (497->1,490 days, 19-24->88-225 trades per cell), which is
itself mild evidence the edge isn't a fluke of the shorter window.

Still open before trusting live output further:
  1. Watch live/paper results accumulate for real -- even 88-225
     historical trades per cell is a backtest sample, not a live one,
     and this project's own standard is to prefer live confirmation
     over any amount of backtesting.
  2. See the historical-data-corruption entry above: momentum and
     price-action both trade near-ATM (inside the zero-contamination
     band), so this sweep is NOT exposed to the far-OTM data issue that
     compromises the condor's numbers.

`config_price_action.py` carries TOTAL_CAPITAL=50,000, MAX_LOTS_PER_TRADE=1.
MAX_RISK_PER_TRADE_PCT is 15% (not momentum's 1%) -- with lots hard-
capped at 1, this constant only ever GATES whether a single lot is
affordable, never scales size up, and at 1% it silently sized every
signal to 0 lots against this strategy's own PREMIUM_MIN/MAX band (see
that constant's own comment; caught by
`test_forced_signal_opens_a_tracked_position_via_auto_approve` before
it could ship as a strategy that looks alive but never actually trades).

## Run every strategy on Bank Nifty as well as NIFTY (added 2026-08-04)

Currently all four strategies (momentum, condor, directional spread,
price-action) trade/analyze NIFTY only. Extending to Bank Nifty is a
real, multi-part build, not a config flag:

  - `banknifty_context.py` already has Bank Nifty's INDEX security ID
    (25, IDX_I segment) and fetches its candles -- but only as a
    divergence/context signal for the NIFTY scanner, never to trade Bank
    Nifty options itself. That scaffolding does not extend to the
    option CHAIN.
  - `dhan_source.py`'s option-chain fetch, `historical_source.py`'s
    NIFTY_SECURITY_ID=13 reconstruction, and `instrument_master.py`'s
    lookups are all hardcoded to NIFTY today and would need an
    underlying-aware parameter, not a second copy-pasted module per
    strategy per index (that path already caused real bugs once in this
    project when config got duplicated instead of parameterized -- see
    `detect_support_resistance`'s config-coupling bug fixed 2026-08-03).
  - Bank Nifty's lot size is NOT 65 (NIFTY's) and has changed over time
    on NSE's own schedule -- must be looked up fresh from Dhan/NSE at
    build time, never assumed from memory or copied from NIFTY's
    config.
  - Every existing config file (`config.py`, `config_condor.py`,
    `config_directional_spread.py`, `config_price_action.py`) currently
    encodes NIFTY-specific premium bands, strike spacing, and capital
    assumptions that do NOT automatically transfer to Bank Nifty's
    different price level, strike spacing (100pt, not 50pt), and
    volatility profile -- each strategy needs its own Bank Nifty tuning
    pass, not a shared multiplier.
  - `historical_source.py`'s ATM+/-10 strike-offset cap is a hard API
    limit (see its own docstring) -- Bank Nifty's wider 100pt spacing
    means that same +/-10 offset covers a WIDER points-from-spot window
    than NIFTY's 50pt spacing does, which changes each strategy's
    historical-data coverage math and needs re-deriving per strategy,
    not assumed to carry over unchanged.

Scope this as one design pass across all four strategies before writing
code for any single one -- an underlying-aware `Underlying` config/
security-id abstraction shared by every strategy's data source and
scanner, rather than four independent Bank Nifty forks that could each
drift differently from their own NIFTY counterpart. Sequencing: do this
AFTER the price-action live runner above and NIFTY forward-validation of
the two adopted re-tunings (directional spread's strike selection,
momentum's SCORING_MODE) below -- Bank Nifty adds a second full
evaluation surface on top of strategies whose NIFTY behavior isn't fully
forward-validated yet, and stacking that now would make any surprising
result impossible to attribute to "the strategy" vs. "the new index."

## Forward-validate directional spread's re-tuned strike selection (added 2026-08-02)

`config_directional_spread.py`'s SHORT_PREMIUM_MIN/MAX and
HEDGE_DISTANCE_POINTS changed to 40-70/100 (from 30-60/150) based on
`sweep_spread_config.py`'s 493-day, 2-year grid -- see README.md's
2026-08-02 entry for the full evidence (12 cells, all profitable, new
config has the best return/drawdown ratio in the grid).

Same caveat as the SCORING_MODE entry below: this is an IN-SAMPLE
result. The grid was built from data the sweep also selected from, LTP
fills only (no bid/ask in historical data, and a spread crosses 4
leg-transactions per round trip), and no untouched data remains to check
it against. Watch actual live fills, credit collected, and whether the
2:1 win/loss ratio holds forward before trusting the backtested total.
Revert to the old values (in that file's own comment) if it doesn't.

## Iron condor: no config adopted after 3 sweep rounds / 36 configs tested (closed 2026-08-02)

`sweep_condor_config.py` was run three times against the same 493-day
history, progressively narrowing hedges and raising the premium band in
the direction each round suggested:

  1. Original grid (premium 23-90, hedge 150-300): best cell z=0.27.
  2. Finer grid (premium 50-100, hedge 100-200): best cell z=0.95,
     coverage gap down to 25% (from 64-84%) -- confirmed narrower hedges
     genuinely fix the coverage problem, as hypothesized.
  3. Pushed further (premium 80-150, hedge 50-100): best cell (115-150
     premium, 75-100 hedge) z=1.72, Rs 44,096 total.

Best-ever result (z=1.72) still doesn't clear a plain single-test 95%
bar (1.96), let alone the ~3.2 Bonferroni bar 36 cumulative comparisons
demand. More importantly: the cells that scored best did so by pushing
the premium band to 115-150, which drops win rate to 37.5% (from 70% at
the original config) -- that isn't a better-tuned condor anymore, it's a
qualitatively different, near-the-money strangle. A parameter sweep
optimizing one metric shouldn't be the thing that decides to change what
the strategy fundamentally IS; that's a judgement call, not a tuning one.

CLOSED, not left open-ended: narrowing the coverage gap was confirmed to
work (25% vs the original 64-84%) but did not, by itself, surface a
tradeable edge. Nothing here is adopted; the condor stays at its
original baseline config. Only reopen this with a genuinely new input --
historical data wider than the ATM+/-10 (~500pt) reconstruction cap that
removes the coverage constraint entirely, not another parameter sweep
against the same data.

## Forward-validate SCORING_MODE = "momentum_only" before trusting its size (added 2026-08-02)

Adopted live 2026-08-02 as the default scoring mode -- see config.py's
`SCORING_MODE` docstring and README's 2026-08-02 entry for the full
evidence trail (493-day, 2-year forward-return study; momentum ROC
alignment the only component that survived Bonferroni correction across
41 tested; re-weighted variants backtested with the daily-loss and
exposure gates actually enforced).

This is an IN-SAMPLE result. The variant was designed by studying the
same 493 days it was then tested on, the underlying data has no bid/ask
(LTP fills, so every figure is an optimistic ceiling), and it trades
~5x/day versus the legacy scorer's ~1x/day, which compounds the LTP-fill
optimism roughly proportionally. No untouched data remains to check it
against. Forward (live) results are the only real test left -- until a
meaningful number of live sessions confirm it, treat the backtested
total (+Rs 470,031 / 493 days at Rs 5L capital) as a ceiling, not an
expectation, and watch the daily-loss breaker: the backtest breached it
on 11 of 493 days.

Switching back to `SCORING_MODE = "legacy"` is a one-line, fully-tested,
reversible change (see tests/test_scoring_mode.py) if live results
diverge materially from the backtest.

## Fast position-check: lightweight LTP endpoint (added 2026-07-27, DONE 2026-08-18)

DONE -- see the "Dhan request demand" entry near the top of this file
for the full writeup. `check_open_trades_fast()` in all four momentum
processes now prefers `orderflow.py`'s live WebSocket book and falls
back to a batched `POST /v2/marketfeed/ltp` (`dhan_source.get_ltp_batch()`)
only for whatever orderflow misses, resolving security IDs via
`instrument_master.py` exactly as anticipated here.

## Broker-side protective stop-loss

Discussed at length (see README's "silent freeze" incident notes): even
with `supervisor.py` auto-restarting on crash/freeze, there's a gap
between "the process dies" and "the supervisor notices" where an open
position has zero protection. The only way to close that gap completely
is a stop-loss order sitting on the exchange itself (a Dhan GTT order
placed at trade entry) — a deliberate, not-yet-made decision to start
placing real orders, since everything currently here is analytics/
tracking only.

## Order flow — feed BUILT 2026-08-02, not yet wired into any strategy

`orderflow_feed.py` (WebSocket process), `orderflow_packets.py` (binary
decoder), `orderflow.py` (read side) and `instrument_master.py` are
built, tested and validated against live data — see README's 2026-08-02
entry. **Nothing consumes them yet**: no strategy reads the book, and no
scoring or gating uses it. That wiring is the remaining work, and it is
deliberately separate so the feed can be observed on its own first.

Two things worth resolving before wiring it in:

  - **Measure real intraday spreads.** TOOLING BUILT 2026-08-02
    (`orderflow_recorder.py` records every sample tagged with its market
    phase; `spread_study.py` analyses them), but the measurement itself
    still needs a live session — recording is on by default whenever
    `orderflow_feed.py` runs, so it just needs the feed up during market
    hours.

    Why it matters: the only capture so far is post-close (17:16), median
    **0.856%**, p90 1.591%. That is NOT a usable estimate — spreads widen
    when nobody is quoting, and `spread_study.py` deliberately refuses to
    report a trading-cost figure from non-regular-session samples. But it
    sits above the 0.2–0.6% band assumed in the momentum cost-sensitivity
    analysis, where 0.6% already cut net expectancy from +0.104R to
    ~+0.062R per trade. If regular-session spreads land near 1%, **every
    LTP-priced backtest total in this project is more optimistic than
    currently documented** — momentum_only's Rs 470,031 included. Run the
    feed through one full session and check before trusting those totals.
  - **Decide what the signal is actually for.** Dhan's feed carries the
    order BOOK (resting size), not a trade tape with aggressor flags, so
    cumulative-delta / footprint order flow cannot be built from it. Book
    imbalance measures intent to trade at a price, and resting orders can
    be pulled. Whether that predicts anything here is an open question
    that deserves the same forward-return treatment
    `component_study.py` applied to the momentum scorer — not an
    assumption that "order flow is informative".

## Dhan rate limiting across three concurrent processes (observed live, 2026-07-30)

Real log from a live `main_condor.py` session:

```
[10:57:14] (dhan) NIFTY spot 24350.7
  [data source] dhan failed, falling back: 429 Client Error: Too Many Requests for url: https://api.dhan.co/v2/optionchain
  [data source] nse failed, falling back: 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  Error this cycle (will retry next cycle): 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
```

UPDATE: the original version of this entry conflated two separate
failures. The second one -- MTM reading "unavailable" even on cycles
where the chain fetch clearly succeeded -- turned out to be a real bug
in our own code (`STRIKE_RANGE_POINTS` silently dropping an
already-tracked strike as spot drifted), **fixed** -- see the README's
"Fixed: 2026-07-30" entry. What's left in this backlog item is only the
genuine external-service problem: both tiers occasionally failing
together with no snapshot at all for that cycle. Same root causes,
still diagnosed, not yet built:

**1. Dhan 429 (rate limit). FIXED 2026-07-31.** Confirmed the problem
reached beyond `main_condor.py`: on 2026-07-31 the same rate-limit
storm hit `main_live.py` directly while a real trade was open -- 48 of
72 failure-related log lines that day landed during that one position,
including the 5s fast-check itself failing outright ("Both dhan and
nse sources are in cooldown"). Nothing broke only because that trade
never approached its stop/target during the gaps -- luck, not the
system working as designed. A stop/target check silently not running
during a real approach is a risk to the P&L data this project's whole
measurement effort depends on.

  Built both fix directions:
  - `dhan_rate_limiter.py`: a file-lock-based cross-process request
    spacer. Before every Dhan HTTP call, each of the three processes
    now calls `wait_for_slot()`, which checks a shared state file for
    the wall-clock time of the last Dhan request **by any process**
    and sleeps out the remainder of `MIN_INTERVAL_SECONDS` (3.5s) if
    needed. Uses `os.O_CREAT | os.O_EXCL` for an atomic, portable
    advisory lock; force-clears locks older than `STALE_LOCK_SECONDS`
    (10s) so a crashed owner can't deadlock the other two processes;
    gives up and proceeds without the lock past
    `MAX_ACQUIRE_WAIT_SECONDS` (8s) rather than ever blocking a trading
    loop indefinitely over a coordination mechanism. Wired into all 4
    of `dhan_source.py`'s `requests.post()` call sites. Covered by
    `tests/test_dhan_rate_limiter.py` (spacing enforcement, stale-lock
    clearing, fail-open behavior under contention).
  - `config_directional_spread.py`'s `POLL_INTERVAL_SECONDS` widened
    30 -> 90: this strategy's entry signal is a bias score that doesn't
    move within seconds like the momentum scanner's setups do, so
    there's no accuracy cost, and it cuts this process's share of
    shared Dhan request volume to a third of what it was.

  What this does NOT fix: NSE fallback tier blocking (#2 below) --
  unrelated, external, and not being pursued (see that item).

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
