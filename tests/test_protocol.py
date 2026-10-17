"""Tests for groundstation.protocol (framing, options, checksum, FrameReader)."""
import random
import struct

import pytest

from groundstation.protocol import (
    DEFAULT_PORTS, HEADER_BYTES, MAX_MESSAGE_BYTES, MIN_MESSAGE_BYTES,
    POSTAMBLE_BYTES, POSTAMBLE_WORD, START_BYTES, START_WORD, FrameReader,
    Message, ProtocolError, ProtocolOptions, StreamError, build_message,
    checksum_word, pad_to_words, parse_message, to_i32, to_u32)

MONITORING_REQUEST = bytes.fromhex("499602d20000001c00000000000000000000000000000007b669fd2e")
TM_START = bytes.fromhex("499602d20000004000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000b669fd2e")  # noqa: E501
BD_WHOLE = bytes.fromhex("499602d20000002c000000000000001e000000030000000900000005deadbeef01000000205240b6b669fd2e")  # noqa: E501
BD_EXCL = bytes.fromhex("499602d20000002c000000000000001e000000030000000900000005deadbeef01000000d6bc3de4b669fd2e")  # noqa: E501
BD_PAYLOAD = bytes.fromhex("499602d20000002c000000000000001e000000030000000900000005deadbeef01000000205240e2b669fd2e")  # noqa: E501


def pack(*words):
    """Independent big-endian packer (does not use the code under test)."""
    return struct.pack(f">{len(words)}I", *[w & 0xFFFFFFFF for w in words])


def test_constants():
    assert START_BYTES.hex() == "499602d2"
    assert POSTAMBLE_BYTES.hex() == "b669fd2e"
    assert START_WORD == 1234567890 and POSTAMBLE_WORD == -1234567890
    assert (HEADER_BYTES, MIN_MESSAGE_BYTES, MAX_MESSAGE_BYTES) == (12, 16, 1_048_576)
    assert DEFAULT_PORTS == {"control": 3101, "monitoring": 3100,
                             "telecommand": 3120, "telemetry": 3070}


def test_twos_complement_helpers():
    assert to_u32(-1) == 0xFFFFFFFF and to_u32(5) == 5
    assert to_i32(0xFFFFFFFF) == -1 and to_i32(0x80000000) == -(2 ** 31)
    assert to_i32(0x7FFFFFFF) == 2 ** 31 - 1


def test_build_message_matches_vector():
    assert build_message(0, [0, 0, 7]) == MONITORING_REQUEST


def test_build_message_round_trip():
    raw = build_message(5, [1, -1, 2 ** 32 - 1, -(2 ** 31)])
    msg = parse_message(raw)
    assert msg.flow_id == 5
    assert msg.body == (1, -1, -1, -(2 ** 31))
    assert msg.size == len(raw) == 4 * (3 + 4 + 1)
    assert msg.raw == raw
    assert msg.body_bytes() == pack(1, -1, -1, -(2 ** 31))


def test_empty_body_round_trip():
    raw = build_message(0, [])
    assert len(raw) == 16
    assert parse_message(raw).body == ()


@pytest.mark.parametrize("bad", [2 ** 32, -(2 ** 31) - 1])
def test_build_message_rejects_out_of_range_words(bad):
    with pytest.raises(ProtocolError):
        build_message(0, [bad])


def test_pad_to_words():
    assert pad_to_words(b"") == []
    assert pad_to_words(bytes.fromhex("deadbeef01")) == [to_i32(0xDEADBEEF), 0x01000000]
    assert pad_to_words(b"\x00\x00\x00\x01") == [1]


@pytest.mark.parametrize("data, text", [
    (b"\x00" * 12, "too short"),
    (MONITORING_REQUEST + b"\x00", "multiple of 4"),
    (pack(1, 28, 0, 0, 0, 7, POSTAMBLE_WORD), "start"),
    (pack(START_WORD, 32, 0, 0, 0, 7, POSTAMBLE_WORD), "size"),
    (pack(START_WORD, 28, 0, 0, 0, 7, 99), "postamble"),
])
def test_parse_message_errors(data, text):
    with pytest.raises(ProtocolError, match=text):
        parse_message(data)


def test_options_defaults_and_validation():
    opts = ProtocolOptions()
    assert opts.cortex_byte0_is_msb is True
    assert opts.checksum_mode == "whole_packet"
    assert (opts.control_flow_id, opts.monitoring_flow_id,
            opts.telecommand_flow_id, opts.telemetry_flow_id) == (1, 0, 0, 0)
    assert opts.fec_in_frame is False
    with pytest.raises(ValueError):
        ProtocolOptions(checksum_mode="bogus")
    for field in ("control_flow_id", "monitoring_flow_id",
                  "telecommand_flow_id", "telemetry_flow_id"):
        for bad in (-1, 2 ** 32, "1", 1.5, None):
            with pytest.raises(ValueError):
                ProtocolOptions(**{field: bad})
    assert ProtocolOptions(telemetry_flow_id=0xFFFFFFFF).telemetry_flow_id == 0xFFFFFFFF


def test_checksum_modes_on_bd_example():
    words = list(struct.unpack(">11I", BD_WHOLE))
    # Remove the checksum slot (the word before the postamble) from the packet.
    without = words[:-2] + words[-1:]
    expected = {"whole_packet": BD_WHOLE, "excluding_postamble": BD_EXCL,
                "payload_only": BD_PAYLOAD}
    for mode, vector in expected.items():
        word = checksum_word(without, mode)
        assert -(2 ** 31) <= word < 2 ** 31  # signed 32-bit
        assert to_u32(word) == struct.unpack(">11I", vector)[-2]
    # Sum properties, with the checksum slot put back in.
    full = without[:-1] + [checksum_word(without, "whole_packet")] + without[-1:]
    assert sum(full) % 2 ** 32 == 0
    full = without[:-1] + [checksum_word(without, "excluding_postamble")] + without[-1:]
    assert sum(full[:-1]) % 2 ** 32 == 0
    full = without[:-1] + [checksum_word(without, "payload_only")] + without[-1:]
    assert sum(full[3:-1]) % 2 ** 32 == 0
    with pytest.raises(ValueError):
        checksum_word(without, "bogus")


# ------------------------------------------------------------ FrameReader
def messages_of(events):
    return [e.raw for e in events if isinstance(e, Message)]


STREAM = MONITORING_REQUEST + b"\x00\x01\x02" + TM_START + MONITORING_REQUEST


@pytest.mark.parametrize("step", [1, 3, 7, len(STREAM)])
def test_framereader_stream_in_chunks(step):
    reader = FrameReader()
    events = []
    for i in range(0, len(STREAM), step):
        events += reader.feed(STREAM[i:i + step])
    assert messages_of(events) == [MONITORING_REQUEST, TM_START, MONITORING_REQUEST]
    assert reader.discarded_total == 3


def test_framereader_bad_postamble_then_good():
    bad = pack(START_WORD, 28, 0, 0, 0, 7, 12345)  # wrong postamble word
    reader = FrameReader()
    events = reader.feed(bad + MONITORING_REQUEST)
    assert messages_of(events) == [MONITORING_REQUEST]
    assert any(isinstance(e, StreamError) and e.reason == "bad postamble" for e in events)
    assert reader.discarded_total > 0


@pytest.mark.parametrize("size", [7, 12, 30, 0, MAX_MESSAGE_BYTES + 4])
def test_framereader_invalid_size_then_good(size):
    bad = pack(START_WORD, size)
    reader = FrameReader()
    events = reader.feed(bad + MONITORING_REQUEST)
    assert messages_of(events) == [MONITORING_REQUEST]
    assert any(isinstance(e, StreamError) and e.reason == "invalid size" for e in events)


def test_framereader_two_messages_in_one_chunk():
    reader = FrameReader()
    events = reader.feed(MONITORING_REQUEST + TM_START)
    assert messages_of(events) == [MONITORING_REQUEST, TM_START]
    assert reader.discarded_total == 0


def test_framereader_message_split_across_chunks():
    reader = FrameReader()
    assert reader.feed(MONITORING_REQUEST[:10]) == []
    assert reader.feed(MONITORING_REQUEST[10:20]) == []
    assert messages_of(reader.feed(MONITORING_REQUEST[20:])) == [MONITORING_REQUEST]


def test_framereader_start_word_straddling_chunks():
    reader = FrameReader()
    reader.feed(b"\xff\xff" + START_BYTES[:2])  # 2 junk bytes, then half a start word
    events = reader.feed(START_BYTES[2:] + MONITORING_REQUEST[4:])
    assert messages_of(events) == [MONITORING_REQUEST]


def test_framereader_pure_garbage_keeps_buffer_small():
    reader = FrameReader()
    reader.feed(b"\x11" * 1000)
    assert reader.discarded_total == 997  # last 3 bytes are kept in case of a split start


def build_noisy_stream(seed):
    """20 random messages with 0..29 bytes of garbage (never containing a start word) between them."""
    rng = random.Random(seed)
    messages, parts, garbage_total = [], [], 0
    for i in range(20):
        body = [rng.randrange(0, 2 ** 32) for _ in range(rng.randrange(0, 11))]
        raw = build_message(rng.randrange(0, 5), body)
        messages.append(raw)
        parts.append(raw)
        if i < 19:  # garbage goes between messages only
            garbage = rng.randbytes(rng.randrange(0, 30))
            while START_BYTES in garbage:
                garbage = rng.randbytes(rng.randrange(0, 30))
            parts.append(garbage)
            garbage_total += len(garbage)
    return b"".join(parts), messages, garbage_total


def test_framereader_noise_recovery():
    stream, messages, garbage_total = build_noisy_stream(99)
    rng = random.Random(99)
    reader = FrameReader()
    events, pos = [], 0
    while pos < len(stream):
        step = rng.randrange(1, 41)  # random chunk of 1 to 40 bytes
        events += reader.feed(stream[pos:pos + step])
        pos += step
    assert messages_of(events) == messages
    assert garbage_total - 3 <= reader.discarded_total <= garbage_total


def test_framereader_chunking_is_deterministic():
    stream, messages, _ = build_noisy_stream(7)
    byte_by_byte, one_chunk = FrameReader(), FrameReader()
    events_a = []
    for i in range(len(stream)):
        events_a += byte_by_byte.feed(stream[i:i + 1])
    events_b = one_chunk.feed(stream)
    msgs_a = [e for e in events_a if isinstance(e, Message)]
    msgs_b = [e for e in events_b if isinstance(e, Message)]
    assert msgs_a == msgs_b
    assert messages_of(events_a) == messages


def test_framereader_stalled_message_and_resync():
    noise = struct.pack(">iI", START_WORD, 1_000_000)  # start word with a huge size
    reader = FrameReader()
    events = reader.feed(noise + MONITORING_REQUEST * 50)
    assert messages_of(events) == []
    assert reader.pending_size == 1_000_000
    events = reader.resync()
    assert messages_of(events) == [MONITORING_REQUEST] * 50
    assert reader.pending_size is None
    assert reader.discarded_total == 8  # 4 forced + 4 garbage
    assert events[0] == StreamError("resync forced", 4)


def test_framereader_resync_on_empty_and_garbage_buffers():
    reader = FrameReader()
    assert reader.resync() == []
    assert reader.pending_size is None
    reader.feed(b"\x11\x22")  # 2 bytes: kept in case they start a split start word
    before = reader.discarded_total
    assert reader.resync() == []
    assert reader.discarded_total == before == 0


def test_framereader_pending_size_cases():
    reader = FrameReader()
    assert reader.pending_size is None
    reader.feed(START_BYTES)  # start word but no size yet
    assert reader.pending_size is None
    reader.feed(struct.pack(">I", 28))
    assert reader.pending_size == 28
