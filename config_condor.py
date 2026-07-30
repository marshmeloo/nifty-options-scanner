"""
Config for the iron condor (short strangle + protective wings) strategy.

Deliberately a SEPARATE file from config.py, not a CONDOR_* section bolted
onto it: this strategy has a different risk model (defined-risk credit
spread vs. the momentum scanner's long-premium debit trades), a
different holding period (weekly, held to expiry vs. intraday), and is
meant to run as its own process (main_condor.py) alongside, not inside,
main_live.py. Keeping config separate makes that separation real instead
of aspirational.
"""

# --- Strike selection ---
# Sell the CE and PE (independently) whose current premium falls in this
# band. Selection prefers the highest-OI (most liquid) match within the
# band over the one closest to spot -- liquidity matters more than exact
# moneyness for a position you intend to hold a week.
SHORT_PREMIUM_MIN = 23.0
SHORT_PREMIUM_MAX = 30.0

# How far OTM (in strike points) the protective long legs sit beyond each
# short strike. This IS your max-loss dial: wider = cheaper hedge but
# bigger max loss if breached; narrower = more expensive hedge (eats into
# credit) but caps risk tighter. 300 points is a starting assumption, not
# a researched optimum -- tune this once you've seen a few cycles of
# real premium data for what's actually available at various distances.
HEDGE_DISTANCE_POINTS = 300

# --- Position sizing / capital ---
LOTS_PER_LEG = 1
NIFTY_LOT_SIZE = 65   # keep in sync with config.py -- duplicated here so this file has no import-order dependency on it

# --- Risk gates (see condor_risk_checker.py) ---
MAX_CONCURRENT_POSITIONS = 1        # don't stack a new condor on top of one still open
MIN_NET_CREDIT = 10.0               # reject if the 4-leg net credit is too thin to be worth the risk/margin tied up
MAX_CAPITAL_AT_RISK = 50000.0       # reject if this cycle's max possible loss (see plan.max_loss_inr) exceeds this

# --- Expiry selection (see main_condor.choose_expiry_to_open) ---
# CHANGED 2026-07-29: this used to only open in a narrow 1-3 day window
# right after the PREVIOUS expiry passed, and a bug meant that window
# could never actually trigger (see the 2026-07-29 README entry) -- the
# condor never opened at all. Now it can open ANY day the position is
# flat, against whichever expiry has at least this many days left --
# skipping past the nearest one if it's too close (e.g. running the
# script ON expiry day itself correctly rolls straight into next week's
# expiry rather than selling ~0 DTE premium). A low value on purpose:
# 1 means "don't open with less than a day of theta left," not "only
# open fresh weekly cycles." Selling with only 1-2 DTE remaining is a
# different risk/reward shape than selling a fresh 6-7 DTE cycle (less
# premium available for the same hedge width, faster gamma ramp into
# expiry) -- know that if you see it open mid-week.
MIN_DAYS_TO_EXPIRY_TO_OPEN = 1

# --- Approval (see main_condor.py) ---
# When True, a candidate that clears the risk check is opened
# immediately -- still written through trade_staging.py first (so it
# stays visible in the same audit trail/dashboard as a manually-approved
# one), but auto-approved and auto-executed rather than waiting for
# approve_orders.py. Nothing here ever calls a broker API regardless of
# this setting -- "opening a position" only ever means writing this
# strategy's own tracked state, same as it always has. Set to False to
# go back to manual review if the strike-selection logic ever needs
# another look before trusting it unattended.
AUTO_APPROVE_NEW_POSITIONS = True

# --- Position management ---
# If spot gets within this many points of EITHER short strike, that's a
# "breach warning" -- condor_tracker.py stages an early-close candidate
# via trade_staging.py for human review rather than closing
# automatically. This is a credit-spread position with real capital
# behind it; an automatic close on a brief wick could realize a loss on
# a move that reverses by end of day, but ignoring a genuine breach
# until expiry can turn a small loss into the full max loss. A human
# call in the moment is the right tradeoff here, not either extreme.
BREACH_WARNING_BUFFER_POINTS = 50

# --- Timing ---
# main_condor.py polls this often during market hours -- much coarser
# than the momentum scanner's 30s, since this is a weekly swing
# strategy, not an intraday one. Frequent enough to catch a same-day
# breach, not so frequent it's pointless API load.
POLL_INTERVAL_SECONDS = 300  # 5 minutes

STATE_PATH = "state/condor_position.json"
JOURNAL_PATH = "logs/condor_journal.jsonl"
