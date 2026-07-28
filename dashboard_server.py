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

HOST = "127.0.0.1"
PORT = 8787

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"
DASHBOARD_HTML_PATH = BASE_DIR / "dashboard" / "live_dashboard.html"
TRADE_JOURNAL_PATH = LOGS_DIR / "trade_journal.jsonl"

DECISION_LOG_TAIL_LINES = 5   # how many recent cycles to show in the "recent activity" panel


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
    """Adds a computed capital_deployed figure (entry * lot size * lots) to each open trade."""
    lot_size = getattr(config, "NIFTY_LOT_SIZE", 65)
    enriched = []
    for t in trades:
        t = dict(t)
        t["capital_deployed"] = round(t.get("entry", 0) * lot_size * t.get("lots", 1), 2)
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
    opening_gap = _read_json(STATE_DIR / "opening_gap.json", default={})
    staged_orders = _read_json(STATE_DIR / "staged_orders.json", default=[])
    pending_staged = [r for r in (staged_orders or []) if r.get("status") == "PENDING"]

    main_log_path = todays_main_log_path()
    log_age_seconds = None
    if main_log_path.exists():
        log_age_seconds = round(datetime.now().timestamp() - main_log_path.stat().st_mtime, 1)

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
        "condor_position": (condor_state or {}).get("position"),
        "opening_gap": opening_gap,
        "pending_approvals": pending_staged,
        "main_log_age_seconds": log_age_seconds,
    }


class DashboardServer(socketserver.TCPServer):
    allow_reuse_address = True


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet -- don't spam the terminal with every poll request

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/state":
            self._serve_state()
        else:
            self.send_error(404, "Not found")

    def _serve_html(self):
        try:
            content = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.send_error(500, f"Dashboard HTML not found at {DASHBOARD_HTML_PATH}")
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
