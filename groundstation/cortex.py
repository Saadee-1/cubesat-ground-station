"""CORTEX telemetry request builders and message parsers.

Written from the CORTEX interface document.
"""
from dataclasses import dataclass
from typing import Union

from .protocol import (Message, ProtocolError, ProtocolOptions, build_message,
                       to_u32)

CHANNELS = "ABCDEF"  # channel index 0..5
DATA_FLOW_REALTIME_PERMANENT = 0
DATA_FLOW_REALTIME_SINGLE = 1
DATA_FLOW_REALTIME_DUMMY = 2
DATA_FLOW_OFFLINE_PERMANENT = 4
DATA_FLOW_OFFLINE_SINGLE = 5
DATA_FLOW_OFFLINE_DUMMY = 6
DATA_FLOW_STOP = 0x80
_DATA_FLOWS = (DATA_FLOW_REALTIME_PERMANENT, DATA_FLOW_REALTIME_SINGLE,
               DATA_FLOW_REALTIME_DUMMY, DATA_FLOW_OFFLINE_PERMANENT,
               DATA_FLOW_OFFLINE_SINGLE, DATA_FLOW_OFFLINE_DUMMY, DATA_FLOW_STOP)
NEG_ACK_MEANING = {1: "Invalid syntax", 2: "TM chain not mounted", 3: "No matching OTT data"}
_U64_MAX = 2 ** 64 - 1


def build_tm_request(channel: int, buffered_blocks: int, data_flow: int,
                     opts: ProtocolOptions, frame_mask: int = 0, expected_value: int = 0,
                     offline_words: "tuple[int, ...]" = (0, 0, 0, 0, 0)) -> bytes:
    """Telemetry request, 64 bytes, on the telemetry flow.

    Body (offsets 3 to 14): channel, buffered blocks, data flow, mask high and
    low 32 bits, expected value high and low 32 bits, then five offline words.
    """
    if not 0 <= channel <= 5:
        raise ProtocolError(f"channel must be 0..5, got {channel!r}")
    if not 0 <= buffered_blocks <= 1024:
        raise ProtocolError(f"buffered blocks must be 0..1024, got {buffered_blocks!r}")
    if data_flow not in _DATA_FLOWS:
        raise ProtocolError(f"unknown data flow {data_flow!r}")
    for name, value in (("frame mask", frame_mask), ("expected value", expected_value)):
        if not 0 <= value <= _U64_MAX:
            raise ProtocolError(f"{name} must be 0..2**64-1")
    if len(offline_words) != 5:
        raise ProtocolError("offline_words must contain exactly 5 words")
    body = [channel, buffered_blocks, data_flow,
            frame_mask >> 32, frame_mask & 0xFFFFFFFF,
            expected_value >> 32, expected_value & 0xFFFFFFFF, *offline_words]
    return build_message(opts.telemetry_flow_id, body)


def build_stop_request(channel: int, opts: ProtocolOptions) -> bytes:
    """Stop request: a TM request with 0 buffered blocks, data flow 0x80, rest zero."""
    return build_tm_request(channel, 0, DATA_FLOW_STOP, opts)


@dataclass(frozen=True)
class TelemetryNegativeAck:
    """Negative telemetry acknowledgement."""
    code: int
    meaning: str


@dataclass(frozen=True)
class TelemetryStopAck:
    """Stop telemetry acknowledgement (size 20, body 0)."""


@dataclass(frozen=True)
class TelemetryMessage:
    """Telemetry message."""
    time_tag_1: int
    time_tag_2: int
    sequence: int
    frame_check_raw: int
    sync_status: int
    bit_slip: int
    tm_delay: int
    frame_length: int
    sync_word_length: int
    decoder_status: int
    transmitted_data: int
    frame: bytes
    length_consistent: bool


@dataclass(frozen=True)
class DummyTelemetry:
    """Dummy telemetry message (size 68, frame length 0)."""
    time_tag_1: int
    time_tag_2: int
    sequence: int
    sync_status: int


@dataclass(frozen=True)
class UnknownTmMessage:
    raw: bytes
    reason: str


def parse_tm_message(msg: Message):
    """Parse a server telemetry message.

    body[0] is offset 3. Size 20 is an acknowledgement; size >= 68 carries
    telemetry (offset 10 is the frame length, frame data starts at offset 16).
    """
    body = msg.body
    if msg.size == 20:
        if body[0] == 0:
            return TelemetryStopAck()
        if body[0] in NEG_ACK_MEANING:
            return TelemetryNegativeAck(body[0], NEG_ACK_MEANING[body[0]])
        return UnknownTmMessage(msg.raw, f"unknown acknowledgement code {body[0]}")
    if msg.size >= 68:
        frame_length = to_u32(body[7])  # offset 10, read as unsigned
        if frame_length == 0:
            # Dummy or end-of-data layout (size 68): only the first fields matter.
            return DummyTelemetry(time_tag_1=to_u32(body[0]), time_tag_2=to_u32(body[1]),
                                  sequence=to_u32(body[2]), sync_status=body[4])
        # Offset 16 up to the word before the postamble.
        payload = b"".join(to_u32(w).to_bytes(4, "big") for w in body[13:])
        if frame_length > len(payload):
            raise ProtocolError("frame length exceeds payload")
        padded = -(-frame_length // 4) * 4  # frame_length rounded up to a multiple of 4
        return TelemetryMessage(
            time_tag_1=to_u32(body[0]), time_tag_2=to_u32(body[1]),
            sequence=to_u32(body[2]), frame_check_raw=to_u32(body[3]),
            sync_status=body[4], bit_slip=body[5], tm_delay=body[6],
            frame_length=frame_length, sync_word_length=body[8],
            decoder_status=body[9], transmitted_data=body[10],
            frame=payload[:frame_length], length_consistent=(len(payload) == padded))
    return UnknownTmMessage(msg.raw, f"unexpected telemetry message size {msg.size}")


@dataclass(frozen=True)
class FrameCheck:
    """Result of interpreting the frame check word."""
    status: str  # "not_significant", "ok" or "error"
    reason: str
    corrected_errors: "Union[int, None]"


def interpret_frame_check(m: TelemetryMessage, opts: ProtocolOptions) -> FrameCheck:
    """Interpret frame_check_raw (offset 6 of the telemetry message) by decoder type.

    Flag bits in decoder_status and transmitted_data are always
    least-significant-bit-first (1 << n); only the byte order of the check word
    depends on opts.cortex_byte0_is_msb.
    """
    if m.sync_status in (0, 4) or m.decoder_status == 0:
        return FrameCheck("not_significant", "no frame synchronisation or no decoder", None)
    w = m.frame_check_raw

    def byte(k: int) -> int:
        """Byte k of the check word; byte 0 is the MSB when cortex_byte0_is_msb."""
        shift = 24 - 8 * k if opts.cortex_byte0_is_msb else 8 * k
        return (w >> shift) & 0xFF

    if m.decoder_status & 0x01:  # Reed-Solomon
        corrected = byte(1) if (m.transmitted_data & 0x02) else None
        b0 = byte(0)
        if b0 & 0x01:
            return FrameCheck("error", "capacity exceeded", corrected)
        if b0 & 0x02:
            return FrameCheck("error", "frame check error", corrected)
        return FrameCheck("ok", "Reed-Solomon decoding ok", corrected)
    if m.decoder_status & 0x08:  # Turbo
        if byte(0) == 0:
            return FrameCheck("ok", "Turbo decoding ok", None)
        return FrameCheck("error", "Turbo decoding error", None)
    if m.decoder_status & 0x10:  # LDPC (Low-Density Parity-Check)
        count = (w & 0xFFFF) if opts.cortex_byte0_is_msb else ((w >> 16) & 0xFFFF)
        if byte(0) != 0 or count == 0xFFFF:
            return FrameCheck("error", "LDPC decoding error", count)
        return FrameCheck("ok", "LDPC decoding ok", count)
    # CRC or checksum only: the word must be zero.
    if w == 0:
        return FrameCheck("ok", "CRC or checksum ok", None)
    return FrameCheck("error", "CRC or checksum error", None)
