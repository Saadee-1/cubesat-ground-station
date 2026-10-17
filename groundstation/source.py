"""Frame sources: an abstract interface and a simulated implementation."""
import abc
import math
import queue
import random
import struct
import threading
from typing import Optional, Sequence, Union

from .config import DATA_TYPES, STRUCT_CODES, ParamDef, byte_offset

Number = Union[int, float]


class FrameSource(abc.ABC):
    """Anything that produces telemetry frames (bytes) onto `self.frames`."""

    name = "Unnamed source"

    def __init__(self) -> None:
        self.frames: "queue.Queue[bytes]" = queue.Queue()  # consumers drain this

    @abc.abstractmethod
    def start(self) -> None:
        """Begin producing frames."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop producing frames."""

    def set_params(self, params: Sequence[ParamDef]) -> None:
        """Tell the source about a new configuration (default: ignore it)."""


def build_frame(params: Sequence[ParamDef], raw_values: Sequence[Number],
                frame_len: int) -> bytes:
    """Pack each raw value at its position (with its endianness) into a frame."""
    frame = bytearray(frame_len)  # starts as all zero bytes
    for param, raw in zip(params, raw_values):
        start = byte_offset(param)
        end = start + param.length
        if start < 0 or end > frame_len:
            raise ValueError(f"{param.name!r} does not fit in a {frame_len}-byte frame")
        order = ">" if param.endianness == "big" else "<"  # struct byte-order prefix
        frame[start:end] = struct.pack(order + STRUCT_CODES[param.data_type], raw)
    return bytes(frame)


def _type_range(data_type: str) -> tuple[float, float]:
    """Smallest and largest value a data type can hold."""
    if data_type == "Float32":
        return -3.4e38, 3.4e38
    if data_type == "Float64":
        return -1.7e308, 1.7e308
    bits = DATA_TYPES[data_type] * 8
    if data_type.startswith("U"):
        return 0, 2 ** bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


def _sample_raw(data_type: str, phase: float, tick: int, rng: random.Random) -> Number:
    """A slowly varying raw value: sinusoid + small noise, clamped to the type range."""
    lo, hi = _type_range(data_type)
    if data_type.startswith("Float"):
        centre, amp = 10.0, 5.0
    else:
        # Keep the swing modest so values are readable even for 32/64-bit types.
        eff_lo, eff_hi = max(lo, -500), min(hi, 1000 if lo == 0 else 500)
        centre, amp = (eff_lo + eff_hi) / 2, (eff_hi - eff_lo) * 0.4
    value = centre + amp * math.sin(2 * math.pi * tick / 40 + phase) + rng.gauss(0, 0.02 * amp)
    value = min(max(value, lo), hi)  # clamp into the valid range
    return value if data_type.startswith("Float") else int(round(value))


class SimFrameSource(FrameSource):
    """Generates fake frames on a background daemon thread."""

    name = "Simulated source"

    def __init__(self, params: Sequence[ParamDef], rate_hz: float = 2.0,
                 frame_len: Optional[int] = None, corrupt_prob: float = 0.0,
                 seed: Optional[int] = None) -> None:
        super().__init__()
        if rate_hz <= 0:
            raise ValueError("rate_hz must be > 0")
        self.rate_hz = rate_hz
        self.corrupt_prob = corrupt_prob
        self._frame_len_arg = frame_len
        self._rng = random.Random(seed)  # own RNG so a seed gives repeatable output
        self._lock = threading.Lock()    # guards params/rng between GUI and sim thread
        self._tick = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.set_params(params)

    def set_params(self, params: Sequence[ParamDef]) -> None:
        """Switch to a new parameter list (frames are rebuilt to match it)."""
        with self._lock:
            self._params = list(params)
            self._phases = [self._rng.uniform(0, 2 * math.pi) for _ in self._params]
            if self._frame_len_arg is not None:
                self._frame_len = self._frame_len_arg
            else:
                # Last used byte + 8 spare bytes of padding.
                self._frame_len = max(byte_offset(p) + p.length for p in self._params) + 8

    def next_frame(self) -> bytes:
        """Build one frame (possibly corrupted). Called by the thread; tests may too."""
        with self._lock:
            raws = [_sample_raw(p.data_type, ph, self._tick, self._rng)
                    for p, ph in zip(self._params, self._phases)]
            self._tick += 1
            frame = build_frame(self._params, raws, self._frame_len)
            if self._rng.random() < self.corrupt_prob:
                if self._rng.random() < 0.5:
                    frame = frame[:self._rng.randrange(0, len(frame))]  # truncated
                else:
                    frame = self._rng.randbytes(self._rng.randrange(0, self._frame_len + 1))
            return frame

    def _run(self) -> None:
        """Thread body: emit a frame every 1/rate_hz seconds until stopped."""
        while not self._stop_event.is_set():
            self.frames.put(self.next_frame())
            self._stop_event.wait(1.0 / self.rate_hz)  # sleeps but wakes on stop()

    def start(self) -> None:
        """Start the producer thread (no-op if it is already running)."""
        if self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="SimFrameSource", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def is_running(self) -> bool:
        """True while the producer thread is alive."""
        return self._thread is not None and self._thread.is_alive()
