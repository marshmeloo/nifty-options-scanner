"""
Risk gate for opening a new directional credit spread. Same philosophy
as condor_risk_checker.py: a few simple checks against
config_directional_spread.py, since the real risk control is the DEFINED
max loss baked into the plan itself (the hedge leg) plus active
management (profit target / stop loss / breach staging) in
directional_spread_tracker.py, not a scoring system.
"""

import config_directional_spread as dcfg
from models import RiskVerdict


def check(plan, currently_open_positions: int = 0, opened_today: int = 0) -> RiskVerdict:
    if plan is None:
        return RiskVerdict(
            decision="REJECTED",
            reasons=["No complete directional spread available this cycle "
                    "(bias not strong enough, or a leg was missing from the chain)."],
            checks={},
        )

    checks = {}
    reasons = []

    concurrency_ok = currently_open_positions < dcfg.MAX_CONCURRENT_POSITIONS
    checks["concurrency"] = {
        "pass": concurrency_ok,
        "detail": f"{currently_open_positions} position(s) already open (max {dcfg.MAX_CONCURRENT_POSITIONS})",
    }
    if not concurrency_ok:
        reasons.append("Max concurrent directional spread positions already open.")

    daily_cap_ok = opened_today < dcfg.MAX_NEW_POSITIONS_PER_DAY
    checks["daily_cap"] = {
        "pass": daily_cap_ok,
        "detail": f"{opened_today} opened today (max {dcfg.MAX_NEW_POSITIONS_PER_DAY})",
    }
    if not daily_cap_ok:
        reasons.append("Max new positions for today already opened.")

    min_credit_inr = dcfg.MIN_NET_CREDIT * dcfg.NIFTY_LOT_SIZE * dcfg.LOTS_PER_LEG
    credit_ok = plan.net_credit_inr >= min_credit_inr
    checks["min_credit"] = {
        "pass": credit_ok,
        "detail": f"net credit Rs {plan.net_credit_inr:,.0f} vs minimum Rs {min_credit_inr:,.0f}",
    }
    if not credit_ok:
        reasons.append("Net credit too thin relative to capital/margin tied up.")

    capital_ok = plan.max_loss_inr <= dcfg.MAX_CAPITAL_AT_RISK
    checks["max_capital_at_risk"] = {
        "pass": capital_ok,
        "detail": f"max loss Rs {plan.max_loss_inr:,.0f} vs cap Rs {dcfg.MAX_CAPITAL_AT_RISK:,.0f}",
    }
    if not capital_ok:
        reasons.append("Max possible loss on this structure exceeds your configured capital-at-risk cap.")

    decision = "APPROVED" if (concurrency_ok and daily_cap_ok and credit_ok and capital_ok) else "REJECTED"
    return RiskVerdict(decision=decision, reasons=reasons, checks=checks)
