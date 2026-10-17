"""Seeded fuzzing of every client message parser and of FrameReader.

Rule under test: for ANY bytes the only exception a parser may raise is ProtocolError; a documented
result (UnknownResponse, UnknownTmMessage, StreamError) is fine. Anything else is a parser bug.
The whole file runs in a few seconds and is fully deterministic (fixed seeds, printed on failure).
"""
import dataclasses
import random
import struct
import time

import pytest

from groundstation import cop1, cortex
from groundstation.guibase import summarise
from groundstation.protocol import (HEADER_BYTES, MAX_MESSAGE_BYTES, POSTAMBLE_BYTES, START_BYTES, FrameReader,
                                    Message, ProtocolError, ProtocolOptions, StreamError, build_message,
                                    parse_message, to_i32)
from groundstation.tab_telecommand import describe_reply

PARSERS = {
    "control": cop1.parse_control_response,
    "monitoring": cop1.parse_monitoring_response,
    "telecommand": cop1.parse_tc_response,
    "telemetry": cortex.parse_tm_message,
}
RANDOM_BODIES = 20000
MUTATIONS_PER_TEMPLATE = 500
# Each fuzz test takes about a second here. The bound below is a guard against a parser that hangs or
# turns quadratic, NOT a speed test: it is far above any machine speed or load, because a bound close to
# the real time would make these tests depend on how busy the machine is.
HANG_GUARD_SECONDS = 60.0
SPECIAL_WORDS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 13, 20, 30, 32, 36, 40, 50, 63, 64, 68, 255, 256, 1024, 65535,
                 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE, 0xFFFFFFFF, -1, -2, -128, -2 ** 31)
BODY_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 20, 40, 62, 63, 64, 65, 80)


def hexdump(data: bytes) -> str:
    return data.hex() if len(data) <= 96 else data[:96].hex() + f"...(+{len(data) - 96} bytes)"


def random_word(rng: random.Random) -> int:
    pick = rng.random()
    if pick < 0.45:
        return rng.choice(SPECIAL_WORDS)
    if pick < 0.6:
        return rng.randrange(0, 12)  # small values hit the op, flow and code branches
    return to_i32(rng.getrandbits(32))


def random_body(rng: random.Random) -> "list[int]":
    length = rng.choice(BODY_LENGTHS) if rng.random() < 0.8 else rng.randrange(0, 120)
    return [random_word(rng) for _ in range(length)]


def valid_templates() -> "list[tuple[str, bytes]]":
    """One well-formed server message per reply type (bodies laid out as the interface documents describe them)."""
    config, status = [0] * 40, [0] * 20
    frame_words = [0x01020304, 0x05060708, 0x090A0B0C]
    telemetry_body = [111, 222, 5, 0x12345678, 1, 0, 0, 12, 16, 1, 0, 0, 0, *frame_words]
    return [(name, build_message(flow, body)) for name, flow, body in [
        ("control ack", 1, [1, 0, 7, 0]), ("negative", 1, [-1]),
        ("monitoring report", 0, [0, 0, 9, *config, *status]),
        ("accept", 0, [0, 30, 1, 5, 0]), ("reject", 0, [0, 20, 1, 5, 1]), ("confirm", 0, [1, 20, 1, 5, 0]),
        ("alert", 0, [2, 0, 1, 5]), ("suspend", 0, [3, 0, 2, 0]),
        ("segment echo", 0, [4, 30, 5, 9, 4, 0x01020304]), ("cltu echo", 0, [5, 0, 5, 9, 1, 2, 3, 0xAABBCCDD]),
        ("additional info", 0, [6, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
        ("tm negative ack", 0, [1]), ("tm stop ack", 0, [0]),
        ("telemetry", 0, telemetry_body),
        ("telemetry odd length", 0, [111, 222, 5, 0, 1, 0, 0, 10, 16, 1, 0, 0, 0, *frame_words]),
        ("dummy", 0, [111, 222, 5, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0]),
    ]]


def run_all_parsers(msg: Message, context: str) -> None:
    """Every parser, then everything a GUI does with the result; only ProtocolError may escape."""
    for name, parser in PARSERS.items():
        try:
            result = parser(msg)
        except ProtocolError:
            continue
        except Exception as exc:  # noqa: BLE001 - this IS the bug we are looking for
            pytest.fail(f"{name} parser raised {type(exc).__name__}: {exc}; {context}; raw={hexdump(msg.raw)}")
        try:
            summarise(result)
            describe_reply(result)
            if isinstance(result, cortex.TelemetryMessage):
                for msb in (True, False):
                    cortex.interpret_frame_check(result, ProtocolOptions(cortex_byte0_is_msb=msb))
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"formatting {type(result).__name__} from the {name} parser raised "
                        f"{type(exc).__name__}: {exc}; {context}; raw={hexdump(msg.raw)}")


def test_random_bodies_never_escape_a_parser_as_anything_but_protocol_error():
    started = time.perf_counter()
    rng = random.Random(20260110)
    for i in range(RANDOM_BODIES):
        raw = build_message(rng.choice((0, 1, 2, 3, rng.getrandbits(32))), random_body(rng))
        run_all_parsers(parse_message(raw), f"random body #{i}, seed 20260110")
    assert time.perf_counter() - started < HANG_GUARD_SECONDS


def mutate(rng: random.Random, raw: bytes) -> bytes:
    kind = rng.randrange(6)
    data = bytearray(raw)
    if kind == 0:  # flip 1 to 3 random bits anywhere (header, body, postamble)
        for _ in range(rng.randrange(1, 4)):
            data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
    elif kind == 1:  # truncate
        del data[rng.randrange(len(data)):]
    elif kind == 2:  # extend with random bytes
        data += rng.randbytes(rng.randrange(1, 40))
    elif kind == 3:  # a wrong size field, keeping the bytes
        data[4:8] = struct.pack(">I", rng.choice((0, 3, 12, 16, 20, 31, 36, len(raw) - 4, len(raw) + 4,
                                                  len(raw) + 1, 2 ** 20, 2 ** 20 + 4, 2 ** 31, 0xFFFFFFFF,
                                                  rng.getrandbits(32))))
    elif kind == 4:  # one random body word replaced
        if len(data) >= 20:
            at = 12 + 4 * rng.randrange((len(data) - 16) // 4)
            data[at:at + 4] = struct.pack(">I", rng.getrandbits(32) & 0xFFFFFFFF)
    else:  # duplicate or drop a whole word in the body (the size word is then wrong)
        if len(data) >= 20:
            at = 12 + 4 * rng.randrange((len(data) - 16) // 4)
            if rng.random() < 0.5:
                data[at:at + 4] = b""
            else:
                data[at:at] = data[at:at + 4]
    return bytes(data)


def test_mutations_of_valid_messages_only_ever_give_documented_results():
    started = time.perf_counter()
    rng = random.Random(77)
    counts = {"framed": 0, "dropped": 0}
    for name, template in valid_templates():
        for i in range(MUTATIONS_PER_TEMPLATE):
            raw = mutate(rng, template)
            context = f"mutation #{i} of '{name}'"
            try:
                msg = parse_message(raw)
            except ProtocolError:
                pass
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"parse_message raised {type(exc).__name__}: {exc}; {context}; raw={hexdump(raw)}")
            else:
                run_all_parsers(msg, context)
            reader = FrameReader()
            for event in reader.feed(raw):  # the same bytes through the stream reader
                assert isinstance(event, (Message, StreamError)), (context, event)
                if isinstance(event, Message):
                    counts["framed"] += 1
                    run_all_parsers(event, context)
                else:
                    counts["dropped"] += 1
    assert counts["framed"] > 0 and counts["dropped"] > 0  # the corpus really reached both outcomes
    assert time.perf_counter() - started < HANG_GUARD_SECONDS


def test_the_unmutated_templates_parse_to_real_results():
    """Guards the fuzz corpus itself: if the templates drifted from the parsers, the fuzz would be empty."""
    kinds = set()
    for name, raw in valid_templates():
        msg = parse_message(raw)
        for parser in PARSERS.values():
            result = parser(msg)
            if not isinstance(result, (cop1.UnknownResponse, cortex.UnknownTmMessage)):
                kinds.add(type(result).__name__)
    assert {"ControlAck", "NegativeResponse", "MonitoringReport", "AcceptReject", "ConfirmResponse",
            "AlertIndication", "SuspendIndication", "SegmentEcho", "CltuEcho", "AdditionalInfo",
            "TelemetryNegativeAck", "TelemetryStopAck", "TelemetryMessage", "DummyTelemetry"} <= kinds


def feed_in_random_chunks(rng: random.Random, reader: FrameReader, data: bytes) -> "list":
    events, at = [], 0
    while at < len(data):
        step = rng.choice((1, 2, 3, 4, 5, 7, 16, 64, 500))
        events += reader.feed(data[at:at + step])
        at += step
    return events


def test_framereader_accounts_for_every_byte_whatever_it_is_fed():
    """fed == discarded + delivered + still buffered; only Message and StreamError ever come out."""
    started = time.perf_counter()
    rng = random.Random(4242)
    templates = [raw for _, raw in valid_templates()]
    for stream_no in range(1500):
        parts = []
        for _ in range(rng.randrange(1, 12)):
            pick = rng.random()
            if pick < 0.4:
                parts.append(rng.choice(templates))
            elif pick < 0.6:
                parts.append(mutate(rng, rng.choice(templates)))
            elif pick < 0.8:
                parts.append(rng.randbytes(rng.randrange(0, 80)))
            elif pick < 0.9:
                parts.append(START_BYTES + struct.pack(">I", rng.choice((0, 16, 20, 2 ** 20, 2 ** 20 + 4, 2 ** 31,
                                                                          0xFFFFFFFF, rng.getrandbits(32)))))
            else:
                parts.append(POSTAMBLE_BYTES * rng.randrange(1, 4))
        stream = b"".join(parts)
        reader = FrameReader()
        events = feed_in_random_chunks(rng, reader, stream)
        delivered = 0
        for event in events:
            assert isinstance(event, (Message, StreamError)), (stream_no, event)
            if isinstance(event, Message):
                delivered += event.size
                run_all_parsers(event, f"stream #{stream_no}")
            else:
                assert event.discarded > 0
        assert len(stream) == reader.discarded_total + delivered + len(reader._buf), (stream_no, hexdump(stream))
        assert len(reader._buf) <= MAX_MESSAGE_BYTES  # the cap holds: a hostile size cannot grow the buffer
        pending = reader.pending_size
        assert pending is None or HEADER_BYTES + 4 <= pending <= MAX_MESSAGE_BYTES
        reader.resync()  # must never raise either
    assert time.perf_counter() - started < HANG_GUARD_SECONDS


def test_chunking_never_changes_what_the_reader_delivers():
    rng = random.Random(5)
    templates = [raw for _, raw in valid_templates()]
    for _ in range(300):
        stream = b"".join(rng.choice((rng.choice(templates), rng.randbytes(rng.randrange(0, 30))))
                          for _ in range(rng.randrange(1, 10)))
        whole = [e for e in FrameReader().feed(stream) if isinstance(e, Message)]
        pieces = [e for e in feed_in_random_chunks(rng, FrameReader(), stream) if isinstance(e, Message)]
        assert whole == pieces, hexdump(stream)


def test_a_hostile_size_word_never_stalls_the_reader_for_more_than_its_cap():
    """A size just above the cap is skipped at once; a size at the cap waits (that is the stall case)."""
    over = START_BYTES + struct.pack(">I", MAX_MESSAGE_BYTES + 4)
    reader = FrameReader()
    events = reader.feed(over + build_message(0, [1]))
    assert any(isinstance(e, StreamError) and e.reason == "invalid size" for e in events)
    assert [e for e in events if isinstance(e, Message)]  # the good message behind it still arrives
    waiting = FrameReader()
    waiting.feed(START_BYTES + struct.pack(">I", MAX_MESSAGE_BYTES))
    assert waiting.pending_size == MAX_MESSAGE_BYTES
    assert dataclasses.is_dataclass(StreamError)
