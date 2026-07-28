"""
Structured, per-cycle decision audit trail -- logs.jsonl.jsonl, i.e.
logs/decision_log.jsonl. NOT just the winning candidate's score: every
candidate considered this cycle, the full indicator/context state that
produced it, and the PRECISE reason each one did or didn't result in a
trade (raw score cleared the bar but the learned adjustment dropped it
below; risk check rejected it; a position was already open on that
strike; the daily cap was reached; or a higher-ranked candidate was
picked instead).

Why this exists: trade_journal.jsonl only records trades that actually
opened. That's the wrong dataset to answer "is our conviction bar or
learned-adjustment penalty well-calibrated" -- you need to see the
candidates that DIDN'T open too, and why, or you're reasoning from a
survivorship-biased sample.
"""

import json
from pathlib import Path
from datetime import datetime

import config
import trade_tracker as tt

LOG_PATH = Path(__file__).parent / "logs" / "decision_log.jsonl"
LOG_PATH.parent.mkdir(exist_ok=True)


def _candidate_record(setup, plan, verdict, state: dict, opened_trade: dict) -> dict:
    adjusted_score, learn_notes = tt.apply_learned_adjustment(setup.score, setup.reasons)
    conviction_bar, expiry_blocked = tt.expiry_day_rules(setup.expiry, datetime.now())
    open_keys = {(t["strike"], t["option_type"]) for t in state["trades"]}
    is_this_the_opened_one = (
        opened_trade is not None
        and opened_trade["strike"] == setup.strike
        and opened_trade["option_type"] == setup.option_type
    )

    if is_this_the_opened_one:
        final_decision, detail = "OPENED", "This candidate was opened this cycle."
    elif (setup.strike, setup.option_type) in open_keys:
        final_decision, detail = "ALREADY_OPEN", "A trade on this strike/type is already being tracked."
    elif plan is not None and tt.is_repeat_of_stopped_plan(state, setup.strike, setup.option_type, plan.entry):
        final_decision = "REJECTED_REPEAT_OF_STOPPED_PLAN"
        detail = (
            f"A trade on this strike/type already stopped out today at an entry within "
            f"{config.REENTRY_PRICE_TOLERANCE_PCT}% of {plan.entry} -- same entry means the same "
            f"stop and target, i.e. re-running a plan that already failed."
        )
    elif verdict.decision != "APPROVED":
        final_decision = "REJECTED_RISK"
        detail = "; ".join(verdict.reasons) if verdict.reasons else "Risk check rejected this plan."
    elif expiry_blocked:
        final_decision = "REJECTED_EXPIRY_DAY_CUTOFF"
        detail = f"Blocked: {expiry_blocked}"
    elif adjusted_score < conviction_bar:
        final_decision = "REJECTED_BELOW_BAR_AFTER_ADJUSTMENT"
        detail = (
            f"Raw score {setup.score} adjusted to {adjusted_score} (bar is {conviction_bar}"
            + (" -- raised because this contract expires today" if conviction_bar != config.MIN_CONVICTION_SCORE_TO_TRACK else "")
            + ") -- "
            + ("; ".join(learn_notes) if learn_notes else "no learned adjustment applied, still below bar")
        )
    elif state["opened_today"] >= config.MAX_NEW_TRADES_PER_DAY:
        final_decision = "DAILY_CAP_REACHED"
        detail = f"{state['opened_today']}/{config.MAX_NEW_TRADES_PER_DAY} trades already opened today."
    else:
        final_decision, detail = "NOT_SELECTED", "Cleared the bar, but a higher-ranked candidate was chosen instead this cycle."

    return {
        "strike": setup.strike,
        "option_type": setup.option_type,
        "expiry": setup.expiry,
        "raw_score": setup.score,
        "reasons": list(setup.reasons),
        "adjusted_score": adjusted_score,
        "conviction_bar": conviction_bar,
        "learned_adjustment_notes": learn_notes,
        "risk_decision": verdict.decision,
        "risk_reasons": verdict.reasons,
        "plan": {
            "entry": plan.entry, "target": plan.target, "stop": plan.stop,
            "lots": plan.lots, "stop_basis": getattr(plan, "stop_basis", ""),
        } if plan else None,
        "final_decision": final_decision,
        "detail": detail,
    }


def log_cycle(snapshot, context, bias_label, bias_score, bias_reasons, banknifty_ctx, banknifty_divergence,
              news_risk, opening_gap, results, state, opened_trade, volume_profile=None, anchored_vwap=None,
              top_n: int = 5):
    """
    Logs one full cycle's decision context. `results` is main_live.py's
    already-sorted (setup, plan, verdict) list -- only the top `top_n`
    are logged in full; a long tail of near-zero-score setups isn't
    useful to audit and would just bloat the file.
    """
    oi = snapshot.oi_analysis
    bn_ctx_clean = {k: v for k, v in (banknifty_ctx or {}).items() if k != "error"}

    record = {
        "timestamp": snapshot.timestamp.isoformat(),
        "spot": snapshot.spot,
        "vwap": snapshot.vwap,
        "pcr": snapshot.pcr,
        "source": snapshot.source,
        "oi_analysis": {
            "max_pain_strike": oi.max_pain_strike,
            "max_pain_distance_pct": oi.max_pain_distance_pct,
            "call_wall_strike": oi.call_wall_strike,
            "call_wall_oi": oi.call_wall_oi,
            "put_wall_strike": oi.put_wall_strike,
            "put_wall_oi": oi.put_wall_oi,
            "net_delta_oi": oi.net_delta_oi,
            "net_delta_oi_bias": oi.net_delta_oi_bias,
        } if oi else None,
        "market_context": {
            "trend": context.trend,
            "trend_note": context.trend_note,
            "rsi": context.rsi,
            "rsi_state": context.rsi_state,
            "roc_pct": context.roc_pct,
            "volume_ratio": context.volume_ratio,
        } if context else None,
        "market_bias": {"label": bias_label, "score": bias_score, "reasons": bias_reasons},
        "banknifty": {**bn_ctx_clean, "divergence_read": (banknifty_divergence or {}).get("read")},
        "news_risk": {
            "level": (news_risk or {}).get("level"),
            "categories_hit": (news_risk or {}).get("categories_hit", []),
        },
        "opening_gap": opening_gap,
        "volume_profile": volume_profile,
        "anchored_vwap": anchored_vwap,
        "candidates": [_candidate_record(s, p, v, state, opened_trade) for s, p, v in results[:top_n]],
        "trade_opened": (
            {"strike": opened_trade["strike"], "option_type": opened_trade["option_type"]}
            if opened_trade else None
        ),
    }

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
