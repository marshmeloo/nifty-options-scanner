"""
Pre-market brief: run this once before 9:15 IST to get a written plan for
the day instead of walking into the open cold.

Pulls together:
  1. Previous session recap (OHLC, where it closed relative to its range)
  2. Structural levels projected from recent daily candles (support/
     resistance via price_action.py, same logic the live scanner uses)
  3. Overnight global cues (US close, crude, dollar, India VIX) -- see
     global_cues.py for the honest caveat on why this isn't GIFT Nifty
  4. Previous session's FII/DII net flow
  5. Whether today is an expiry day, and any known scheduled event
     (RBI/Budget/Fed -- see config.KNOWN_EVENT_DATES, which you maintain)
  6. A synthesized bias -- explicitly a starting lean, not a signal; the
     live scanner's own OI/price reads during the session are what
     actually drive trade decisions

This is a planning aid, not a trade signal generator. It doesn't open,
approve, or stage any trade -- see trade_staging.py for that gate,
which this module doesn't touch.

Run:
    python3 premarket.py
Writes logs/premarket_brief_YYYYMMDD.md and prints the same to console.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import config as cfg
import global_cues
import nse_source
import news_source
import price_action
from resilient_source import get_nifty_intraday_candles, get_nearest_expiry, get_nifty_snapshot

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

log = logging.getLogger("nifty_scanner")


def _previous_trading_day_window(lookback_days: int):
    """
    Rough from/to window covering the last `lookback_days` calendar days,
    wide enough to contain that many trading days after weekends are
    excluded. Does NOT account for NSE holidays specifically -- a holiday
    just means slightly fewer daily candles come back, which is harmless
    for level projection.
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(days=int(lookback_days * 1.6) + 3)
    return from_date.strftime("%Y-%m-%d 09:15:00"), to_date.strftime("%Y-%m-%d %H:%M:%S")


def _aggregate_to_daily(candles: list) -> list:
    """Roll 60-min candles up into one daily OHLCV candle per calendar date."""
    from models import Candle

    by_date = {}
    for c in candles:
        d = c.timestamp.date()
        by_date.setdefault(d, []).append(c)

    daily = []
    for d, day_candles in sorted(by_date.items()):
        day_candles.sort(key=lambda c: c.timestamp)
        daily.append(
            Candle(
                timestamp=datetime.combine(d, datetime.min.time()),
                open=day_candles[0].open,
                high=max(c.high for c in day_candles),
                low=min(c.low for c in day_candles),
                close=day_candles[-1].close,
                volume=sum(c.volume for c in day_candles),
            )
        )
    return daily


def get_previous_session_recap(daily_candles: list) -> dict:
    if not daily_candles:
        return {}
    prev = daily_candles[-1]
    day_range = prev.high - prev.low
    close_position_pct = round((prev.close - prev.low) / day_range * 100, 1) if day_range else 50.0
    return {
        "date": prev.timestamp.strftime("%Y-%m-%d"),
        "open": prev.open,
        "high": prev.high,
        "low": prev.low,
        "close": prev.close,
        "close_position_pct": close_position_pct,  # 0 = closed at low, 100 = closed at high
    }


def get_expiry_context() -> dict:
    """Days to nearest expiry, computed from the actual expiry date -- not a hardcoded weekday."""
    try:
        expiry_str = get_nearest_expiry()
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        days_to_expiry = (expiry_date - datetime.now().date()).days
        return {
            "expiry": expiry_str,
            "days_to_expiry": days_to_expiry,
            "is_expiry_day": days_to_expiry == 0,
        }
    except Exception as e:
        return {"error": str(e)}


def get_today_event() -> str:
    return cfg.KNOWN_EVENT_DATES.get(datetime.now().strftime("%Y-%m-%d"))


def get_previous_chain_context() -> dict:
    """
    Best-effort PCR / max pain from the last available chain snapshot.
    Chain endpoints can be stale or briefly unavailable before market
    open; this degrades to an empty dict rather than failing the brief.
    """
    try:
        snap = get_nifty_snapshot()
        oi = snap.oi_analysis
        return {
            "source": snap.source,
            "pcr": snap.pcr,
            "max_pain_strike": oi.max_pain_strike if oi else None,
            "max_pain_distance_pct": oi.max_pain_distance_pct if oi else None,
            "net_delta_oi_bias": oi.net_delta_oi_bias if oi else None,
        }
    except Exception as e:
        return {"error": str(e)}


def synthesize_bias(recap: dict, context, cues_bias: str, chain_ctx: dict) -> str:
    """
    Combine (a) where price closed within its previous-day range, (b) the
    price_action trend read on recent daily candles, (c) the overnight
    global cues lean, and (d) chain net-delta-OI bias (if available) into
    one plain-language starting lean. Explicitly a lean, not a call --
    ties or missing inputs fall back to "neutral / wait for confirmation".
    """
    votes = []

    if recap.get("close_position_pct") is not None:
        if recap["close_position_pct"] >= 70:
            votes.append("bullish")
        elif recap["close_position_pct"] <= 30:
            votes.append("bearish")

    if context and context.trend == "uptrend":
        votes.append("bullish")
    elif context and context.trend == "downtrend":
        votes.append("bearish")

    if cues_bias == "positive":
        votes.append("bullish")
    elif cues_bias == "negative":
        votes.append("bearish")

    if chain_ctx.get("net_delta_oi_bias") in ("bullish", "bearish"):
        votes.append(chain_ctx["net_delta_oi_bias"])

    if not votes:
        return "neutral / insufficient data"

    bulls = votes.count("bullish")
    bears = votes.count("bearish")
    if bulls > bears:
        return f"leaning bullish ({bulls}/{len(votes)} signals)"
    if bears > bulls:
        return f"leaning bearish ({bears}/{len(votes)} signals)"
    return "mixed / neutral"


def build_brief() -> dict:
    from_date, to_date = _previous_trading_day_window(cfg.PREMARKET_LOOKBACK_DAYS)
    raw_candles = get_nifty_intraday_candles(interval="60", from_date=from_date, to_date=to_date)
    daily_candles = _aggregate_to_daily(raw_candles)

    recap = get_previous_session_recap(daily_candles)
    levels = price_action.detect_support_resistance(daily_candles) if daily_candles else []
    context = price_action.build_context(daily_candles) if daily_candles else None

    cues = global_cues.get_global_cues()
    cues_bias = global_cues.summarize_bias(cues)

    try:
        fii_dii = nse_source.get_fii_dii_activity()
    except Exception as e:
        fii_dii = {"error": str(e)}

    expiry_ctx = get_expiry_context()
    event_today = get_today_event()
    chain_ctx = get_previous_chain_context()
    news_flags = news_source.get_news_flags()

    bias = synthesize_bias(recap, context, cues_bias, chain_ctx)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "previous_session": recap,
        "levels": [{"kind": lvl.kind, "low": lvl.low, "high": lvl.high} for lvl in levels],
        "trend_context": {
            "trend": context.trend if context else None,
            "trend_note": context.trend_note if context else None,
            "rsi": context.rsi if context else None,
            "rsi_state": context.rsi_state if context else None,
        },
        "global_cues": cues,
        "global_cues_bias": cues_bias,
        "fii_dii": fii_dii,
        "expiry": expiry_ctx,
        "event_today": event_today,
        "chain_context": chain_ctx,
        "news": news_flags,
        "bias": bias,
    }


def render_markdown(brief: dict) -> str:
    lines = [f"# NIFTY Pre-Market Brief -- {datetime.now().strftime('%A, %d %B %Y')}", ""]

    lines.append(f"**Overall lean: {brief['bias']}** (a starting point, not a trade signal)")
    lines.append("")

    if brief.get("event_today"):
        lines.append(f"**Today's flagged event: {brief['event_today']} -- expect elevated volatility/whipsaw.**")
        lines.append("")

    if brief.get("news", {}).get("risk", {}).get("level") == "elevated":
        cats = ", ".join(brief["news"]["risk"]["categories_hit"])
        lines.append(f"**News risk flagged as elevated ({cats}) -- see News / event risk section below.**")
        lines.append("")

    exp = brief["expiry"]
    if "error" not in exp:
        exp_note = f"Nearest expiry: {exp['expiry']} ({exp['days_to_expiry']} day(s) away)"
        if exp["is_expiry_day"]:
            exp_note += " -- **TODAY IS EXPIRY DAY**: expect sharper theta decay and whipsaws."
        lines.append(exp_note)
        lines.append("")

    recap = brief["previous_session"]
    if recap:
        lines.append("## Previous session")
        lines.append(
            f"- {recap['date']}: O {recap['open']}  H {recap['high']}  L {recap['low']}  C {recap['close']}"
        )
        lines.append(f"- Closed at {recap['close_position_pct']}% of the day's range (0=low, 100=high)")
        lines.append("")

    ctx = brief["trend_context"]
    if ctx.get("trend"):
        lines.append("## Trend context (recent daily candles)")
        lines.append(f"- Trend: {ctx['trend']} -- {ctx['trend_note']}")
        if ctx.get("rsi") is not None:
            lines.append(f"- RSI: {ctx['rsi']} ({ctx['rsi_state']})")
        lines.append("")

    if brief["levels"]:
        lines.append("## Projected levels (from recent structure)")
        for lvl in brief["levels"]:
            lines.append(f"- {lvl['kind']}: {lvl['low']} - {lvl['high']}")
        lines.append("")

    lines.append("## Overnight global cues")
    for cue in brief["global_cues"]:
        if "error" in cue:
            lines.append(f"- {cue['label']}: unavailable ({cue['error']})")
        else:
            lines.append(f"- {cue['label']}: {cue['close']} ({cue['change_pct']:+.2f}%)")
    lines.append(f"- US cues bias: {brief['global_cues_bias']}")
    lines.append("")

    fii_dii = brief["fii_dii"]
    lines.append("## FII / DII flow (previous session, provisional)")
    if "error" in fii_dii:
        lines.append(f"- Unavailable: {fii_dii['error']}")
    else:
        lines.append(f"- Date: {fii_dii.get('date', 'n/a')}")
        lines.append(f"- FII net: Rs {fii_dii.get('fii_net_crore')} cr")
        lines.append(f"- DII net: Rs {fii_dii.get('dii_net_crore')} cr")
    lines.append("")

    chain_ctx = brief["chain_context"]
    lines.append("## Option chain context (last available snapshot)")
    if "error" in chain_ctx:
        lines.append(f"- Unavailable pre-market: {chain_ctx['error']}")
    else:
        lines.append(f"- Source: {chain_ctx['source']}")
        lines.append(f"- PCR: {chain_ctx['pcr']}")
        lines.append(
            f"- Max pain: {chain_ctx['max_pain_strike']} ({chain_ctx['max_pain_distance_pct']:+.2f}% from spot)"
        )
        lines.append(f"- Net delta OI bias: {chain_ctx['net_delta_oi_bias']}")
    lines.append("")

    news = brief.get("news", {})
    news_risk = news.get("risk", {})
    lines.append("## News / event risk")
    if news_risk.get("level") == "unknown":
        lines.append(f"- Unavailable: {news_risk.get('error', 'no feeds reachable')}")
    elif news_risk.get("headline_count"):
        lines.append(
            f"- Risk level: **{news_risk['level']}** "
            f"(categories: {', '.join(news_risk['categories_hit'])}; {news_risk['headline_count']} matching headline(s))"
        )
        for h in news.get("headlines", [])[:5]:
            lines.append(f"  - [{h['source']}] {h['title']} ({', '.join(h['categories'])})")
    else:
        lines.append("- No event-risk headlines matched today's keyword categories.")
    lines.append("")

    lines.append("---")
    lines.append(f"_Generated {brief['generated_at']}. Planning aid only -- not a trade recommendation._")

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    brief = build_brief()
    markdown = render_markdown(brief)

    out_path = LOG_DIR / f"premarket_brief_{datetime.now().strftime('%Y%m%d')}.md"
    out_path.write_text(markdown)

    print(markdown)
    print(f"\n[saved to {out_path}]")


if __name__ == "__main__":
    main()
