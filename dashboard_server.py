"""
Local live dashboard server. Read-only, stdlib-only (no new dependency,
no Flask) -- it just reads the same state/log files main_live.py,
condor_tracker.py, and trade_staging.py already write, and serves them
as JSON to a browser page that polls every few seconds. It never writes
to any of those files, never talks to Dhan/NSE itself, and never
touches the live trading process in any way -- it's a window onto what
main_live.py (or supervisor.py) is already doing, nothing more.

Run this in its OWN terminal, alongside main_live.py / supervisor.py:
  python3 dashboard_server.py
Then open http://127.0.0.1:8787 in a browser. Bound to 127.0.0.1 only --
not reachable from other machines on your network, let alone the
internet.
"""

import json
import http.server
import socketserver
from pathlib import Path
from datetime import datetime, date

import config
import config_condor as ccfg
import config_directional_spread as dcfg
import config_price_action as pacfg
import trade_tracker as tt
import condor_tracker as ct
import directional_spread_tracker as dst
import price_action_tracker as pat

HOST = "127.0.0.1"
PORT = 8787

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
DASHBOARD_HTML_PATH = BASE_DIR / "dashboard" / "live_dashboard.html"
TRADE_JOURNAL_PATH = LOGS_DIR / "trade_journal.jsonl"

DECISION_LOG_TAIL_LINES = 5   # how many recent cycles to show in the "recent activity" panel

# --- Bank Nifty: separate state/journal/log files, one per strategy,
# same naming convention each main_*_banknifty.py process itself uses
# (see e.g. main_live_banknifty.py's own path overrides). Added
# 2026-08-13 -- these processes were already running and producing real
# candidate/decision data with ZERO dashboard visibility: every
# strike/score/reason was only ever readable from raw log files.
BANKNIFTY_LOT_SIZE = 30
BN_TRADE_JOURNAL_PATH = LOGS_DIR / "trade_journal_banknifty.jsonl"
BN_OPEN_TRADES_PATH = STATE_DIR / "open_trades_banknifty.json"
BN_DECISION_LOG_PATH = LOGS_DIR / "decision_log_banknifty.jsonl"
BN_CONDOR_STATE_PATH = STATE_DIR / "condor_position_banknifty.json"
BN_SPREAD_STATE_PATH = STATE_DIR / "directional_spread_position_banknifty.json"
BN_PRICE_ACTION_STATE_PATH = STATE_DIR / "price_action_position_banknifty.json"
BN_PRICE_ACTION_JOURNAL_PATH = LOGS_DIR / "price_action_journal_banknifty.jsonl"


def _read_todays_closed_trades() -> list:
    """
    Reads logs/trade_journal.jsonl and returns only entries closed TODAY
    -- so exited trades don't just vanish from the live view the moment
    they close, they move into a "Closed Today" section instead.
    """
    if not TRADE_JOURNAL_PATH.exists():
        return []
    today_str = date.today().isoformat()
    closed_today = []
    try:
        with open(TRADE_JOURNAL_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                closed_at = entry.get("closed_at", "")
                if closed_at.startswith(today_str):
                    closed_today.append(entry)
    except Exception:
        return []
    return closed_today


def _enrich_open_trades(trades: list) -> list:
    """
    Adds computed fields the tracker doesn't persist: capital deployed
    (entry * lot size * lots), and the trade's live R-multiple position.

    R is the trade's own risk unit (entry - stop), so `current_r` answers
    "how far has this gone, measured in what it stands to lose" -- a scale
    that's comparable across trades in a way raw percentages aren't.
    """
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)
    enriched = []
    for t in trades:
        t = dict(t)
        t["capital_deployed"] = round(t.get("entry", 0) * lot_size * t.get("lots", 1), 2)
        t["current_r"] = tt.r_multiple(t, t.get("current_ltp"))
        t["target_r"] = getattr(config, "DEFAULT_TARGET_RR", None)
        hit = sorted(float(m) for m in (t.get("rr_milestones_hit") or {}))
        t["best_milestone_hit"] = hit[-1] if hit else None
        enriched.append(t)
    return enriched


def _read_price_action_closed_today() -> list:
    """Same shape and reasoning as _read_todays_closed_trades, pointed at
    the price-action strategy's own journal instead of momentum's."""
    path = Path(pacfg.JOURNAL_PATH)
    if not path.exists():
        return []
    today_str = date.today().isoformat()
    closed_today = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (entry.get("closed_at") or "").startswith(today_str):
                    closed_today.append(entry)
    except Exception:
        return []
    return closed_today


def _enrich_price_action_trades(trades: list) -> list:
    """Same computed fields as _enrich_open_trades (capital deployed,
    live R-multiple), using price_action_tracker's own r_multiple and
    config_price_action's lot size/target R rather than momentum's."""
    lot_size = getattr(pacfg, "NIFTY_LOT_SIZE", 65)
    enriched = []
    for t in trades:
        t = dict(t)
        t["capital_deployed"] = round(t.get("entry", 0) * lot_size * t.get("lots", 1), 2)
        t["current_r"] = pat.r_multiple(t, t.get("current_ltp"))
        t["target_r"] = getattr(pacfg, "TARGET_RR", None)
        hit = sorted(float(m) for m in (t.get("rr_milestones_hit") or {}))
        t["best_milestone_hit"] = hit[-1] if hit else None
        enriched.append(t)
    return enriched


def _read_closed_today(journal_path: Path) -> list:
    """
    Generic version of _read_todays_closed_trades / _read_price_action_closed_today
    -- same logic, parameterized by journal path, so the same helper covers
    any strategy's journal (including a Bank Nifty variant's own) without
    duplicating the read/filter loop a third and fourth time.
    """
    if not journal_path.exists():
        return []
    today_str = date.today().isoformat()
    closed_today = []
    try:
        with open(journal_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (entry.get("closed_at") or "").startswith(today_str):
                    closed_today.append(entry)
    except Exception:
        return []
    return closed_today


def _enrich_trades(trades: list, lot_size: int, r_multiple_fn, target_rr) -> list:
    """
    Generic version of _enrich_open_trades / _enrich_price_action_trades --
    same computed fields (capital deployed, live R-multiple), parameterized
    by lot size and which tracker's r_multiple() to use, so Bank Nifty's
    own lot size (30, not 65) and trackers can reuse this without a
    NIFTY-specific value baked in.
    """
    enriched = []
    for t in trades:
        t = dict(t)
        t["capital_deployed"] = round(t.get("entry", 0) * lot_size * t.get("lots", 1), 2)
        t["current_r"] = r_multiple_fn(t, t.get("current_ltp"))
        t["target_r"] = target_rr
        hit = sorted(float(m) for m in (t.get("rr_milestones_hit") or {}))
        t["best_milestone_hit"] = hit[-1] if hit else None
        enriched.append(t)
    return enriched


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_jsonl_tail(path: Path, n: int) -> list:
    """Read the last n JSON objects from a JSONL file without loading the whole file for huge logs."""
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        tail = lines[-n:] if len(lines) > n else lines
        records = []
        for line in tail:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records
    except Exception:
        return []


def todays_decision_log_path() -> Path:
    # decision_log.py writes to a single ever-growing file, not date-stamped
    # (see decision_log.py -- LOG_PATH is fixed). Kept as a function here in
    # case that changes later.
    return LOGS_DIR / "decision_log.jsonl"


def todays_main_log_path() -> Path:
    return LOGS_DIR / f"nifty_scan_{datetime.now().strftime('%Y%m%d')}.log"


def todays_price_action_log_path() -> Path:
    return LOGS_DIR / f"price_action_{datetime.now().strftime('%Y%m%d')}.log"


def todays_banknifty_main_log_path() -> Path:
    return LOGS_DIR / f"banknifty_scan_{datetime.now().strftime('%Y%m%d')}.log"


def todays_banknifty_price_action_log_path() -> Path:
    return LOGS_DIR / f"price_action_banknifty_{datetime.now().strftime('%Y%m%d')}.log"


DASHBOARD_PNL_HTML_PATH = BASE_DIR / "dashboard" / "pnl_dashboard.html"

# Every closed-trade journal this project produces, across both indices
# and all four strategies. Not every combination has ever run live (e.g.
# Bank Nifty condor was backtested and never adopted -- see BACKLOG.md),
# so this list intentionally includes paths that may not exist on disk;
# _load_all_pnl_trades() skips missing files rather than erroring, and
# the frontend only offers a filter for combinations that actually
# produced at least one closed trade.
PNL_JOURNALS = [
    (LOGS_DIR / "trade_journal.jsonl", "NIFTY", "Momentum (Anchor)"),
    (LOGS_DIR / "condor_journal.jsonl", "NIFTY", "Condor"),
    (LOGS_DIR / "directional_spread_journal.jsonl", "NIFTY", "Directional Spread"),
    (LOGS_DIR / "price_action_journal.jsonl", "NIFTY", "Price Action"),
    (LOGS_DIR / "trade_journal_banknifty.jsonl", "Bank Nifty", "Momentum (Anchor)"),
    (LOGS_DIR / "condor_journal_banknifty.jsonl", "Bank Nifty", "Condor"),
    (LOGS_DIR / "directional_spread_journal_banknifty.jsonl", "Bank Nifty", "Directional Spread"),
    (LOGS_DIR / "price_action_journal_banknifty.jsonl", "Bank Nifty", "Price Action"),
    # Sentinel v1.1-dev: the correlated-cluster-cap candidate, running as
    # its own paper-tracking process alongside Anchor -- see
    # STRATEGY_VERSIONS.md. Same journal shape as Anchor's momentum
    # (both use trade_tracker.py), so this needs no new parsing logic,
    # just a distinct label so the two never get pooled together.
    (LOGS_DIR / "trade_journal_sentinel.jsonl", "NIFTY", "Momentum (Sentinel)"),
    (LOGS_DIR / "trade_journal_banknifty_sentinel.jsonl", "Bank Nifty", "Momentum (Sentinel)"),
]


# Lot size per index, for deriving a peak favorable move where a
# journal doesn't already store one in rupees -- see _peak_favorable_inr.
PNL_INDEX_LOT_SIZE = {"NIFTY": 65, "Bank Nifty": 30}


def _peak_favorable_inr(t: dict, index_label: str):
    """
    Best favorable move this trade ever saw, in rupees, before its
    final close -- the thing a closed P&L figure alone can't show (a
    trade can close negative after having been well in profit). Three
    different journal shapes store this three different ways:

      - momentum: max_favorable_inr, already rupees, already lot-scaled.
      - condor / directional-spread: max_pnl_inr_seen, same shape.
      - price-action: no rupee figure at all, only max_ltp_seen -- has
        to be derived from (peak price - entry) * lot_size * lots.

    Returns None if none of those are available (e.g. a legacy journal
    entry from before excursion tracking existed) rather than a
    misleading 0, which would read as "this trade never moved."
    """
    if t.get("max_favorable_inr") is not None:
        return round(t["max_favorable_inr"], 2)
    if t.get("max_pnl_inr_seen") is not None:
        return round(t["max_pnl_inr_seen"], 2)
    max_ltp, entry, lots = t.get("max_ltp_seen"), t.get("entry"), t.get("lots")
    if max_ltp is not None and entry is not None and lots is not None:
        lot_size = PNL_INDEX_LOT_SIZE.get(index_label, 65)
        return round((max_ltp - entry) * lot_size * lots, 2)
    return None


def _load_all_pnl_trades() -> list:
    """
    Every CLOSED trade across every strategy/index journal, normalised to
    a common shape the P&L dashboard can group by day without caring
    which strategy produced it.

    The three P&L figures deliberately mirror what a broker's own P&L
    report shows (see the Groww F&O P&L dashboard this was modelled on):
    REALISED = pnl_inr, the raw price-move P&L a broker's contract note
    would show, before this project's own cost model. CHARGES = costs_inr
    if this journal's tracker computes one (momentum and price-action do;
    condor and directional-spread do not yet -- see their own trackers),
    else 0, meaning "not modelled here" rather than "zero cost", which the
    frontend must not conflate. NET = pnl_inr_net if present, else falls
    back to realised (again, not truly cost-free, just not measured for
    that strategy yet).

    Also carries peak_favorable_inr and peak_r -- the best this trade
    ever looked before it closed, which the closed-trade figures above
    can't show on their own. Added 2026-08-15: a real Bank Nifty session
    closed six trades net negative that had each run to ~0.75R in
    profit first, and the dashboard had no way to surface that a trade
    gave back real ground rather than never having been ahead.
    """
    trades = []
    for path, index_label, strategy_label in PNL_JOURNALS:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            closed_at = t.get("closed_at")
            if not closed_at:
                continue   # still open -- the P&L dashboard is realised-trades only
            realised = t.get("pnl_inr")
            if realised is None:
                continue   # no recorded P&L at all, nothing to plot
            has_cost_model = "costs_inr" in t
            charges = t.get("costs_inr") if has_cost_model else 0.0
            net = t.get("pnl_inr_net") if t.get("pnl_inr_net") is not None else realised
            trades.append({
                "index": index_label,
                "strategy": strategy_label,
                "opened_at": t.get("opened_at"),
                "closed_at": closed_at,
                "date": closed_at[:10],
                "realised_pnl_inr": round(realised, 2),
                "charges_inr": round(charges, 2) if charges is not None else 0.0,
                "charges_modelled": has_cost_model,
                "net_pnl_inr": round(net, 2),
                "strike": t.get("strike"),
                "option_type": t.get("option_type"),
                "outcome": t.get("outcome") or t.get("status"),
                "lots": t.get("lots"),
                "peak_favorable_inr": _peak_favorable_inr(t, index_label),
                "peak_r": t.get("max_r_seen"),
            })
    return trades


def build_state() -> dict:
    """Assemble everything the dashboard needs from whatever's on disk right now."""
    decision_cycles = _read_jsonl_tail(todays_decision_log_path(), DECISION_LOG_TAIL_LINES)
    latest_cycle = decision_cycles[-1] if decision_cycles else None

    open_trades_state = _read_json(STATE_DIR / "open_trades.json", default={})
    open_trades_raw = open_trades_state.get("trades", []) if open_trades_state else []
    open_trades = _enrich_open_trades(open_trades_raw)
    closed_today = _read_todays_closed_trades()

    total_capital_deployed = round(sum(t["capital_deployed"] for t in open_trades), 2)
    total_open_pnl_inr = round(sum(t.get("running_pnl_inr", 0) or 0 for t in open_trades), 2)
    total_realized_pnl_inr = round(sum(t.get("pnl_inr", 0) or 0 for t in closed_today), 2)
    total_pnl_today_inr = round(total_open_pnl_inr + total_realized_pnl_inr, 2)

    condor_state = _read_json(STATE_DIR / "condor_position.json", default={})
    # NEVER READ before 2026-08-03: the directional spread strategy had no
    # dashboard visibility at all -- condor_position.json was read here,
    # directional_spread_position.json was not, and the frontend had no
    # panel for it either. A real position was opened, breached, and
    # approved via approve_orders.py entirely invisibly on the dashboard.
    spread_state = _read_json(STATE_DIR / "directional_spread_position.json", default={})
    price_action_state = _read_json(STATE_DIR / "price_action_position.json", default={})
    price_action_trades = _enrich_price_action_trades(
        (price_action_state or {}).get("trades", []) if price_action_state else []
    )
    price_action_closed_today = _read_price_action_closed_today()
    opening_gap = _read_json(STATE_DIR / "opening_gap.json", default={})
    staged_orders = _read_json(STATE_DIR / "staged_orders.json", default=[])
    pending_staged = [r for r in (staged_orders or []) if r.get("status") == "PENDING"]

    # Mirror what risk_checker.check() is actually being handed each cycle.
    # Computed here rather than read from a file because trade_tracker owns
    # the definition -- duplicating the formula would let the two drift.
    try:
        exposure_pct, daily_loss_pct = tt.compute_risk_state(
            {"trades": open_trades_raw} if open_trades_raw else {"trades": []}
        )
        risk_state = {
            "open_exposure_pct": exposure_pct,
            "max_total_exposure_pct": config.MAX_TOTAL_EXPOSURE_PCT,
            "daily_loss_pct": daily_loss_pct,
            "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
            "breaker_tripped": daily_loss_pct >= config.MAX_DAILY_LOSS_PCT,
        }
    except Exception as e:
        risk_state = {"error": str(e)}

    try:
        rr_stats = tt.rr_milestone_stats()
    except Exception as e:
        rr_stats = {"error": str(e), "sample": 0, "milestones": []}

    # Same evidence, other two strategies' own unit (% of max profit,
    # since a credit spread/condor has no symmetric "R" -- see each
    # tracker's profit_milestone_stats docstring).
    try:
        spread_profit_stats = dst.profit_milestone_stats()
    except Exception as e:
        spread_profit_stats = {"error": str(e), "sample": 0, "milestones": []}
    try:
        condor_profit_stats = ct.profit_milestone_stats()
    except Exception as e:
        condor_profit_stats = {"error": str(e), "sample": 0, "milestones": []}

    main_log_path = todays_main_log_path()
    log_age_seconds = None
    if main_log_path.exists():
        log_age_seconds = round(datetime.now().timestamp() - main_log_path.stat().st_mtime, 1)

    price_action_log_path = todays_price_action_log_path()
    price_action_log_age_seconds = None
    if price_action_log_path.exists():
        price_action_log_age_seconds = round(
            datetime.now().timestamp() - price_action_log_path.stat().st_mtime, 1
        )

    def _position_age_seconds(position: dict):
        """
        Seconds since this position's MTM was last marked, from its own
        mtm_updated_at field -- not the state file's mtime, which would
        also tick on unrelated writes. Answers "is this MTM actually
        live" directly rather than leaving it implied.
        """
        if not position or not position.get("mtm_updated_at"):
            return None
        try:
            updated = datetime.fromisoformat(position["mtm_updated_at"])
            return round((datetime.now() - updated).total_seconds(), 1)
        except (ValueError, TypeError):
            return None

    condor_position = (condor_state or {}).get("position")
    spread_position = (spread_state or {}).get("position")

    # --- Bank Nifty: own section entirely, never merged into the NIFTY
    # fields above -- each main_*_banknifty.py process writes its own
    # state/journal/log files (see the BN_* path constants), so this
    # mirrors the NIFTY block but reads those instead. Bank Nifty condor
    # (main_condor_banknifty.py) isn't run live (tested and closed as
    # net-negative, see BACKLOG.md) -- its state file plausibly won't
    # exist, which the panel below handles the same as "no position open".
    bn_decision_cycles = _read_jsonl_tail(BN_DECISION_LOG_PATH, DECISION_LOG_TAIL_LINES)
    bn_latest_cycle = bn_decision_cycles[-1] if bn_decision_cycles else None

    bn_open_trades_state = _read_json(BN_OPEN_TRADES_PATH, default={})
    bn_open_trades_raw = bn_open_trades_state.get("trades", []) if bn_open_trades_state else []
    bn_open_trades = _enrich_trades(bn_open_trades_raw, BANKNIFTY_LOT_SIZE, tt.r_multiple, config.DEFAULT_TARGET_RR)
    bn_closed_today = _read_closed_today(BN_TRADE_JOURNAL_PATH)

    bn_total_capital_deployed = round(sum(t["capital_deployed"] for t in bn_open_trades), 2)
    bn_total_open_pnl_inr = round(sum(t.get("running_pnl_inr", 0) or 0 for t in bn_open_trades), 2)
    bn_total_realized_pnl_inr = round(sum(t.get("pnl_inr", 0) or 0 for t in bn_closed_today), 2)
    bn_total_pnl_today_inr = round(bn_total_open_pnl_inr + bn_total_realized_pnl_inr, 2)

    bn_condor_state = _read_json(BN_CONDOR_STATE_PATH, default={})
    bn_spread_state = _read_json(BN_SPREAD_STATE_PATH, default={})
    bn_price_action_state = _read_json(BN_PRICE_ACTION_STATE_PATH, default={})
    bn_price_action_trades = _enrich_trades(
        (bn_price_action_state or {}).get("trades", []) if bn_price_action_state else [],
        BANKNIFTY_LOT_SIZE, pat.r_multiple, getattr(pacfg, "TARGET_RR", None),
    )
    bn_price_action_closed_today = _read_closed_today(BN_PRICE_ACTION_JOURNAL_PATH)

    bn_main_log_path = todays_banknifty_main_log_path()
    bn_log_age_seconds = None
    if bn_main_log_path.exists():
        bn_log_age_seconds = round(datetime.now().timestamp() - bn_main_log_path.stat().st_mtime, 1)

    bn_price_action_log_path = todays_banknifty_price_action_log_path()
    bn_price_action_log_age_seconds = None
    if bn_price_action_log_path.exists():
        bn_price_action_log_age_seconds = round(
            datetime.now().timestamp() - bn_price_action_log_path.stat().st_mtime, 1
        )

    bn_condor_position = (bn_condor_state or {}).get("position")
    bn_spread_position = (bn_spread_state or {}).get("position")

    banknifty = {
        "latest_cycle": bn_latest_cycle,
        "recent_cycles": bn_decision_cycles,
        "open_trades": bn_open_trades,
        "closed_today": bn_closed_today,
        "opened_today": bn_open_trades_state.get("opened_today") if bn_open_trades_state else None,
        "totals": {
            "capital_deployed": bn_total_capital_deployed,
            "open_pnl_inr": bn_total_open_pnl_inr,
            "realized_pnl_inr": bn_total_realized_pnl_inr,
            "total_pnl_today_inr": bn_total_pnl_today_inr,
        },
        "condor_position": bn_condor_position,
        "condor_position_age_seconds": _position_age_seconds(bn_condor_position),
        "condor_poll_interval_seconds": ccfg.POLL_INTERVAL_SECONDS,
        "directional_spread_position": bn_spread_position,
        "directional_spread_position_age_seconds": _position_age_seconds(bn_spread_position),
        "directional_spread_poll_interval_seconds": getattr(dcfg, "POLL_INTERVAL_SECONDS", None),
        "main_log_age_seconds": bn_log_age_seconds,
        "price_action_trades": bn_price_action_trades,
        "price_action_closed_today": bn_price_action_closed_today,
        "price_action_log_age_seconds": bn_price_action_log_age_seconds,
        "price_action_poll_interval_seconds": getattr(pacfg, "POLL_INTERVAL_SECONDS", None),
    }

    return {
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "latest_cycle": latest_cycle,
        "recent_cycles": decision_cycles,
        "open_trades": open_trades,
        "closed_today": closed_today,
        "opened_today": open_trades_state.get("opened_today") if open_trades_state else None,
        "totals": {
            "capital_deployed": total_capital_deployed,
            "open_pnl_inr": total_open_pnl_inr,
            "realized_pnl_inr": total_realized_pnl_inr,
            "total_pnl_today_inr": total_pnl_today_inr,
        },
        "condor_position": condor_position,
        "condor_position_age_seconds": _position_age_seconds(condor_position),
        "condor_poll_interval_seconds": ccfg.POLL_INTERVAL_SECONDS,
        "directional_spread_position": spread_position,
        "directional_spread_position_age_seconds": _position_age_seconds(spread_position),
        "directional_spread_poll_interval_seconds": getattr(dcfg, "POLL_INTERVAL_SECONDS", None),
        "opening_gap": opening_gap,
        "pending_approvals": pending_staged,
        "main_log_age_seconds": log_age_seconds,
        # Live risk-gate state. These drive real decisions in
        # risk_checker.check() (and used to be silently hardcoded to 0.0),
        # so they belong on the dashboard rather than only in the log.
        "risk_state": risk_state,
        # Aggregate R-multiple history: how often trades actually reach
        # each candidate target, and the simulated expectancy of setting
        # the target there. Evidence for tuning DEFAULT_TARGET_RR later.
        "rr_stats": rr_stats,
        "directional_spread_profit_stats": spread_profit_stats,
        "condor_profit_stats": condor_profit_stats,
        "price_action_trades": price_action_trades,
        "price_action_closed_today": price_action_closed_today,
        "price_action_poll_interval_seconds": getattr(pacfg, "POLL_INTERVAL_SECONDS", None),
        "price_action_log_age_seconds": price_action_log_age_seconds,
        "banknifty": banknifty,
    }


class DashboardServer(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """
        A browser tab closed or refreshed mid-response is normal traffic
        for a page that polls every few seconds, not a server fault --
        the client just isn't there to receive the bytes anymore. The
        default socketserver behaviour prints a full traceback per
        occurrence, which reads as something broke every time someone
        reloads the page. Only THESE specific "the other end went away"
        exceptions are swallowed (quietly noted, not silently dropped);
        anything else still prints the full traceback, since that could
        be a real bug in build_state() or the handler.
        """
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet -- don't spam the terminal with every poll request

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html(DASHBOARD_HTML_PATH)
        elif self.path == "/api/state":
            self._serve_state()
        elif self.path == "/pnl" or self.path == "/pnl.html":
            self._serve_html(DASHBOARD_PNL_HTML_PATH)
        elif self.path == "/api/pnl":
            self._serve_pnl()
        else:
            self.send_error(404, "Not found")

    def _serve_html(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.send_error(500, f"Dashboard HTML not found at {path}")
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_state(self):
        try:
            state = build_state()
            body = json.dumps(state, default=str).encode("utf-8")
            status = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_pnl(self):
        try:
            trades = _load_all_pnl_trades()
            body = json.dumps({"trades": trades, "generated_at": datetime.now().isoformat()},
                              default=str).encode("utf-8")
            status = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    with DashboardServer((HOST, PORT), DashboardHandler) as httpd:
        print(f"Live dashboard running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
        print("Read-only -- this never writes to any state file or talks to Dhan/NSE itself.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nDashboard server stopped.")


if __name__ == "__main__":
    main()
