"""
Persistent WebSocket process for Dhan's live market feed, maintaining an
order-book snapshot for the NIFTY option chain.

WHAT THIS ACTUALLY PROVIDES, AND WHAT IT DOES NOT
--------------------------------------------------
Dhan's feed carries the ORDER BOOK -- five levels of resting bid/ask
quantities and prices, plus exchange-wide total buy/sell quantity. It
does NOT carry a trade tape with an aggressor flag, so classic footprint
/ cumulative-delta order flow ("was this print a buy or a sell?") cannot
be built from it. What can be built is book IMBALANCE: how much size is
resting on each side and how it shifts. Those are different signals, and
conflating them would be the kind of category error that makes a metric
look meaningful while measuring something else.

The second, arguably larger win is real BID/ASK. Every backtest number
in this project is currently optimistic because historical reconstruction
carries no book, so fills are priced at LTP (see historical_source.py's
docstring). A live book means forward results can finally be measured
against the side actually crossed.

WHY IT IS A SEPARATE PROCESS
----------------------------
Everything else here polls REST on an interval. A WebSocket is
long-lived, event-driven, and must not be interrupted by a trading
loop's own timing -- so it runs on its own (like main_live.py,
main_condor.py and main_directional_spread.py do relative to each other)
and communicates through a state file rather than in-process calls.

Strategies READ that file; nothing blocks on this process being alive.
If the feed dies, the file goes stale and readers see stale data -- which
is why orderflow.py checks age rather than mere existence. A missing
book is missing information, not a flat book. That distinction is the
same one behind the 2026-07-30 condor "MTM unavailable" incident.

RATE LIMITS AND ISOLATION
-------------------------
The WebSocket is subject to its own limits (5 connections per user, 5000
instruments per connection, 100 per subscribe message) and does NOT
consume the REST quota that dhan_rate_limiter.py coordinates. One REST
call is made at startup to fetch the chain for strike selection; that one
goes through the shared limiter like any other.

NEVER PLACES ORDERS. This is a read-only market data feed -- the Dhan
order API is a different host and is not imported anywhere in this file
or this project.
"""

import argparse
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

import websocket

import config
import dhan_source
import instrument_master
import orderflow_packets as packets
from atomic_state import atomic_write_json

log = logging.getLogger("orderflow")

FEED_URL = "wss://api-feed.dhan.co"

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_PATH = STATE_DIR / "orderflow.json"

# Dhan's documented ceilings.
MAX_INSTRUMENTS_PER_MESSAGE = 100
MAX_INSTRUMENTS_PER_CONNECTION = 5000

REQUEST_CODE_SUBSCRIBE_FULL = 21   # Full packet: quote + OI + 5-level depth
REQUEST_CODE_DISCONNECT = 12

# How often the in-memory book is flushed to disk. The feed itself
# updates continuously; writing on every packet would be hundreds of
# writes a second for no benefit, since the fastest reader
# (main_live.py's 5s fast-check) cannot consume them.
STATE_WRITE_INTERVAL_SECONDS = 2.0

# Reconnect backoff. Capped rather than unbounded: this is a market-hours
# process and a feed that has been down for a minute should keep trying
# at a steady cadence, not drift out to hour-long sleeps.
RECONNECT_MIN_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 30.0


def _credentials() -> tuple:
    client_id = os.environ.get("DHAN_CLIENT_ID")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise RuntimeError(
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables first."
        )
    return client_id, access_token


def feed_url() -> str:
    client_id, access_token = _credentials()
    return (f"{FEED_URL}?version=2&token={access_token}"
            f"&clientId={client_id}&authType=2")


def select_contracts(expiry: str = None, strike_range_points: float = None) -> list:
    """
    The contracts worth subscribing to: every strike within
    `strike_range_points` of spot, on the given expiry.

    Deliberately bounded rather than "the whole chain": Dhan allows 5000
    instruments per connection and the chain is only a few hundred, so
    the limit is not the constraint -- signal quality is. Strikes far
    from spot have thin, erratic books whose imbalance is noise, and
    including them would dilute any aggregate computed across the chain.
    """
    expiry = expiry or dhan_source.get_nearest_expiry()
    snapshot = dhan_source.get_nifty_snapshot(expiry=expiry)
    reach = strike_range_points or getattr(config, "STRIKE_RANGE_POINTS", 800)

    index = instrument_master.build_index()
    selected = []
    for quote in snapshot.chain:
        if abs(quote.strike - snapshot.spot) > reach:
            continue
        security_id = instrument_master.security_id_for(
            quote.strike, expiry, quote.option_type, index)
        if security_id is None:
            # A strike present in the chain but absent from the master is
            # worth surfacing, not silently skipping: it means the cached
            # master is stale relative to what is actually trading.
            log.info(f"  [orderflow] no security id for {quote.strike:.0f}{quote.option_type}"
                     f" -- instrument master may be stale")
            continue
        selected.append({
            "security_id": security_id,
            "strike": quote.strike,
            "option_type": quote.option_type,
            "expiry": expiry,
        })

    if len(selected) > MAX_INSTRUMENTS_PER_CONNECTION:
        selected = selected[:MAX_INSTRUMENTS_PER_CONNECTION]
    return selected


def subscribe_messages(contracts: list) -> list:
    """
    Subscription payloads, chunked to Dhan's 100-instruments-per-message
    limit. A single oversized message is silently ignored by the server
    rather than rejected, which would look like a feed that connects fine
    and then never sends anything.
    """
    messages = []
    for start in range(0, len(contracts), MAX_INSTRUMENTS_PER_MESSAGE):
        chunk = contracts[start:start + MAX_INSTRUMENTS_PER_MESSAGE]
        messages.append({
            "RequestCode": REQUEST_CODE_SUBSCRIBE_FULL,
            "InstrumentCount": len(chunk),
            "InstrumentList": [
                {"ExchangeSegment": instrument_master.NSE_FNO_SEGMENT,
                 "SecurityId": str(c["security_id"])}
                for c in chunk
            ],
        })
    return messages


class OrderFlowFeed:
    """
    Owns the socket, the in-memory book, and the state file. One instance
    per process.
    """

    def __init__(self, contracts: list, state_path: Path = None):
        self.contracts = contracts
        self.by_security_id = {c["security_id"]: c for c in contracts}
        self.state_path = state_path or STATE_PATH
        self.book = {}          # security_id -> latest parsed full packet
        self.lock = threading.Lock()
        self.connected_at = None
        self.packets_seen = 0
        self.last_packet_at = None
        self._stop = threading.Event()
        self._ws = None

    # --- packet handling -------------------------------------------------

    def handle_frame(self, frame: bytes):
        for packet in packets.split_packets(frame):
            if packet["code"] == packets.CODE_DISCONNECT:
                log.info(f"  [orderflow] server disconnect code "
                         f"{packet['disconnect_code']} (805 = a 6th connection was opened)")
                continue
            contract = self.by_security_id.get(packet["security_id"])
            if contract is None:
                continue
            with self.lock:
                entry = self.book.setdefault(packet["security_id"], dict(contract))
                entry.update(packet)
                entry["updated_at"] = time.time()
                self.packets_seen += 1
                self.last_packet_at = time.time()

    # --- state file ------------------------------------------------------

    def snapshot_state(self) -> dict:
        with self.lock:
            entries = [dict(v) for v in self.book.values()]
        return {
            "written_at": time.time(),
            "written_at_iso": datetime.now().isoformat(timespec="seconds"),
            "connected_at": self.connected_at,
            "subscribed": len(self.contracts),
            "receiving": len(entries),
            "packets_seen": self.packets_seen,
            "last_packet_at": self.last_packet_at,
            "contracts": entries,
        }

    def write_state(self):
        try:
            atomic_write_json(self.state_path, self.snapshot_state(), default=str)
        except Exception as e:
            # Writing state is observability, not the job. A full disk
            # must not take down a feed that strategies may be reading.
            log.info(f"  [orderflow] state write failed, continuing: {e}")

    # --- socket lifecycle ------------------------------------------------

    def _on_open(self, ws):
        self.connected_at = time.time()
        messages = subscribe_messages(self.contracts)
        for msg in messages:
            ws.send(json.dumps(msg))
        log.info(f"  [orderflow] connected, subscribed {len(self.contracts)} contracts "
                 f"in {len(messages)} message(s)")

    def _on_message(self, ws, message):
        if isinstance(message, bytes):
            self.handle_frame(message)

    def _on_error(self, ws, error):
        log.info(f"  [orderflow] socket error: {error}")

    def _on_close(self, ws, status_code, msg):
        log.info(f"  [orderflow] socket closed ({status_code})")

    def stop(self):
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def run_forever(self):
        """
        Connect, and keep reconnecting with capped backoff until stopped.

        websocket-client answers the server's 10s ping automatically; the
        connection is dropped server-side after 40s without a pong, so no
        manual heartbeat is needed here.
        """
        writer = threading.Thread(target=self._write_loop, daemon=True)
        writer.start()

        delay = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    feed_url(),
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_timeout=20)
            except Exception as e:
                log.info(f"  [orderflow] connection failed: {e}")

            if self._stop.is_set():
                break
            log.info(f"  [orderflow] reconnecting in {delay:.0f}s")
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    def _write_loop(self):
        while not self._stop.is_set():
            self.write_state()
            if self._stop.wait(STATE_WRITE_INTERVAL_SECONDS):
                break
        self.write_state()   # final flush on shutdown


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--expiry", help="YYYY-MM-DD; defaults to nearest")
    p.add_argument("--strike-range", type=float, default=None,
                   help="points either side of spot (default config.STRIKE_RANGE_POINTS)")
    p.add_argument("--duration", type=float, default=None,
                   help="seconds to run before exiting; omit to run until stopped")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    contracts = select_contracts(args.expiry, args.strike_range)
    if not contracts:
        print("No contracts selected -- check the expiry and instrument master.")
        return
    log.info(f"  [orderflow] selected {len(contracts)} contracts")

    feed = OrderFlowFeed(contracts)

    def _shutdown(signum, frame):
        log.info("  [orderflow] shutting down")
        feed.stop()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass   # SIGTERM is not settable everywhere on Windows

    if args.duration:
        threading.Timer(args.duration, feed.stop).start()

    feed.run_forever()
    log.info(f"  [orderflow] stopped after {feed.packets_seen:,} packets")


if __name__ == "__main__":
    main()
