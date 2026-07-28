"""
NSE's daily Participant-wise Open Interest report -- the actual "who is
big money and how are they positioned in index options specifically"
data, as distinct from:
  - oi_analytics.py: aggregate OI across ALL participants combined (a
    proxy for where positioning is concentrated, not who's behind it)
  - nse_source.get_fii_dii_activity(): FII/DII CASH market flow (equity
    buying/selling), not their derivatives/options positioning

NSE classifies every F&O position into one of four participant types:
  - Client: retail + HNI traders -- "the public," largest by count
  - Pro:    proprietary desks (brokers/firms trading their own capital)
  - FII:    foreign institutional investors
  - DII:    domestic institutions (mutual funds, insurers, banks) --
            regulation mostly restricts these to hedging, so DII
            derivatives OI is structurally small; largely uninformative
            for options positioning specifically

Common heuristic (real, but worth treating as a lean not a rule): FII
and Pro are considered the more informed/"smart money" side; Client is
often on the other side of that trade. Every contract needs a
counterparty, so these positions mechanically offset each other --
divergence between Client and FII/Pro is a real signal, but "which side
is right" isn't guaranteed by this data alone.

Published once daily, after market close (typically 5-6pm IST) --
NOT something to poll during the live session; this is pre-market
context for premarket.py, using the most recently published (i.e.
previous trading day's) report.
"""

import csv
from datetime import datetime, timedelta

import requests

import config as cfg

CSV_URL_TEMPLATE = "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _fetch_csv_for_date(d) -> str:
    url = CSV_URL_TEMPLATE.format(ddmmyyyy=d.strftime("%d%m%Y"))
    resp = requests.get(url, headers=_HEADERS, timeout=getattr(cfg, "NSE_REQUEST_TIMEOUT", 10))
    resp.raise_for_status()
    return resp.text


def _parse_csv(text: str) -> dict:
    """
    Parses the report into {"Client": {...}, "DII": {...}, "FII": {...},
    "Pro": {...}, "TOTAL": {...}}, each a dict of column name -> int.
    Row 1 is a title line (not real CSV headers), row 2 is the real
    header -- skip the title, read by column NAME (not position), in
    case NSE ever reorders columns.
    """
    lines = text.strip().splitlines()
    reader = csv.reader(lines[1:])  # skip the title line
    header = next(reader)
    header = [h.strip() for h in header]

    participants = {}
    for row in reader:
        if not row or not row[0].strip():
            continue
        row_dict = {}
        for col_name, value in zip(header[1:], row[1:]):
            try:
                row_dict[col_name.strip()] = int(value.strip())
            except (ValueError, AttributeError):
                row_dict[col_name.strip()] = None
        participants[row[0].strip()] = row_dict
    return participants


def get_latest_participant_oi(max_lookback_days: int = 5) -> dict:
    """
    Tries today, then walks backward up to max_lookback_days looking for
    the most recently published report -- NSE publishes end-of-day, so
    "today's" file won't exist until evening, and weekends/holidays have
    no file at all. Returns {"as_of_date": "YYYY-MM-DD", "participants": {...}}
    or {"error": "..."} if nothing was found in the lookback window.
    """
    d = datetime.now().date()
    for _ in range(max_lookback_days + 1):
        try:
            text = _fetch_csv_for_date(d)
            participants = _parse_csv(text)
            if participants:
                return {"as_of_date": d.isoformat(), "participants": participants}
        except Exception:
            pass
        d -= timedelta(days=1)
    return {"error": f"No participant OI report found in the last {max_lookback_days} days"}


def compute_smart_money_bias(report: dict) -> dict:
    """
    Distills FII + Pro's INDEX OPTIONS positioning (not futures, not
    stock options) into a simple directional lean. DII is reported but
    not used in the lean itself -- regulatory restrictions make DII
    derivatives OI structurally small and largely uninformative here.

    net_call = Call Long - Call Short (positive = net long calls, a
    bullish-leaning position since a long call profits on upside)
    net_put  = Put Long - Put Short (positive = net long puts, a
    bearish-leaning position since a long put profits on downside)
    """
    if "error" in report:
        return {"lean": "unavailable", "detail": report["error"]}

    participants = report["participants"]

    def _net_positions(name):
        p = participants.get(name)
        if not p:
            return None, None
        call_net = (p.get("Option Index Call Long") or 0) - (p.get("Option Index Call Short") or 0)
        put_net = (p.get("Option Index Put Long") or 0) - (p.get("Option Index Put Short") or 0)
        return call_net, put_net

    fii_call_net, fii_put_net = _net_positions("FII")
    pro_call_net, pro_put_net = _net_positions("Pro")
    dii_call_net, dii_put_net = _net_positions("DII")

    if fii_call_net is None and pro_call_net is None:
        return {"lean": "unavailable", "detail": "FII/Pro rows not found in report"}

    combined_call = (fii_call_net or 0) + (pro_call_net or 0)
    combined_put = (fii_put_net or 0) + (pro_put_net or 0)
    directional_score = combined_call - combined_put

    threshold = getattr(cfg, "SMART_MONEY_NEUTRAL_BAND", 50000)
    if directional_score > threshold:
        lean = "bullish"
    elif directional_score < -threshold:
        lean = "bearish"
    else:
        lean = "neutral"

    return {
        "as_of_date": report.get("as_of_date"),
        "lean": lean,
        "fii_net_call": fii_call_net,
        "fii_net_put": fii_put_net,
        "pro_net_call": pro_call_net,
        "pro_net_put": pro_put_net,
        "dii_net_call": dii_call_net,
        "dii_net_put": dii_put_net,
        "detail": (
            f"FII+Pro combined: net calls {combined_call:+,}, net puts {combined_put:+,} "
            f"in index options -> {lean}"
        ),
    }


def get_smart_money_bias() -> dict:
    """One-call convenience: fetch the latest report and distill it."""
    report = get_latest_participant_oi()
    return compute_smart_money_bias(report)
