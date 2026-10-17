"""COP-1 request builders and response parsers.

Written from the COP-1 interface document.
COP-1 messages always use most-significant-byte-first (XDR); the option
ProtocolOptions.cortex_byte0_is_msb does not apply here.
"""
from dataclasses import dataclass
from typing import Optional, Union

from .protocol import (Message, ProtocolError, ProtocolOptions, build_message,
                       checksum_word, pad_to_words, to_u32,
                       START_WORD, POSTAMBLE_WORD)

OP_MONITORING = 0
OP_CONTROL = 1
OP_AD = 20
OP_BD = 30
OP_CLTU = 40
OP_FRAME = 50
OP_NOP = 13
OP_SUBSCRIBE = 14

# Operation codes and message layout of directives.
DIRECTIVES = {
    2: "Initiate AD (no CLCW check)", 3: "Initiate AD (CLCW check)",
    4: "Initiate AD (unlock)", 5: "Initiate AD (set V(R))",
    6: "Terminate AD", 7: "Resume AD", 8: "Set V(S)",
    9: "Set FOP sliding window width", 10: "Set T1 initial",
    11: "Set transmission limit", 12: "Set timeout type",
}

# Size limits in bytes of the AD segment, BD segment, CLTU and Transfer Frame.
SEGMENT_MAX = 1019
SEGMENT_MAX_FEC = 1017
CLTU_MAX = 1284
FRAME_MIN = 6
FRAME_MAX = 1024
CODEBLOCK_LENGTHS = (0, 5, 6, 7, 8)
VCID_MAX = 63
PARAM_ID_MAX = 39
_U32_MAX = 0xFFFFFFFF

# Configuration parameters 0 to 28.
CONFIG_PARAM_NAMES = {
    0: "Spacecraft identifier", 1: "Codeblock length", 2: "Channel auto-switch",
    3: "CORTEX_CRT port number/address", 4: "CORTEX_CRT IP address", 5: "Reserved",
    6: "FEC present in telemetry data", 7: "Programming perturbation authorization",
    8: "Check out facilities", 9: "Satellite telecommand data flow identifier",
    10: "Monitoring flow identifier", 11: "Check operational parameters",
    12: "Check VCID equivalence in CLCW data",
    13: "FOP1 Virtual Channel ID", 14: "FOP2 Virtual Channel ID",
    15: "FOP3 Virtual Channel ID", 16: "FOP4 Virtual Channel ID",
    17: "FOP1 Virtual Channel ID equivalence", 18: "FOP2 Virtual Channel ID equivalence",
    19: "FOP3 Virtual Channel ID equivalence", 20: "FOP4 Virtual Channel ID equivalence",
    21: "CLCW offset in TM frame", 22: "OCF offset in TM frame",
    23: "Randomize the Transfer Frame", 24: "FEC present in Transfer Frame",
    25: "FOP1 repetition parameter", 26: "FOP2 repetition parameter",
    27: "FOP3 repetition parameter", 28: "FOP4 repetition parameter",
}
# Status parameters.
STATUS_PARAM_NAMES = {
    0: "Telecommand global status", 1: "Number of programming perturbation",
    2: "Channel auto switch occurs", 7: "COP1 version and compatibility options",
}
# Bits of the telecommand global status word.
STATUS_ERROR_BITS = {0x01: "SOCKET", 0x02: "CLIENT", 0x04: "HOST", 0x08: "RESOURCES",
                     0x10: "FOP", 0x20: "TIME", 0x40: "CLCW", 0x80: "CODAGE"}

# Alert codes.
_ALERT_NAMES = {1: "limit", 2: "lockout", 3: "sync", 4: "NN(R)",
                5: "CLCW", 6: "LLIF", 7: "term", 8: "not applicable"}


# ---------------------------------------------------------------- helpers
def encode_ip(address: str) -> int:
    """Dotted quad to a 32-bit word, e.g. 192.52.109.15 -> 0xC0346D0F."""
    parts = address.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ProtocolError(f"bad IPv4 address {address!r}")
    value = 0
    for p in parts:
        value = (value << 8) | int(p)
    return value


def decode_ip(word: int) -> str:
    """Inverse of encode_ip."""
    w = to_u32(word)
    return ".".join(str((w >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def encode_tm_port(tmu: int, port: int) -> int:
    """TM port word: TMU number in the upper half, port in the lower.

    Assumption: the interface document draws the TMU number as the upper 16 bits.
    """
    if not 0 <= port <= 0xFFFF or not 0 <= tmu <= 0xFFFF:
        raise ProtocolError("tmu and port must be 0..0xFFFF")
    return (tmu << 16) | port


def decode_tm_port(word: int) -> "tuple[int, int]":
    """Inverse of encode_tm_port; returns (tmu, port)."""
    w = to_u32(word)
    return (w >> 16, w & 0xFFFF)


# ---------------------------------------------------------------- builders
def _check_tag(tag: int) -> None:
    if not isinstance(tag, int) or isinstance(tag, bool) or not 0 <= tag <= _U32_MAX:
        raise ProtocolError(f"tag must be an unsigned 32-bit integer, got {tag!r}")


def _check_vcid(vcid: int) -> None:
    if not isinstance(vcid, int) or isinstance(vcid, bool) or not 0 <= vcid <= VCID_MAX:
        raise ProtocolError(f"VCID must be 0..{VCID_MAX}, got {vcid!r}")


def build_control_request(tag: int, params: "dict[int, int]", opts: ProtocolOptions) -> bytes:
    """Control request: body [1, 0, tag, count, id1, val1, ...] on the control flow."""
    _check_tag(tag)
    body = [OP_CONTROL, 0, tag, len(params)]
    for pid, value in params.items():
        if not isinstance(pid, int) or not 0 <= pid <= PARAM_ID_MAX:
            raise ProtocolError(f"parameter id must be 0..{PARAM_ID_MAX}, got {pid!r}")
        body += [pid, value]
    return build_message(opts.control_flow_id, body)


def build_monitoring_request(tag: int, opts: ProtocolOptions) -> bytes:
    """Monitoring request: body [0, 0, tag] on the monitoring flow."""
    _check_tag(tag)
    return build_message(opts.monitoring_flow_id, [OP_MONITORING, 0, tag])


def _data_request(op: int, field4: int, tag: int, data: bytes,
                  opts: ProtocolOptions) -> bytes:
    """Shared layout of the AD, BD, CLTU and Transfer Frame requests, with the trailing checksum word."""
    _check_tag(tag)
    body = [op, field4, tag, len(data), *pad_to_words(bytes(data))]
    # Checksum is computed over the packet with its own slot removed.
    packet = [START_WORD, 4 * (3 + len(body) + 1 + 1), opts.telecommand_flow_id,
              *body, POSTAMBLE_WORD]
    return build_message(opts.telecommand_flow_id,
                         [*body, checksum_word(packet, opts.checksum_mode)])


def _segment_limit(opts: ProtocolOptions) -> int:
    return SEGMENT_MAX_FEC if opts.fec_in_frame else SEGMENT_MAX


def _as_bytes(data: "Union[bytes, bytearray, memoryview]") -> bytes:
    """Accept bytes, bytearray or memoryview and return bytes; reject anything else (e.g. str)."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise ProtocolError(f"data must be bytes, bytearray or memoryview, got {type(data).__name__}")


def _check_length(name: str, data: bytes, low: int, high: int) -> None:
    if not low <= len(data) <= high:
        raise ProtocolError(f"{name} length must be {low}..{high} bytes, got {len(data)}")


def build_bd_segment(vcid: int, tag: int, data: bytes, opts: ProtocolOptions,
                     validate: bool = True) -> bytes:
    """BD segment (op 30). validate=False skips the VCID and length limits."""
    data = _as_bytes(data)
    if validate:
        _check_vcid(vcid)
        _check_length("BD segment", data, 1, _segment_limit(opts))
    return _data_request(OP_BD, vcid, tag, data, opts)


def build_ad_segment(vcid: int, tag: int, data: bytes, opts: ProtocolOptions,
                     validate: bool = True) -> bytes:
    """AD segment (op 20). validate=False skips the VCID and length limits."""
    data = _as_bytes(data)
    if validate:
        _check_vcid(vcid)
        _check_length("AD segment", data, 1, _segment_limit(opts))
    return _data_request(OP_AD, vcid, tag, data, opts)


def build_cltu(vcid: int, tag: int, data: bytes, opts: ProtocolOptions,
               validate: bool = True) -> bytes:
    """CLTU (op 40). validate=False skips the VCID and length limits."""
    data = _as_bytes(data)
    if validate:
        _check_vcid(vcid)
        _check_length("CLTU", data, 1, CLTU_MAX)
    return _data_request(OP_CLTU, vcid, tag, data, opts)


def build_transfer_frame(codeblock_length: int, tag: int, data: bytes,
                         opts: ProtocolOptions, validate: bool = True) -> bytes:
    """Transfer Frame (op 50); offset 4 is the codeblock length, not a VCID."""
    data = _as_bytes(data)
    if validate:
        if codeblock_length not in CODEBLOCK_LENGTHS:
            raise ProtocolError(f"codeblock length must be one of {CODEBLOCK_LENGTHS}")
        _check_length("Transfer Frame", data, FRAME_MIN, FRAME_MAX)
    return _data_request(OP_FRAME, codeblock_length, tag, data, opts)


def build_directive(op: int, vcid: int, tag: int, param: int, opts: ProtocolOptions) -> bytes:
    """Directive, body [op, vcid, tag, param], 32 bytes."""
    if op not in DIRECTIVES:
        raise ProtocolError(f"unknown directive {op!r}")
    _check_vcid(vcid)
    _check_tag(tag)
    return build_message(opts.telecommand_flow_id, [op, vcid, tag, param])


def build_nop(vcid: int, tag: int, opts: ProtocolOptions) -> bytes:
    """NOP, body [13, vcid, tag, 0], 32 bytes."""
    _check_vcid(vcid)
    _check_tag(tag)
    return build_message(opts.telecommand_flow_id, [OP_NOP, vcid, tag, 0])


def build_output_flow_subscription(vcid: int, tag: int, flow: int, authorization: int,
                                   opts: ProtocolOptions) -> bytes:
    """Output flow subscription, body [14, vcid, tag, flow, auth], 36 bytes."""
    _check_vcid(vcid)
    _check_tag(tag)
    if not 0 <= flow <= 6:
        raise ProtocolError(f"output flow must be 0..6, got {flow!r}")
    if not 0 <= authorization <= 2:
        raise ProtocolError(f"authorization must be 0..2, got {authorization!r}")
    return build_message(opts.telecommand_flow_id,
                         [OP_SUBSCRIBE, vcid, tag, flow, authorization])


# ---------------------------------------------------------------- results
@dataclass(frozen=True)
class UnknownResponse:
    """Any response body the parser does not recognise; parsers return it instead of raising."""
    raw: bytes
    reason: str


@dataclass(frozen=True)
class NegativeResponse:
    """Server answered with the single word -1 (size 20)."""


@dataclass(frozen=True)
class ControlAck:
    """Control request acknowledgement."""
    tag: int
    status: int


@dataclass(frozen=True)
class MonitoringReport:
    """Monitoring message: config (40 words) and status (20 words)."""
    tag: int
    config: "tuple[int, ...]"
    status: "tuple[int, ...]"

    def config_named(self) -> "dict[str, int]":
        """Configuration values by name for ids 0..28."""
        return {name: self.config[pid] for pid, name in CONFIG_PARAM_NAMES.items()}

    def status_error_flags(self) -> "list[str]":
        """Names of the bits set in status[0], ascending bit order."""
        word = to_u32(self.status[0])
        return [name for bit, name in sorted(STATUS_ERROR_BITS.items()) if word & bit]

    def tm_port(self) -> "tuple[int, int]":
        """(TMU number, port) decoded from config[3]."""
        return decode_tm_port(self.config[3])

    def cop1_version(self) -> "tuple[int, int, int]":
        """(revision, issue, options) from status[7].

        Byte 0 is the MOST significant byte and holds the revision.
        """
        word = to_u32(self.status[7])
        return ((word >> 24) & 0xFF, (word >> 16) & 0xFF, (word >> 8) & 0xFF)


@dataclass(frozen=True)
class AcceptReject:
    """Accept/reject response."""
    op: int
    vcid: int
    tag: int
    accepted: bool


@dataclass(frozen=True)
class ConfirmResponse:
    """Confirm response."""
    op: int
    vcid: int
    tag: int
    positive: bool


@dataclass(frozen=True)
class AlertIndication:
    """Alert indication."""
    vcid: int
    code: int
    name: str


@dataclass(frozen=True)
class SuspendIndication:
    """Suspend indication."""
    vcid: int


@dataclass(frozen=True)
class SegmentEcho:
    """Transfer Frame / Transfer Segment echo."""
    op: int
    field: int
    tag: int
    length: int
    data: bytes


@dataclass(frozen=True)
class CltuEcho:
    """CLTU echo."""
    vcid: int
    tag: int
    frame_sequence: int
    error_location: int
    length: int
    data: bytes


@dataclass(frozen=True)
class AdditionalInfo:
    """Additional information."""
    vcid: int
    last_clcw: int
    state: int
    last_event: int
    v_s: int
    nn_r: int
    n_r: int
    timer_initial: int
    timeout_type: int
    transmission_limit: int
    transmission_count: int
    suspended: int
    sliding_window: int


# ---------------------------------------------------------------- parsers
def _is_negative(msg: Message) -> bool:
    return msg.size == 20 and msg.body == (-1,)


def parse_control_response(msg: Message):
    """Control acknowledgement (size 32) or negative response (size 20)."""
    if _is_negative(msg):
        return NegativeResponse()
    if msg.size == 32 and msg.body[0] == OP_CONTROL:
        return ControlAck(tag=to_u32(msg.body[2]), status=msg.body[3])
    return UnknownResponse(msg.raw, "unrecognised control response")


def parse_monitoring_response(msg: Message):
    """Monitoring message (size 268) or negative message (size 20)."""
    if _is_negative(msg):
        return NegativeResponse()
    if msg.size == 268 and msg.body[0] == OP_MONITORING:
        return MonitoringReport(tag=to_u32(msg.body[2]), config=tuple(msg.body[3:43]),
                                status=tuple(msg.body[43:63]))
    return UnknownResponse(msg.raw, "unrecognised monitoring response")


def _echo_data(words: "tuple[int, ...]", length: int) -> Optional[bytes]:
    """First `length` bytes of the data words, or None if the length is impossible."""
    if length < 0 or length > 4 * len(words):
        return None
    packed = b"".join(to_u32(w).to_bytes(4, "big") for w in words)
    return packed[:length]


def parse_tc_response(msg: Message):
    """Telecommand output flows 0 to 6, chosen by size and body[0].

    Flow 0 accept/reject, flow 1 confirm, flow 2 alert, flow 3 suspend, flow 4
    Transfer Frame and Segment echo, flow 5 CLTU echo, flow 6 additional information;
    size 20 is the negative reply.
    """
    if _is_negative(msg):
        return NegativeResponse()
    body = msg.body
    if not body:
        return UnknownResponse(msg.raw, "empty body")
    flow = body[0]
    size = msg.size
    if flow == 0 and size == 36:
        return AcceptReject(op=body[1], vcid=body[2], tag=to_u32(body[3]), accepted=(body[4] == 0))
    if flow == 1 and size == 36:
        return ConfirmResponse(op=body[1], vcid=body[2], tag=to_u32(body[3]), positive=(body[4] == 0))
    if flow == 2 and size == 32:
        return AlertIndication(vcid=body[2], code=body[3],
                               name=_ALERT_NAMES.get(body[3], "unknown"))
    if flow == 3 and size == 32:
        return SuspendIndication(vcid=body[2])
    if flow == 4 and len(body) >= 5:
        data = _echo_data(body[5:], body[4])
        if data is None:
            return UnknownResponse(msg.raw, "echo length exceeds available data")
        return SegmentEcho(op=body[1], field=body[2], tag=to_u32(body[3]),
                           length=body[4], data=data)
    if flow == 5 and len(body) >= 7:
        data = _echo_data(body[7:], body[6])
        if data is None:
            return UnknownResponse(msg.raw, "echo length exceeds available data")
        return CltuEcho(vcid=body[2], tag=to_u32(body[3]), frame_sequence=body[4],
                        error_location=body[5], length=body[6], data=data)
    if flow == 6 and size == 76:
        return AdditionalInfo(
            vcid=body[2], last_clcw=body[3], state=body[4], last_event=body[5],
            v_s=body[6], nn_r=body[7], n_r=body[8], timer_initial=body[9],
            timeout_type=body[10], transmission_limit=body[11],
            transmission_count=body[12], suspended=body[13], sliding_window=body[14])
    return UnknownResponse(msg.raw, f"unrecognised output flow {flow} with size {size}")
