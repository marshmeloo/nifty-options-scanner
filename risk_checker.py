"""
Risk checker: the last gate before a plan is surfaced to you.
This never places an order. It only produces a recommendation.
"""

import config
from models import RiskVerdict


def check(plan, current_open_exposure_pct: float = 0.0, current_daily_loss_pct: float = 0.0,
          news_risk_level: str = "normal") -> RiskVerdict:
    checks = {}
    reasons = []

    # 1. Position sizing
    lots_ok = plan.lots > 0
    checks["position_size"] = {
        "pass": lots_ok,
        "detail": f"{plan.lots} lots sized from risk budget",
    }
    if not lots_ok:
        reasons.append("Risk budget too small to size even 1 lot for this setup")

    # 2. Per-trade risk cap
    risk_ok = plan.risk_pct_of_capital <= config.MAX_RISK_PER_TRADE_PCT
    checks["per_trade_risk"] = {
        "pass": risk_ok,
        "detail": f"{plan.risk_pct_of_capital}% of capital vs {config.MAX_RISK_PER_TRADE_PCT}% cap",
    }
    if not risk_ok:
        reasons.append("Per-trade risk exceeds your configured cap")

    # 3. Total exposure (existing positions + this one)
    projected_exposure = current_open_exposure_pct + plan.risk_pct_of_capital
    exposure_ok = projected_exposure <= config.MAX_TOTAL_EXPOSURE_PCT
    checks["total_exposure"] = {
        "pass": exposure_ok,
        "detail": f"{projected_exposure:.2f}% projected vs {config.MAX_TOTAL_EXPOSURE_PCT}% cap",
    }
    if not exposure_ok:
        reasons.append("Adding this trade would breach your total exposure cap")

    # 4. Daily loss circuit breaker
    daily_loss_ok = current_daily_loss_pct < config.MAX_DAILY_LOSS_PCT
    checks["daily_loss_breaker"] = {
        "pass": daily_loss_ok,
        "detail": f"{current_daily_loss_pct:.2f}% lost today vs {config.MAX_DAILY_LOSS_PCT}% breaker",
    }
    if not daily_loss_ok:
        reasons.append("Daily loss circuit breaker already tripped, no new trades today")

    # 5. News / event risk (advisory unless config.NEWS_RISK_BLOCKS_NEW_TRADES)
    news_ok = not (news_risk_level == "elevated" and getattr(config, "NEWS_RISK_BLOCKS_NEW_TRADES", False))
    checks["news_risk"] = {
        "pass": news_ok,
        "detail": f"news risk level: {news_risk_level}"
        + ("" if news_ok else " -- new trades blocked by config.NEWS_RISK_BLOCKS_NEW_TRADES"),
    }
    if news_risk_level == "elevated":
        reasons.append(
            "Elevated news/event risk today -- consider smaller size or wider stops regardless of this verdict"
            if news_ok else "Elevated news/event risk today: new trades blocked by config"
        )

    # Decision logic
    if not daily_loss_ok or not exposure_ok or not news_ok:
        decision = "REJECTED"
    elif not lots_ok or not risk_ok:
        decision = "REJECTED"
    elif plan.setup.score < 1.5:
        decision = "WATCHLIST"
        reasons.append("Signal present but composite score is below conviction threshold")
    else:
        decision = "APPROVED"

    return RiskVerdict(decision=decision, reasons=reasons, checks=checks)
