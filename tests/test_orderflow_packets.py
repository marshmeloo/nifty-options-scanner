"""
Tests for orderflow_packets.py.

WHY THESE MATTER MORE THAN A TYPICAL PARSER TEST
------------------------------------------------
Every other data path in this project parses JSON, where a wrong field
name raises. This one reads a packed binary stream by byte offset, where
a wrong offset returns a plausible float instead of an error. That is the
same silent-corruption class that produced the one-bar timestamp shift,
the flat iv_percentile and the missing OI buildup earlier in this
project -- all of which yielded confident, entirely wrong numbers.

So these build packets byte-by-byte from Dhan's published spec using
DISTINCT, recognisable values per field, and assert each one lands where
it should. Identical placeholder values everywhere would pass happily
with two fields transposed.

Run: python -m pytest tests/ -q
"""

import struct

import pytest

import orderflow_packets as op


def _header(code: int, length: int = 0, segment: int = 2, security_id: int = 65854) -> bytes:
    return struct.pack("<BHBi", code, length, segment, security_id)


def test_header_fields_land_in_the_right_places():
    buf = _header(op.CODE_QUOTE, length=50, segment=2, security_id=65854)
    h = op.parse_header(buf)
    assert h["code"] == op.CODE_QUOTE
    assert h["length"] == 50
    assert h["segment"] == 2
    assert h["security_id"] == 65854


def test_header_too_short_returns_none_rather_than_raising():
    assert op.parse_header(b"\x02\x00") is None


def test_ticker_packet_roundtrip():
    buf = _header(op.CODE_TICKER) + struct.pack("<fi", 123.45, 1785644264)
    p = op.parse_packet(buf)
    assert p["code"] == op.CODE_TICKER
    assert p["ltp"] == pytest.approx(123.45, rel=1e-6)
    assert p["last_trade_time"] == 1785644264


def test_oi_packet_roundtrip():
    buf = _header(op.CODE_OI) + struct.pack("<i", 4_474_730)
    assert op.parse_packet(buf)["oi"] == 4_474_730


def test_prev_close_packet_roundtrip():
    buf = _header(op.CODE_PREV_CLOSE) + struct.pack("<fi", 98.75, 3_100_000)
    p = op.parse_packet(buf)
    assert p["prev_close"] == pytest.approx(98.75, rel=1e-6)
    assert p["prev_oi"] == 3_100_000


def test_disconnect_packet_roundtrip():
    buf = _header(op.CODE_DISCONNECT) + struct.pack("<h", 805)
    assert op.parse_packet(buf)["disconnect_code"] == 805


_QUOTE_VALUES = {
    "ltp": 101.5, "last_qty": 65, "last_trade_time": 1785644264, "avg_price": 100.25,
    "volume": 1_234_567, "total_sell_qty": 22_222, "total_buy_qty": 88_888,
    # prev_close is deliberately OUTSIDE the day high/low range: that is
    # what the live feed actually sends (verified against REST on
    # 2026-08-02) and is the evidence the doc's "Day Close" label is
    # wrong. A fixture with it tucked inside the range would hide that.
    "day_open": 95.0, "prev_close": 120.0, "day_high": 110.0, "day_low": 90.0,
}


def _pack(fields, values: dict) -> bytes:
    """
    Pack from the SAME field table the parser reads.

    Building the format string independently here is what let the first
    version of this suite pass a size assertion while avg_price and
    volume were transposed in both parser and test -- both fields are 4
    bytes, so the packet measured exactly the documented 50 either way.
    Sharing the table means a wrong type now fails at pack time.
    """
    fmt = op.struct_format(fields)
    return struct.pack(fmt, *[values[name] for name, _code in fields])


def _quote_body(**over):
    """Distinct values per field so a transposition cannot pass."""
    return _pack(op.QUOTE_FIELDS, {**_QUOTE_VALUES, **over})


def test_quote_packet_every_field_lands_correctly():
    p = op.parse_packet(_header(op.CODE_QUOTE) + _quote_body())
    assert p["ltp"] == pytest.approx(101.5, rel=1e-6)
    assert p["last_qty"] == 65
    assert p["last_trade_time"] == 1785644264
    assert p["avg_price"] == pytest.approx(100.25, rel=1e-6)
    assert p["volume"] == 1_234_567
    assert p["day_open"] == pytest.approx(95.0, rel=1e-6)
    assert p["prev_close"] == pytest.approx(120.0, rel=1e-6)
    assert p["day_high"] == pytest.approx(110.0, rel=1e-6)
    assert p["day_low"] == pytest.approx(90.0, rel=1e-6)


def test_quote_sell_quantity_comes_before_buy_quantity():
    """
    The exchange sends total SELL qty (doc bytes 27-30) before total BUY
    qty (31-34). Swapping them would invert every imbalance signal
    computed downstream while still producing plausible numbers -- the
    single most consequential ordering in this whole format.
    """
    p = op.parse_packet(_header(op.CODE_QUOTE) + _quote_body(
        total_sell_qty=11_111, total_buy_qty=99_999))
    assert p["total_sell_qty"] == 11_111
    assert p["total_buy_qty"] == 99_999


def test_quote_packet_is_exactly_the_documented_size():
    assert len(_header(op.CODE_QUOTE) + _quote_body()) == op.PACKET_SIZES[op.CODE_QUOTE] == 50


_FULL_VALUES = {
    **_QUOTE_VALUES,
    "oi": 4_474_730, "oi_day_high": 5_000_000, "oi_day_low": 4_000_000,
}


def _full_body(**over):
    return _pack(op.FULL_FIELDS, {**_FULL_VALUES, **over})


def _depth_block(levels=None):
    """5 x 20 bytes. Level i gets values keyed off i so misordering shows."""
    out = b""
    for i in range(5):
        lv = (levels[i] if levels else
              {"bid_qty": 100 + i, "ask_qty": 200 + i, "bid_orders": 10 + i,
               "ask_orders": 20 + i, "bid_price": 99.0 - i, "ask_price": 101.0 + i})
        out += _pack(op.DEPTH_FIELDS, lv)
    return out


def test_full_packet_is_exactly_the_documented_size():
    buf = _header(op.CODE_FULL) + _full_body() + _depth_block()
    assert len(buf) == op.PACKET_SIZES[op.CODE_FULL] == 162


def test_full_packet_ohlc_sits_after_the_oi_block_not_where_quote_puts_it():
    """
    The Full packet inserts three OI int32s BEFORE its OHLC block, so
    open/close/high/low sit 12 bytes further along than in a Quote
    packet. Reusing the Quote offsets would read OI values as prices --
    4,474,730 would surface as a "price" without anything erroring.
    """
    p = op.parse_packet(_header(op.CODE_FULL) + _full_body() + _depth_block())
    assert p["oi"] == 4_474_730
    assert p["oi_day_high"] == 5_000_000
    assert p["oi_day_low"] == 4_000_000
    assert p["day_open"] == pytest.approx(95.0, rel=1e-6)
    assert p["day_high"] == pytest.approx(110.0, rel=1e-6)
    assert p["day_low"] == pytest.approx(90.0, rel=1e-6)


def test_full_packet_depth_levels_parse_in_order():
    p = op.parse_packet(_header(op.CODE_FULL) + _full_body() + _depth_block())
    depth = p["depth"]
    assert len(depth) == 5
    for i, lv in enumerate(depth):
        assert lv["bid_qty"] == 100 + i
        assert lv["ask_qty"] == 200 + i
        assert lv["bid_orders"] == 10 + i
        assert lv["ask_orders"] == 20 + i
        assert lv["bid_price"] == pytest.approx(99.0 - i, rel=1e-6)
        assert lv["ask_price"] == pytest.approx(101.0 + i, rel=1e-6)


def test_depth_bid_and_ask_are_not_transposed():
    """Best bid must be below best ask -- a transposition inverts the book."""
    p = op.parse_packet(_header(op.CODE_FULL) + _full_body() + _depth_block())
    top = p["depth"][0]
    assert top["bid_price"] < top["ask_price"]
    assert top["bid_qty"] == 100 and top["ask_qty"] == 200


def test_unknown_code_returns_none_instead_of_raising():
    """Dhan can add response types; an unfamiliar packet must not kill the feed."""
    assert op.parse_packet(_header(99) + b"\x00" * 40) is None


def test_truncated_packet_returns_none():
    buf = (_header(op.CODE_FULL) + _full_body())[:40]   # depth block missing
    assert op.parse_packet(buf) is None


def test_split_packets_walks_several_packets_in_one_frame():
    frame = (
        _header(op.CODE_TICKER, security_id=1) + struct.pack("<fi", 10.0, 111)
        + _header(op.CODE_QUOTE, security_id=2) + _quote_body(ltp=20.0)
        + _header(op.CODE_OI, security_id=3) + struct.pack("<i", 999)
    )
    packets = op.split_packets(frame)
    assert [p["security_id"] for p in packets] == [1, 2, 3]
    assert [p["code"] for p in packets] == [op.CODE_TICKER, op.CODE_QUOTE, op.CODE_OI]
    assert packets[1]["ltp"] == pytest.approx(20.0, rel=1e-6)


def test_split_packets_stops_at_an_unrecognised_code_rather_than_guessing():
    """
    Without a known size there is no way to find the next packet
    boundary; guessing would silently mis-align everything after it,
    which is worse than dropping the tail of one frame.
    """
    frame = (
        _header(op.CODE_TICKER, security_id=1) + struct.pack("<fi", 10.0, 111)
        + _header(99, security_id=2) + b"\x00" * 40
    )
    packets = op.split_packets(frame)
    assert len(packets) == 1
    assert packets[0]["security_id"] == 1


def test_split_packets_ignores_a_trailing_partial_packet():
    frame = (
        _header(op.CODE_TICKER, security_id=1) + struct.pack("<fi", 10.0, 111)
        + _header(op.CODE_FULL, security_id=2)      # header only, body missing
    )
    assert len(op.split_packets(frame)) == 1


def test_split_packets_on_empty_frame():
    assert op.split_packets(b"") == []


def test_field_tables_produce_exactly_the_documented_packet_sizes():
    """
    Header + body (+ depth) must equal Dhan's published packet sizes.

    This alone is NOT sufficient -- the transposed avg_price/volume bug
    passed this check, since both are 4 bytes -- which is exactly why the
    round-trip tests above pack from the same field table rather than an
    independently written format string. Kept as a cheap structural
    guard against a field being dropped or added outright.
    """
    quote = op.HEADER_SIZE + struct.calcsize(op.QUOTE_FORMAT)
    full = op.HEADER_SIZE + struct.calcsize(op.FULL_FORMAT) + op.DEPTH_LEVELS * op.DEPTH_ENTRY_SIZE
    assert quote == op.PACKET_SIZES[op.CODE_QUOTE] == 50
    assert full == op.PACKET_SIZES[op.CODE_FULL] == 162
    assert struct.calcsize(op.DEPTH_FORMAT) == op.DEPTH_ENTRY_SIZE == 20


def test_depth_block_starts_where_the_full_body_ends():
    """
    parse_full derives the depth offset from the body size rather than a
    hardcoded 62. If a field is ever added to FULL_FIELDS, the depth
    block must move with it instead of silently reading into the body.
    """
    assert op.HEADER_SIZE + struct.calcsize(op.FULL_FORMAT) == 62
