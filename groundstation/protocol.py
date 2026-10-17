"""Wire-protocol layer: message framing shared by COP-1 and CORTEX.

Sources: the COP-1 and CORTEX interface documents. Every message is a list of 32-bit XDR words
(big-endian): word 0 start, word 1 size in bytes, word 2 flow id, then the
body, then the postamble. This module has no sockets, threads or GUI code.
"""
import struct
from dataclasses import dataclass
from typing import Union

START_WORD = 1234567890
POSTAMBLE_WORD = -1234567890
START_BYTES = struct.pack(">i", START_WORD)          # 49 96 02 d2
POSTAMBLE_BYTES = struct.pack(">i", POSTAMBLE_WORD)  # b6 69 fd 2e
HEADER_BYTES = 12        # start + size + flow id (offsets 0 to 2)
MIN_MESSAGE_BYTES = 16   # header + postamble, empty body
MAX_MESSAGE_BYTES = 1_048_576  # our own sanity cap, not from any document

DEFAULT_PORTS = {"control": 3101, "monitoring": 3100,
                 "telecommand": 3120, "telemetry": 3070}

_CHECKSUM_MODES = ("whole_packet", "excluding_postamble", "payload_only")
_U32_MAX = 0xFFFFFFFF


class ProtocolError(Exception):
    """A message or request violates the wire format."""


@dataclass(frozen=True)
class ProtocolOptions:
    """Settings the interface documents leave open. Defaults are our working assumptions."""
    cortex_byte0_is_msb: bool = True      # only used by cortex.interpret_frame_check
    checksum_mode: str = "whole_packet"   # see checksum_word
    control_flow_id: int = 1
    monitoring_flow_id: int = 0
    telecommand_flow_id: int = 0
    telemetry_flow_id: int = 0
    fec_in_frame: bool = False

    def __post_init__(self) -> None:
        if self.checksum_mode not in _CHECKSUM_MODES:
            raise ValueError(f"checksum_mode must be one of {_CHECKSUM_MODES}, "
                             f"got {self.checksum_mode!r}")
        for name in ("control_flow_id", "monitoring_flow_id",
                     "telecommand_flow_id", "telemetry_flow_id"):
            value = getattr(self, name)
            # bool is a subclass of int, so reject it explicitly
            if not isinstance(value, int) or isinstance(value, bool) \
                    or not 0 <= value <= _U32_MAX:
                raise ValueError(f"{name} must be an integer in 0..0xFFFFFFFF, got {value!r}")


def to_u32(w: int) -> int:
    """Two's complement view of a word as unsigned 0..2**32-1."""
    return w & _U32_MAX


def to_i32(w: int) -> int:
    """Two's complement view of a word as signed -2**31..2**31-1."""
    w &= _U32_MAX
    return w - (1 << 32) if w >= (1 << 31) else w


def build_message(flow_id: int, body_words: "list[int] | tuple[int, ...]") -> bytes:
    """Pack start, size, flow id, body and postamble into XDR bytes.

    Size = 4 * (3 + len(body) + 1). Words may be -2**31..2**32-1.
    """
    words = [flow_id, *body_words]
    for w in words:
        if not isinstance(w, int) or isinstance(w, bool) or not -(1 << 31) <= w <= _U32_MAX:
            raise ProtocolError(f"word {w!r} does not fit in 32 bits")
    size = 4 * (3 + len(body_words) + 1)
    packed = [START_WORD, size, flow_id, *body_words, POSTAMBLE_WORD]
    return struct.pack(f">{len(packed)}I", *(to_u32(w) for w in packed))


def pad_to_words(data: bytes) -> "list[int]":
    """Zero-pad `data` to a multiple of 4 bytes; return signed 32-bit words."""
    if not data:
        return []
    padded = data + b"\x00" * (-len(data) % 4)
    return [to_i32(w) for w in struct.unpack(f">{len(padded) // 4}I", padded)]


def checksum_word(words_without_checksum: "list[int]", mode: str) -> int:
    """Checksum word that makes the selected words sum to 0 mod 2**32.

    `words_without_checksum` is the whole packet [start, size, flow, body...,
    postamble] with the checksum slot removed. Modes: "whole_packet" sums all
    words, "excluding_postamble" all but the last, "payload_only" words[3:-1].
    """
    if mode == "whole_packet":
        selected = words_without_checksum
    elif mode == "excluding_postamble":
        selected = words_without_checksum[:-1]
    elif mode == "payload_only":
        selected = words_without_checksum[3:-1]
    else:
        raise ValueError(f"unknown checksum mode {mode!r}")
    return to_i32(-sum(selected))


@dataclass(frozen=True)
class Message:
    """A parsed message: flow id, signed body words and the original bytes."""
    flow_id: int
    body: "tuple[int, ...]"
    raw: bytes

    @property
    def size(self) -> int:
        return len(self.raw)

    def body_bytes(self) -> bytes:
        """The body words concatenated big-endian."""
        return struct.pack(f">{len(self.body)}i", *self.body)


def parse_message(data: bytes) -> Message:
    """Validate and split one complete message. Raises ProtocolError."""
    if len(data) < MIN_MESSAGE_BYTES:
        raise ProtocolError(f"message too short: {len(data)} bytes")
    if len(data) % 4:
        raise ProtocolError(f"message length {len(data)} is not a multiple of 4")
    words = struct.unpack(f">{len(data) // 4}i", data)
    if words[0] != START_WORD:
        raise ProtocolError("wrong start word")
    if words[1] != len(data):
        raise ProtocolError(f"size field {words[1]} does not match length {len(data)}")
    if words[-1] != POSTAMBLE_WORD:
        raise ProtocolError("wrong postamble")
    return Message(flow_id=to_u32(words[2]), body=tuple(words[3:-1]), raw=bytes(data))


@dataclass(frozen=True)
class StreamError:
    """Bytes the FrameReader had to drop, and why."""
    reason: str
    discarded: int


class FrameReader:
    """Re-assembles messages from an arbitrary byte stream (no sockets here)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.discarded_total = 0

    def _drop(self, count: int, reason: str, events: list) -> None:
        """Remove `count` bytes from the front and record the loss."""
        del self._buf[:count]
        self.discarded_total += count
        events.append(StreamError(reason, count))

    @property
    def pending_size(self) -> "Union[int, None]":
        """Declared size of the message being waited for, or None.

        Only set when the buffer starts with START_BYTES, at least 8 bytes are
        buffered and the declared size is valid.
        """
        if len(self._buf) < 8 or bytes(self._buf[:4]) != START_BYTES:
            return None
        size = struct.unpack(">I", self._buf[4:8])[0]
        if size < MIN_MESSAGE_BYTES or size % 4 or size > MAX_MESSAGE_BYTES:
            return None
        return size

    def feed(self, chunk: bytes) -> "list[Union[Message, StreamError]]":
        """Add bytes; return every Message / StreamError completed by them."""
        self._buf.extend(chunk)
        return self._drain()

    def resync(self) -> "list[Union[Message, StreamError]]":
        """Give up on a stalled message and look for the next start word.

        If the buffer starts with START_BYTES, those 4 bytes are dropped and
        recorded as StreamError("resync forced", 4); then whatever can be
        completed is returned. An engine calls this when a message has stalled.
        """
        events: list = []
        if bytes(self._buf[:4]) == START_BYTES:
            self._drop(4, "resync forced", events)
        events += self._drain()
        return events

    def _drain(self) -> "list[Union[Message, StreamError]]":
        """Extract every complete message from the buffer, dropping bad bytes."""
        events: list = []
        while True:  # every pass consumes bytes or breaks, so it always ends
            idx = self._buf.find(START_BYTES)
            if idx == -1:
                # No start word: keep the last 3 bytes, a start word may straddle chunks.
                if len(self._buf) > 3:
                    self._drop(len(self._buf) - 3, "garbage before start", events)
                break
            if idx > 0:
                self._drop(idx, "garbage before start", events)
            if len(self._buf) < 8:
                break  # need the size word
            size = struct.unpack(">I", self._buf[4:8])[0]
            if size < MIN_MESSAGE_BYTES or size % 4 or size > MAX_MESSAGE_BYTES:
                self._drop(4, "invalid size", events)  # skip this start word, rescan
                continue
            if len(self._buf) < size:
                break  # wait for the rest of the message
            if bytes(self._buf[size - 4:size]) != POSTAMBLE_BYTES:
                self._drop(4, "bad postamble", events)
                continue
            events.append(parse_message(bytes(self._buf[:size])))
            del self._buf[:size]
        return events
