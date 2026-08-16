"""
Bank Nifty counterpart to orderflow_feed.py -- same WebSocket feed, same
OrderFlowFeed class (it already takes state_path as a constructor
argument, so nothing there is NIFTY-specific), pointed at Bank Nifty's
own underlying scrip/segment, its own state file, and its own spread-
recording directory.

WHY THIS EXISTS
---------------
orderflow_feed.py only ever subscribed to NIFTY strikes (confirmed by
inspecting its state file's tracked strikes: 24100-24650, never anything
near Bank Nifty's ~50000+ range). That meant Bank Nifty's decision_log
book_imbalance/total_quantity_imbalance were structurally guaranteed to
always read None, forever -- not a "not enough data yet" gap, a coverage
gap no amount of waiting would fix. Found 2026-08-16 while investigating
whether there was enough order-flow data to analyse. See BACKLOG.md.

WHY A SEPARATE PROCESS, NOT A --underlying FLAG ON THE EXISTING SCRIPT
------------------------------------------------------------------------
Same reasoning as every other NIFTY/Bank Nifty pair in this project
(main_live.py / main_live_banknifty.py, main_condor.py /
main_condor_banknifty.py, ...): a WebSocket connection is long-lived and
per-underlying, Dhan's 5-connections-per-user cap means each index gets
its own anyway, and running two independent processes means one dying
never takes the other down.

Run:
    python3 orderflow_feed_banknifty.py --strike-range 600
"""

import argparse
import logging
import signal
import threading
from pathlib import Path

import dhan_source
import orderflow_recorder
from orderflow_feed import OrderFlowFeed, select_contracts

log = logging.getLogger("orderflow")

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_PATH = STATE_DIR / "orderflow_banknifty.json"

# Bank Nifty strikes are spaced 100pts apart (vs NIFTY's 50pts) and the
# index itself sits roughly 2x NIFTY's level, so a strike-range reach
# needs roughly double NIFTY's to cover the same number of strikes either
# side of spot. NIFTY's own automation invocation uses 300pts (6 strikes
# each side at 50pt spacing); 600pts here covers the same 6 strikes each
# side at 100pt spacing. First-pass estimate, not independently tuned --
# same caveat this project already gives every other NIFTY-derived
# Bank Nifty default (see main_live_banknifty_sentinel.py's own note).
DEFAULT_STRIKE_RANGE_POINTS = 600


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--expiry", help="YYYY-MM-DD; defaults to nearest")
    p.add_argument("--strike-range", type=float, default=DEFAULT_STRIKE_RANGE_POINTS,
                   help=f"points either side of spot (default {DEFAULT_STRIKE_RANGE_POINTS})")
    p.add_argument("--duration", type=float, default=None,
                   help="seconds to run before exiting; omit to run until stopped")
    p.add_argument("--no-record", action="store_true",
                   help="skip session spread recording (on by default)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    # Own recording directory -- orderflow_recorder.py is written as one
    # RECORD_DIR per process (same pattern as trade_tracker.JOURNAL_PATH
    # for every other NIFTY/Bank Nifty pair in this project), safe here
    # because this runs as its own OS process, never sharing a Python
    # interpreter with orderflow_feed.py's own NIFTY-only instance.
    orderflow_recorder.RECORD_DIR = Path(__file__).parent / "logs" / "orderflow_banknifty"

    contracts, spot = select_contracts(
        args.expiry, args.strike_range,
        underlying="BANKNIFTY",
        underlying_scrip=dhan_source.BANKNIFTY_UNDERLYING_SCRIP,
        underlying_seg=dhan_source.BANKNIFTY_UNDERLYING_SEG,
    )
    if not contracts:
        print("No contracts selected -- check the expiry and instrument master.")
        return
    log.info(f"  [orderflow-bn] selected {len(contracts)} contracts around spot {spot}")

    feed = OrderFlowFeed(contracts, state_path=STATE_PATH,
                         record_spreads=not args.no_record, reference_spot=spot)

    def _shutdown(signum, frame):
        log.info("  [orderflow-bn] shutting down")
        feed.stop()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass   # SIGTERM is not settable everywhere on Windows

    if args.duration:
        threading.Timer(args.duration, feed.stop).start()

    feed.run_forever()
    log.info(f"  [orderflow-bn] stopped after {feed.packets_seen:,} packets, "
             f"{feed.samples_recorded:,} spread samples recorded")


if __name__ == "__main__":
    main()
