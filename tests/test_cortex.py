"""Tests for groundstation.cortex (TM requests, TM message parsing, frame check)."""
import struct

import pytest

from groundstation import cortex
from groundstation.cortex import (
    DummyTelemetry, FrameCheck, TelemetryMessage, TelemetryNegativeAck,
    TelemetryStopAck, UnknownTmMessage)
from groundstation.protocol import (ProtocolError, ProtocolOptions,
                                    parse_message)

START, POST = 1234567890, -1234567890
OPTS = ProtocolOptions()


def pack(*words):
    """Independent big-endian packer (does not use the code under test)."""
    return struct.pack(f">{len(words)}I", *[w & 0xFFFFFFFF for w in words])


def server_message(body, flow=0):
    return parse_message(pack(START, 4 * (len(body) + 4), flow, *body, POST))


# ------------------------------------------------------------ requests
def test_tm_start_vector():
    assert cortex.build_tm_request(0, 0, 0, OPTS) == bytes.fromhex(
        "499602d2000000400000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000b669fd2e")


def test_tm_request_vector():
    raw = cortex.build_tm_request(2, 100, 1, OPTS, frame_mask=0xFFFF0000AABBCCDD,
                                  expected_value=0x1122334455667788)
    assert raw == bytes.fromhex(
        "499602d20000004000000000000000020000006400000001ffff0000aabbccdd"
        "11223344556677880000000000000000000000000000000000000000b669fd2e")
    assert len(raw) == 64


def test_tm_stop_vector():
    raw = cortex.build_stop_request(0, OPTS)
    assert raw == bytes.fromhex(
        "499602d20000004000000000000000000000000000000080000000000000000000000000"
        "000000000000000000000000000000000000000000000000b669fd2e")
    assert len(raw) == 64


def test_tm_request_uses_telemetry_flow_id_and_offline_words():
    opts = ProtocolOptions(telemetry_flow_id=9)
    words = struct.unpack(">16I", cortex.build_tm_request(
        1, 2, 5, opts, offline_words=(1, 2, 3, 4, 5)))
    assert words[2] == 9
    assert words[3:6] == (1, 2, 5)
    assert words[10:15] == (1, 2, 3, 4, 5)


@pytest.mark.parametrize("kwargs", [
    {"channel": 6}, {"channel": -1}, {"buffered_blocks": 1025}, {"buffered_blocks": -1},
    {"data_flow": 3}, {"data_flow": 7}, {"data_flow": 0x81},
    {"frame_mask": 2 ** 64}, {"frame_mask": -1},
    {"expected_value": 2 ** 64}, {"expected_value": -1},
    {"offline_words": (0, 0, 0, 0)}, {"offline_words": (0,) * 6},
])
def test_tm_request_validation(kwargs):
    args = {"channel": 0, "buffered_blocks": 0, "data_flow": 0}
    args.update(kwargs)
    with pytest.raises(ProtocolError):
        cortex.build_tm_request(args.pop("channel"), args.pop("buffered_blocks"),
                                args.pop("data_flow"), OPTS, **args)


def test_tm_request_boundaries_accepted():
    cortex.build_tm_request(5, 1024, 0x80, OPTS, frame_mask=2 ** 64 - 1,
                            expected_value=2 ** 64 - 1)
    for flow in (0, 1, 2, 4, 5, 6, 0x80):
        cortex.build_tm_request(0, 0, flow, OPTS)
    assert cortex.CHANNELS == "ABCDEF"


# ------------------------------------------------------------ parsing
@pytest.mark.parametrize("code, meaning", [
    (1, "Invalid syntax"), (2, "TM chain not mounted"), (3, "No matching OTT data")])
def test_negative_acks(code, meaning):
    msg = server_message([code])
    assert msg.size == 20
    assert cortex.parse_tm_message(msg) == TelemetryNegativeAck(code, meaning)


def test_unknown_ack_code_and_stop_ack():
    assert isinstance(cortex.parse_tm_message(server_message([9])), UnknownTmMessage)
    assert cortex.parse_tm_message(server_message([0])) == TelemetryStopAck()


def test_dummy_telemetry():
    # 68 bytes = 17 words: body is offsets 3..15 (13 words), frame length (offset 10) is 0.
    body = [11, 22, 5, 0, 3, 0, 0, 0, 16, 0, 0, 0, 0]
    msg = server_message(body)
    assert msg.size == 68
    result = cortex.parse_tm_message(msg)
    assert result == DummyTelemetry(time_tag_1=11, time_tag_2=22, sequence=5, sync_status=3)


TM_WORDS = [0x499602D2, 80, 0, 0, 0, 5, 0, 1, 0, 0, 10, 16, 0, 0, 0, 0,
            0x00010203, 0x04050607, 0x08090000, 0xB669FD2E]


def test_telemetry_message_vector():
    raw = struct.pack(">20I", *TM_WORDS)
    result = cortex.parse_tm_message(parse_message(raw))
    assert isinstance(result, TelemetryMessage)
    assert result.sequence == 5
    assert result.sync_status == 1
    assert result.frame == bytes(range(10))
    assert result.frame_length == 10
    assert result.sync_word_length == 16
    assert result.length_consistent is True
    assert (result.time_tag_1, result.time_tag_2) == (0, 0)


def test_unsigned_fields():
    words = list(TM_WORDS)
    words[3], words[4], words[5], words[6] = 0xFFFFFFFF, 0x80000000, 0xFFFFFFFE, 0xF0000000
    result = cortex.parse_tm_message(parse_message(struct.pack(">20I", *words)))
    assert result.time_tag_1 == 0xFFFFFFFF and result.time_tag_2 == 0x80000000
    assert result.sequence == 0xFFFFFFFE and result.frame_check_raw == 0xF0000000


def test_length_not_consistent_with_padding():
    # 5-byte frame needs 2 words, but 3 payload words are present.
    words = list(TM_WORDS)
    words[10] = 5
    result = cortex.parse_tm_message(parse_message(struct.pack(">20I", *words)))
    assert result.frame == bytes(range(5))
    assert result.length_consistent is False


def test_frame_length_exceeding_payload_raises():
    # Declared length 11 but only 2 payload words (8 bytes).
    words = [0x499602D2, 76, 0, 0, 0, 5, 0, 1, 0, 0, 11, 16, 0, 0, 0, 0,
             0x00010203, 0x04050607, 0xB669FD2E]
    words[1] = 4 * len(words)
    with pytest.raises(ProtocolError, match="frame length exceeds payload"):
        cortex.parse_tm_message(parse_message(struct.pack(f">{len(words)}I", *words)))


def test_declared_length_11_in_three_payload_words_is_valid():
    # 3 payload words are 12 bytes and a declared length of 11 pads to exactly 12.
    words = list(TM_WORDS)
    words[10] = 11
    result = cortex.parse_tm_message(parse_message(struct.pack(">20I", *words)))
    assert isinstance(result, TelemetryMessage)
    assert result.frame_length == 11
    assert result.frame == bytes(range(10)) + b"\x00"
    assert result.length_consistent is True


def test_declared_length_13_in_three_payload_words_raises():
    words = list(TM_WORDS)
    words[10] = 13
    with pytest.raises(ProtocolError, match="frame length exceeds payload"):
        cortex.parse_tm_message(parse_message(struct.pack(">20I", *words)))


def test_declared_length_0xffffffff_raises():
    # Read as unsigned this is about 4 billion bytes, far more than the payload.
    words = list(TM_WORDS)
    words[10] = 0xFFFFFFFF
    with pytest.raises(ProtocolError, match="frame length exceeds payload"):
        cortex.parse_tm_message(parse_message(struct.pack(">20I", *words)))


def test_other_sizes_are_unknown():
    assert isinstance(cortex.parse_tm_message(server_message([0, 0])), UnknownTmMessage)  # 24 bytes
    assert isinstance(cortex.parse_tm_message(server_message([])), UnknownTmMessage)  # 16 bytes


# ------------------------------------------------------------ frame check
def make_tm(sync_status, decoder_status, frame_check, transmitted_data):
    return TelemetryMessage(
        time_tag_1=0, time_tag_2=0, sequence=0, frame_check_raw=frame_check,
        sync_status=sync_status, bit_slip=0, tm_delay=0, frame_length=0,
        sync_word_length=0, decoder_status=decoder_status,
        transmitted_data=transmitted_data, frame=b"", length_consistent=True)


FRAME_CHECK_TABLE = [
    (1, 0x00, 0x00000000, 0, True, "not_significant", None),
    (0, 0x02, 0x00000001, 0, True, "not_significant", None),
    (1, 0x02, 0x00000000, 0, True, "ok", None),
    (1, 0x02, 0x00000001, 0, True, "error", None),
    (1, 0x02, 0x00000002, 0, True, "error", None),
    (1, 0x01, 0x00000000, 0, True, "ok", None),
    (1, 0x01, 0x01000000, 0, True, "error", None),
    (1, 0x01, 0x02000000, 0, True, "error", None),
    (1, 0x01, 0x00050000, 2, True, "ok", 5),
    (1, 0x01, 0x00050000, 0, True, "ok", None),
    (1, 0x01, 0x00000001, 0, False, "error", None),
    (1, 0x01, 0x00000002, 0, False, "error", None),
    (1, 0x01, 0x00000500, 2, False, "ok", 5),
    (1, 0x08, 0x00000000, 0, True, "ok", None),
    (1, 0x08, 0x02000000, 0, True, "error", None),
    (1, 0x08, 0x00000002, 0, False, "error", None),
    (1, 0x10, 0x00000000, 0, True, "ok", 0),
    (1, 0x10, 0x0000FFFF, 0, True, "error", 65535),
    (1, 0x10, 0x02000007, 0, True, "error", 7),
    (1, 0x10, 0x00070002, 0, False, "error", 7),
    (4, 0x01, 0x01000000, 0, True, "not_significant", None),
]


@pytest.mark.parametrize(
    "sync, decoder, check, trans, msb, status, corrected", FRAME_CHECK_TABLE)
def test_interpret_frame_check(sync, decoder, check, trans, msb, status, corrected):
    opts = ProtocolOptions(cortex_byte0_is_msb=msb)
    result = cortex.interpret_frame_check(make_tm(sync, decoder, check, trans), opts)
    assert isinstance(result, FrameCheck)
    assert result.status == status
    assert result.corrected_errors == corrected
    assert isinstance(result.reason, str) and result.reason


def test_frame_check_status_values_only():
    for sync, decoder, check, trans, msb, *_ in FRAME_CHECK_TABLE:
        opts = ProtocolOptions(cortex_byte0_is_msb=msb)
        status = cortex.interpret_frame_check(make_tm(sync, decoder, check, trans), opts).status
        assert status in ("not_significant", "ok", "error")
