"""
Risk gate for opening a new iron condor. Deliberately simple -- three
checks, all against config_condor.py -- since the real risk management
for this strategy is the DEFINED max loss baked into the plan itself
(via the hedge legs) plus the breach-warning staging in
condor_tracker.py, not a complex scoring system.
"""

import config_condor as ccfg
from models import RiskVerdict


def check(plan, currently_open_positions: int = 0) -> RiskVerdict:
    if plan is None:
        return RiskVerdict(
            decision="REJECTED",
            reasons=["No complete 4-leg condor available this cycle (a leg was missing from the chain)."],
            checks={},
        )

    checks = {}
    reasons = []

    concurrency_ok = currently_open_positions < ccfg.MAX_CONCURRENT_POSITIONS
    checks["concurrency"] = {
        "pass": concurrency_ok,
        "detail": f"{currently_open_positions} position(s) already open (max {ccfg.MAX_CONCURRENT_POSITIONS})",
    }
    if not concurrency_ok:
        reasons.append("Max concurrent condor positions already open.")

    credit_ok = plan.net_credit_inr >= (ccfg.MIN_NET_CREDIT * ccfg.NIFTY_LOT_SIZE * ccfg.LOTS_PER_LEG)
    checks["min_credit"] = {
        "pass": credit_ok,
        "detail": f"net credit Rs {plan.net_credit_inr:,.0f} vs minimum Rs "
        f"{ccfg.MIN_NET_CREDIT * ccfg.NIFTY_LOT_SIZE * ccfg.LOTS_PER_LEG:,.0f}",
    }
    if not credit_ok:
        reasons.append("Net credit too thin relative to capital/margin tied up.")

    capital_ok = plan.max_loss_inr <= ccfg.MAX_CAPITAL_AT_RISK
    checks["max_capital_at_risk"] = {
        "pass": capital_ok,
        "detail": f"max loss Rs {plan.max_loss_inr:,.0f} vs cap Rs {ccfg.MAX_CAPITAL_AT_RISK:,.0f}",
    }
    if not capital_ok:
        reasons.append("Max possible loss on this structure exceeds your configured capital-at-risk cap.")

    decision = "APPROVED" if (concurrency_ok and credit_ok and capital_ok) else "REJECTED"
    return RiskVerdict(decision=decision, reasons=reasons, checks=checks)
