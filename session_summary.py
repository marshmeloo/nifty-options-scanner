"""
End-of-day digest generator. Run this once the market's closed, and
hand the OUTPUT file to a fresh Claude conversation (or Claude Code) --
not the raw logs directly. The raw logs are either too noisy
(logs/nifty_scan_*.log repeats OI/bias every single cycle) or too
voluminous (logs/decision_log.jsonl has one record per cycle, hundreds
per day) to review efficiently. This pulls the high-signal parts from
all of them into one small markdown file:

  - Every trade closed today: outcome, P&L, capture efficiency, tags
  - "Near-miss" candidates: setups that almost cleared the conviction
    bar but got rejected after the learned adjustment -- worth a human
    look, since these are exactly the ones a bar/penalty miscalibration
    would show up in
  - Operational health: gaps in the scan log longer than expected during
    market hours (freeze/crash evidence) and any WARNING/error lines
  - Day-level context: opening gap, news risk, Bank Nifty divergence
    from the last cycle of the day

Run:
    python3 session_summary.py
Writes logs/session_summary_YYYYMMDD.md and prints it too.
"""

import json
import re
from pathlib import Path
from datetime import datetime, date, time as dtime

import config

LOGS_DIR = Path(__file__).parent / "logs"
TRADE_JOURNAL_PATH = LOGS_DIR / "trade_journal.jsonl"
DECISION_LOG_PATH = LOGS_DIR / "decision_log.jsonl"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
GAP_WARNING_MINUTES = 3  # a gap this long between scan-log lines during market hours is worth flagging


def _todays_main_log_path() -> Path:
    return LOGS_DIR / f"nifty_scan_{date.today().strftime('%Y%m%d')}.log"


def _read_todays_journal_entries() -> list:
    if not TRADE_JOURNAL_PATH.exists():
        return []
    today_str = date.today().isoformat()
    entries = []
    with open(TRADE_JOURNAL_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (entry.get("closed_at") or "").startswith(today_str):
                entries.append(entry)
    return entries


def _read_todays_decision_cycles() -> list:
    if not DECISION_LOG_PATH.exists():
        return []
    today_str = date.today().isoformat()
    cycles = []
    with open(DECISION_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (record.get("timestamp") or "").startswith(today_str):
                cycles.append(record)
    return cycles


def _find_near_misses(cycles: list, within_points: float = 1.0) -> list:
    """
    Candidates rejected after the learned adjustment, but only just --
    within `within_points` of the conviction bar. These are exactly the
    ones worth a human look: genuinely borderline, not obviously wrong.
    """
    near_misses = []
    seen = set()
    for cycle in cycles:
        for c in cycle.get("candidates", []):
            if c.get("final_decision") != "REJECTED_BELOW_BAR_AFTER_ADJUSTMENT":
                continue
            adjusted = c.get("adjusted_score")
            if adjusted is None:
                continue
            bar = getattr(config, "MIN_CONVICTION_SCORE_TO_TRACK", 5.0)
            if bar - adjusted <= within_points:
                key = (c["strike"], c["option_type"], cycle.get("timestamp"))
                if key not in seen:
                    seen.add(key)
                    near_misses.append({**c, "timestamp": cycle.get("timestamp")})
    return near_misses


def _find_operational_issues(log_path: Path) -> dict:
    """
    Scans the raw scan log (if it exists) for two things: gaps between
    consecutive timestamped lines longer than GAP_WARNING_MINUTES during
    market hours (the exact evidence that caught the 2026-07-24 freeze
    incident), and any WARNING/error lines.
    """
    if not log_path.exists():
        return {"gaps": [], "warnings": [], "log_found": False}

    timestamp_pattern = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
    timestamps = []
    warnings = []

    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            m = timestamp_pattern.match(line)
            if m:
                timestamps.append(m.group(1))
            if "WARNING" in line or "Error this cycle" in line or "RESTARTING" in line or "[ESTIMATED]" in line:
                warnings.append(line.strip())

    gaps = []
    for i in range(1, len(timestamps)):
        t1 = datetime.strptime(timestamps[i - 1], "%H:%M:%S")
        t2 = datetime.strptime(timestamps[i], "%H:%M:%S")
        if t1.time() < MARKET_OPEN or t2.time() > MARKET_CLOSE:
            continue
        gap_minutes = (t2 - t1).total_seconds() / 60
        if gap_minutes >= GAP_WARNING_MINUTES:
            gaps.append({"from": timestamps[i - 1], "to": timestamps[i], "minutes": round(gap_minutes, 1)})

    return {"gaps": gaps, "warnings": warnings[:15], "log_found": True}


def build_summary() -> dict:
    journal_entries = _read_todays_journal_entries()
    decision_cycles = _read_todays_decision_cycles()
    near_misses = _find_near_misses(decision_cycles)
    operational = _find_operational_issues(_todays_main_log_path())

    wins = [e for e in journal_entries if e.get("outcome") == "WIN"]
    losses = [e for e in journal_entries if e.get("outcome") == "LOSS"]
    total_pnl_inr = round(sum(e.get("pnl_inr", 0) or 0 for e in journal_entries), 2)
    win_rate = round(len(wins) / len(journal_entries) * 100, 1) if journal_entries else None

    captures = [e["capture_efficiency_pct"] for e in journal_entries if e.get("capture_efficiency_pct") is not None]
    avg_capture = round(sum(captures) / len(captures), 1) if captures else None

    day_context = decision_cycles[-1] if decision_cycles else None

    return {
        "date": date.today().isoformat(),
        "journal_entries": journal_entries,
        "trade_count": len(journal_entries),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": win_rate,
        "total_pnl_inr": total_pnl_inr,
        "avg_capture_efficiency_pct": avg_capture,
        "near_misses": near_misses,
        "operational": operational,
        "day_context": day_context,
    }


def render_markdown(summary: dict) -> str:
    lines = [f"# Session Summary -- {summary['date']}", ""]

    lines.append("## Trades")
    if summary["trade_count"] == 0:
        lines.append("- No trades closed today.")
    else:
        lines.append(
            f"- {summary['trade_count']} closed: {summary['win_count']}W / {summary['loss_count']}L "
            f"({summary['win_rate_pct']}% win rate)"
        )
        lines.append(f"- Total P&L: Rs {summary['total_pnl_inr']:+,.0f}")
        if summary["avg_capture_efficiency_pct"] is not None:
            lines.append(f"- Avg capture of peak favorable move: {summary['avg_capture_efficiency_pct']}%")
        lines.append("")
        for e in summary["journal_entries"]:
            lines.append(
                f"  - {e.get('strike')} {e.get('option_type')}  entry {e.get('entry')} -> exit {e.get('exit_ltp')}  "
                f"{e.get('outcome')}  {e.get('pnl_pct', 0):+.1f}% (Rs {e.get('pnl_inr', 0):+,.0f})"
            )
            if e.get("lesson"):
                lines.append(f"    lesson: {e['lesson']}")
    lines.append("")

    lines.append("## Near-miss candidates (rejected, but close to the bar)")
    if not summary["near_misses"]:
        lines.append("- None today.")
    else:
        for nm in summary["near_misses"]:
            lines.append(
                f"- {nm['strike']} {nm['option_type']} @ {nm['timestamp']}: "
                f"raw {nm['raw_score']} -> adjusted {nm['adjusted_score']}  ({nm['detail']})"
            )
    lines.append("")

    lines.append("## Operational health")
    op = summary["operational"]
    if not op["log_found"]:
        lines.append("- No scan log found for today.")
    else:
        if op["gaps"]:
            lines.append(f"- **{len(op['gaps'])} gap(s) found in the scan log during market hours:**")
            for g in op["gaps"]:
                lines.append(f"  - {g['from']} -> {g['to']}  ({g['minutes']} min gap)")
        else:
            lines.append("- No unusual gaps in the scan log during market hours.")
        if op["warnings"]:
            lines.append(f"- {len(op['warnings'])} warning/error line(s) logged today (showing up to 15):")
            for w in op["warnings"]:
                lines.append(f"  - {w}")
    lines.append("")

    ctx = summary["day_context"]
    if ctx:
        lines.append("## End-of-day context (last cycle)")
        lines.append(f"- Spot: {ctx.get('spot')}  PCR: {ctx.get('pcr')}  Source: {ctx.get('source')}")
        bias = ctx.get("market_bias") or {}
        lines.append(f"- Market bias: {bias.get('label')} (score {bias.get('score')})")
        gap = ctx.get("opening_gap") or {}
        if gap.get("nifty"):
            g = gap["nifty"]
            lines.append(f"- Opening gap: {g.get('gap_points'):+.1f} pts ({g.get('gap_pct'):+.2f}%)")
        news = ctx.get("news_risk") or {}
        if news.get("level") == "elevated":
            lines.append(f"- News risk was elevated: {', '.join(news.get('categories_hit', []))}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Feed this file (not the raw logs) to a fresh Claude conversation or Claude Code session for review. "
        "Suggested prompt: \"Here's today's session digest for the NIFTY options scanner project (see README for "
        "full context). Review for: 1) anything odd about today's trades, 2) whether any near-miss candidates "
        "should have opened, 3) any operational issues. Flag anything concerning.\"_"
    )

    return "\n".join(lines)


def main():
    summary = build_summary()
    markdown = render_markdown(summary)

    out_path = LOGS_DIR / f"session_summary_{date.today().strftime('%Y%m%d')}.md"
    out_path.write_text(markdown)

    print(markdown)
    print(f"\n[saved to {out_path}]")


if __name__ == "__main__":
    main()
