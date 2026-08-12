# Backlog — before going live with real capital

Things that are working and acceptable during the evaluation/testing
phase, but worth revisiting before real money is on the line.

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
