"""
Binary packet decoding for Dhan's WebSocket live market feed.

WHY THIS IS ITS OWN MODULE
--------------------------
Every other data path in this project parses JSON, where a wrong field
name raises a KeyError. This one parses a packed binary stream by byte
offset, where a wrong offset does not raise anything -- it reads four
adjacent bytes and returns a perfectly plausible float. An off-by-four
error in the depth block would produce bid/ask quantities that look like
real numbers and are simply not the ones the exchange sent.

That is the same silent-corruption failure mode that produced the
one-bar timestamp shift, the flat iv_percentile, and the absent OI
buildup earlier in this project's history -- each of which yielded
confident, entirely wrong results. So the decoding lives here, as pure
functions over bytes with no I/O and no sockets, and is tested against
packets built byte-by-byte from the published spec.

OFFSET CONVENTION
-----------------
Dhan's documentation numbers bytes from 1 and states ranges inclusively
("9-12 | float32 | Last Traded Price"). Python slices from 0, so every
documented start byte N appears here as offset N-1. The doc's range is
kept in a comment beside each field so the two can be diffed by eye
against the published table without re-deriving the arithmetic.

Endianness is little-endian ("<" in struct), as the docs specify.
"""

import struct

# --- Feed response codes (first byte of every packet) ---
CODE_TICKER = 2
CODE_QUOTE = 4
CODE_OI = 5
CODE_PREV_CLOSE = 6
CODE_FULL = 8
CODE_DISCONNECT = 50

HEADER_SIZE = 8

# Packet sizes, used to split a frame that carries several packets
# back-to-back. A code we don't recognise has unknown length, which means
# the rest of that frame cannot be safely walked -- see split_packets.
PACKET_SIZES = {
    CODE_TICKER: 16,
    CODE_QUOTE: 50,
    CODE_OI: 12,
    CODE_PREV_CLOSE: 16,
    CODE_FULL: 162,
    CODE_DISCONNECT: 10,
}

DEPTH_LEVELS = 5
DEPTH_ENTRY_SIZE = 20


def parse_header(buf: bytes) -> dict:
    """
    Response header, present on every packet.

    doc bytes 1 / 2-3 / 4 / 5-8  ->  offsets 0 / 1-2 / 3 / 4-7
    """
    if len(buf) < HEADER_SIZE:
        return None
    code, length, segment, security_id = struct.unpack_from("<BHBi", buf, 0)
    return {
        "code": code,
        "length": length,
        "segment": segment,
        "security_id": security_id,
    }


def parse_ticker(buf: bytes) -> dict:
    """doc 9-12 float32 LTP, 13-16 int32 last trade time."""
    ltp, ltt = struct.unpack_from("<fi", buf, 8)
    return {"ltp": ltp, "last_trade_time": ltt}


def parse_oi(buf: bytes) -> dict:
    """doc 9-12 int32 open interest."""
    (oi,) = struct.unpack_from("<i", buf, 8)
    return {"oi": oi}


def parse_prev_close(buf: bytes) -> dict:
    """doc 9-12 float32 prev close, 13-16 int32 prev day OI."""
    prev_close, prev_oi = struct.unpack_from("<fi", buf, 8)
    return {"prev_close": prev_close, "prev_oi": prev_oi}


def parse_disconnect(buf: bytes) -> dict:
    """doc 9-10 int16 disconnection code."""
    (reason,) = struct.unpack_from("<h", buf, 8)
    return {"disconnect_code": reason}


# Field layouts as (name, struct_code) in wire order, and the SINGLE
# source of truth for both the format string and the field names.
#
# Written as a table rather than a literal format string because the
# first version of this file hand-wrote "<fhiifiiffff" with avg_price
# typed as int32 and volume as float32 -- transposed. Both are 4 bytes,
# so the packet still measured exactly the documented 50 and a size
# assertion passed clean while every value after LTP would have been
# garbage. A table makes each field's type sit directly beside its name.
QUOTE_FIELDS = (
    ("ltp", "f"),               # doc 9-12
    ("last_qty", "h"),          # doc 13-14
    ("last_trade_time", "i"),   # doc 15-18
    ("avg_price", "f"),         # doc 19-22
    ("volume", "i"),            # doc 23-26
    # The exchange sends SELL before BUY. Swapping these would invert
    # every imbalance signal downstream while still producing entirely
    # plausible-looking numbers.
    ("total_sell_qty", "i"),    # doc 27-30
    ("total_buy_qty", "i"),     # doc 31-34
    ("day_open", "f"),          # doc 35-38
    # Doc labels this "Day Close", but it is empirically the PREVIOUS
    # session's close. Verified 2026-08-02 against the REST chain's
    # previous_close_price -- exact match on every contract checked --
    # and the values routinely sit outside the same packet's own day
    # high/low, which is impossible for a close of the session being
    # reported. Named for what it is, not what the doc calls it.
    ("prev_close", "f"),        # doc 39-42, labelled "Day Close"
    ("day_high", "f"),          # doc 43-46
    ("day_low", "f"),           # doc 47-50
)

FULL_FIELDS = (
    ("ltp", "f"),               # doc 9-12
    ("last_qty", "h"),          # doc 13-14
    ("last_trade_time", "i"),   # doc 15-18
    ("avg_price", "f"),         # doc 19-22
    ("volume", "i"),            # doc 23-26
    ("total_sell_qty", "i"),    # doc 27-30
    ("total_buy_qty", "i"),     # doc 31-34
    # These three OI int32s are what make the Full packet's OHLC block
    # sit 12 bytes further along than the Quote packet's. Reusing Quote
    # offsets here would read an OI count as a price without erroring.
    ("oi", "i"),                # doc 35-38
    ("oi_day_high", "i"),       # doc 39-42
    ("oi_day_low", "i"),        # doc 43-46
    ("day_open", "f"),          # doc 47-50
    ("prev_close", "f"),        # doc 51-54, labelled "Day Close" -- see QUOTE_FIELDS
    ("day_high", "f"),          # doc 55-58
    ("day_low", "f"),           # doc 59-62
)

DEPTH_FIELDS = (
    ("bid_qty", "i"),           # doc 1-4
    ("ask_qty", "i"),           # doc 5-8
    ("bid_orders", "h"),        # doc 9-10
    ("ask_orders", "h"),        # doc 11-12
    ("bid_price", "f"),         # doc 13-16
    ("ask_price", "f"),         # doc 17-20
)


def struct_format(fields) -> str:
    """Little-endian, unaligned format string for a field table."""
    return "<" + "".join(code for _name, code in fields)


QUOTE_FORMAT = struct_format(QUOTE_FIELDS)
FULL_FORMAT = struct_format(FULL_FIELDS)
DEPTH_FORMAT = struct_format(DEPTH_FIELDS)


def _unpack_fields(fields, fmt, buf, offset) -> dict:
    values = struct.unpack_from(fmt, buf, offset)
    return {name: value for (name, _code), value in zip(fields, values)}


def parse_quote(buf: bytes) -> dict:
    """Quote packet body -- see QUOTE_FIELDS for the layout and doc bytes."""
    return _unpack_fields(QUOTE_FIELDS, QUOTE_FORMAT, buf, 8)


def parse_depth(buf: bytes, offset: int) -> list:
    """Five 20-byte depth entries starting at `offset` -- see DEPTH_FIELDS."""
    return [
        _unpack_fields(DEPTH_FIELDS, DEPTH_FORMAT, buf, offset + i * DEPTH_ENTRY_SIZE)
        for i in range(DEPTH_LEVELS)
    ]


def parse_full(buf: bytes) -> dict:
    """
    Full packet body plus its 5-level depth block -- see FULL_FIELDS for
    the layout, and note the three OI int32s that push OHLC 12 bytes
    later than the Quote packet puts it.
    """
    out = _unpack_fields(FULL_FIELDS, FULL_FORMAT, buf, 8)
    out["depth"] = parse_depth(buf, 8 + struct.calcsize(FULL_FORMAT))
    return out


_BODY_PARSERS = {
    CODE_TICKER: parse_ticker,
    CODE_QUOTE: parse_quote,
    CODE_OI: parse_oi,
    CODE_PREV_CLOSE: parse_prev_close,
    CODE_FULL: parse_full,
    CODE_DISCONNECT: parse_disconnect,
}


def parse_packet(buf: bytes) -> dict:
    """
    One packet -> {header fields..., body fields...}, or None if the
    buffer is too short or the code is unrecognised.

    Returns None rather than raising on an unknown code: Dhan can add
    response types, and a feed that dies on an unfamiliar packet is worse
    than one that ignores it.
    """
    header = parse_header(buf)
    if header is None:
        return None
    parser = _BODY_PARSERS.get(header["code"])
    if parser is None:
        return None
    expected = PACKET_SIZES.get(header["code"], HEADER_SIZE)
    if len(buf) < expected:
        return None
    out = dict(header)
    out.update(parser(buf))
    return out


def split_packets(frame: bytes) -> list:
    """
    A single WebSocket frame can carry several packets back to back.

    Walks the frame by each packet's KNOWN size rather than trusting the
    header's own length field, and stops at the first unrecognised code:
    without a known size there is no way to find where the next packet
    begins, and guessing would silently mis-align everything after it --
    far worse than dropping the tail of one frame.
    """
    packets = []
    offset = 0
    while offset + HEADER_SIZE <= len(frame):
        code = frame[offset]
        size = PACKET_SIZES.get(code)
        if size is None or offset + size > len(frame):
            break
        parsed = parse_packet(frame[offset:offset + size])
        if parsed is not None:
            packets.append(parsed)
        offset += size
    return packets
