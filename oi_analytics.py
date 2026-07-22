"""
Chain-wide OI analytics: Max Pain, call/put "walls", and net delta OI.

These read the WHOLE chain at once, as opposed to scanner.py's per-strike
buildup classification, and answer a different question: not "is this one
contract seeing bullish or bearish flow" but "looking at the whole chain,
where does open interest suggest the big players are positioned, and
where is price likely to gravitate or struggle."

Inspired by the OI Analysis view in retail options-analytics dashboards
(delta OI charts, max pain, PCR trend) — reimplemented here from scratch
against Dhan/NSE's raw option-chain shape, not copied from any particular
tool's source.
"""

from collections import defaultdict

from models import OIAnalysis
import config as cfg


def compute_max_pain(chain: list, lot_size: int = None) -> float:
    """
    Max pain: the strike at which option WRITERS collectively lose the
    least (equivalently, option buyers collectively gain the least) if
    the underlying settled there at expiry.

    For each candidate settlement price S (every strike present in the
    chain), total payout to option holders is:
        sum over call strikes K <= S of (S - K) * OI_call(K)   [ITM calls]
      + sum over put  strikes K >= S of (K - S) * OI_put(K)    [ITM puts]
    The strike minimizing this sum is max pain. Writers as a group have
    the least incentive to let price move away from it, which is why it's
    often watched as a expiry-week gravitational level -- though it is a
    reading of aggregate OI, not a guarantee.
    """
    strikes = sorted({q.strike for q in chain})
    if not strikes:
        return 0.0

    ce_oi = defaultdict(int)
    pe_oi = defaultdict(int)
    for q in chain:
        if q.option_type == "CE":
            ce_oi[q.strike] += q.oi
        elif q.option_type == "PE":
            pe_oi[q.strike] += q.oi

    best_strike = strikes[0]
    best_payout = None
    for settle in strikes:
        call_payout = sum((settle - k) * ce_oi[k] for k in strikes if k < settle)
        put_payout = sum((k - settle) * pe_oi[k] for k in strikes if k > settle)
        total = call_payout + put_payout
        if best_payout is None or total < best_payout:
            best_payout = total
            best_strike = settle

    return best_strike


def compute_oi_walls(chain: list, top_n: int = 5):
    """
    Call wall / put wall: the single strike with the largest CE / PE OI.
    Heavy CE OI above spot acts like a ceiling (writers defending it is
    what "resistance" means in OI terms); heavy PE OI below spot acts
    like a floor. Also returns the top-N strikes by combined OI as a
    coarse "OI concentration" table for a delta-OI-style chart.
    """
    ce_oi = defaultdict(int)
    pe_oi = defaultdict(int)
    for q in chain:
        if q.option_type == "CE":
            ce_oi[q.strike] += q.oi
        elif q.option_type == "PE":
            pe_oi[q.strike] += q.oi

    call_wall_strike, call_wall_oi = (max(ce_oi.items(), key=lambda kv: kv[1])
                                       if ce_oi else (0.0, 0))
    put_wall_strike, put_wall_oi = (max(pe_oi.items(), key=lambda kv: kv[1])
                                     if pe_oi else (0.0, 0))

    all_strikes = sorted(set(ce_oi) | set(pe_oi))
    combined = [(k, ce_oi.get(k, 0), pe_oi.get(k, 0)) for k in all_strikes]
    combined.sort(key=lambda row: row[1] + row[2], reverse=True)

    return call_wall_strike, call_wall_oi, put_wall_strike, put_wall_oi, combined[:top_n]


def compute_net_delta_oi(chain: list) -> int:
    """
    Net delta OI for the session so far: today's OI ADDED on the call
    side minus today's OI added on the put side, summed across the whole
    chain (uses each quote's oi_change_pct against its own current OI to
    recover the absolute OI added, since the chain only carries the %).
    Positive = more fresh call OI than put OI being added -> writers
    leaning bearish on the underlying (or buyers leaning bullish depending
    on which side is initiating -- read this alongside buildup_type per
    strike, not in isolation) -- treat the sign as "call-side vs put-side
    OI momentum," not a standalone directional signal.
    """
    net = 0
    for q in chain:
        if not q.oi_change_pct:
            continue
        # oi_change_pct = (oi - prev_oi) / prev_oi * 100  =>  oi_added = oi - prev_oi
        prev_oi = q.oi / (1 + q.oi_change_pct / 100) if (1 + q.oi_change_pct / 100) else q.oi
        oi_added = q.oi - prev_oi
        net += oi_added if q.option_type == "CE" else -oi_added
    return round(net)


def analyze(chain: list, spot: float) -> OIAnalysis:
    """Run all chain-wide OI analytics and package them into one OIAnalysis."""
    if not chain:
        return OIAnalysis(
            max_pain_strike=spot, max_pain_distance_pct=0.0,
            call_wall_strike=spot, put_wall_strike=spot,
            call_wall_oi=0, put_wall_oi=0,
            net_delta_oi=0, net_delta_oi_bias="neutral",
            top_oi_concentration=[],
        )

    max_pain = compute_max_pain(chain)
    max_pain_dist_pct = round((spot - max_pain) / spot * 100, 2) if spot else 0.0

    call_wall, call_wall_oi, put_wall, put_wall_oi, top_conc = compute_oi_walls(chain)

    net_delta = compute_net_delta_oi(chain)
    threshold = getattr(cfg, "NET_DELTA_OI_NEUTRAL_BAND", 0)
    if net_delta > threshold:
        bias = "bearish"   # heavier fresh call OI than put OI -> writers positioning against upside
    elif net_delta < -threshold:
        bias = "bullish"   # heavier fresh put OI than call OI -> writers positioning against downside
    else:
        bias = "neutral"

    return OIAnalysis(
        max_pain_strike=max_pain,
        max_pain_distance_pct=max_pain_dist_pct,
        call_wall_strike=call_wall,
        put_wall_strike=put_wall,
        call_wall_oi=call_wall_oi,
        put_wall_oi=put_wall_oi,
        net_delta_oi=net_delta,
        net_delta_oi_bias=bias,
        top_oi_concentration=top_conc,
    )
