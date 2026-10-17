"""Frame sources that feed the existing telemetry pipeline from the network (no Tk, no threads).

TelemetryPipeline (tm.py) reads `pipeline.source.frames`. NetworkFrameSource is a FrameSource
whose queue is filled by the Telemetry tab with the frames of CORTEX telemetry messages
. SwitchableSource lets the same pipeline use either the simulated source
or the network source.
"""
import queue
from typing import Callable, Optional, Sequence

from .clients import TelemetryClient
from .config import ParamDef
from .net import ClientError
from .protocol import ProtocolError
from .source import FrameSource

MODE_SIMULATED = "Simulated"
MODE_NETWORK = "Network"
MODES = (MODE_SIMULATED, MODE_NETWORK)


class NetworkFrameSource(FrameSource):
    """Sends the telemetry request and holds the frames received."""

    name = "Network (CORTEX telemetry)"

    def __init__(self, client: TelemetryClient) -> None:
        super().__init__()
        self.client = client
        # Request settings, plain attributes (the Telemetry tab fills them in).
        self.channel = 0
        self.buffered_blocks = 0
        self.data_flow = 0
        self.frame_mask = 0
        self.expected_value = 0
        self.last_error = ""
        self.streaming = False  # True after a successful start() until stop() or link loss
        self.starts = 0         # how many successful start() calls so far
        # Optional hook run before a request is sent. It returns False (after setting
        # last_error) when the settings are not valid; then nothing is sent.
        self.prepare: Optional[Callable[[], bool]] = None

    def start(self) -> bool:
        """Send the telemetry request. Never raises: False plus last_error on failure."""
        if self.prepare is not None and not self.prepare():
            return False
        try:
            self.client.start(self.channel, self.buffered_blocks, self.data_flow,
                              self.frame_mask, self.expected_value)
        except (ClientError, ProtocolError) as exc:
            self.last_error = str(exc)
            return False
        self.last_error = ""
        self.streaming = True
        self.starts += 1
        return True

    def stop(self) -> bool:
        """Send the stop request. Never raises: False plus last_error on failure."""
        try:
            self.client.stop(self.channel)
        except (ClientError, ProtocolError) as exc:
            self.last_error = str(exc)
            self.streaming = False
            return False
        self.last_error = ""
        self.streaming = False
        return True

    def mark_stopped(self) -> None:
        """The stream is over (link lost, refusal, stop acknowledged); nothing is sent."""
        self.streaming = False

    def push_frame(self, frame: bytes) -> None:
        """Queue one frame for the pipeline."""
        self.frames.put(bytes(frame))


def _drain(q: "queue.Queue") -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class SwitchableSource(FrameSource):
    """Wraps a simulated and a network source; `frames` is always the ACTIVE source's queue.

    The FrameSource base class assigns `self.frames` in its constructor. Here `frames` is a
    read-only property, so the base constructor is deliberately not called.
    """

    def __init__(self, simulated: FrameSource, network: FrameSource) -> None:
        self.simulated = simulated
        self.network = network
        self._mode = MODE_SIMULATED

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active(self) -> FrameSource:
        return self.network if self._mode == MODE_NETWORK else self.simulated

    @property
    def frames(self) -> "queue.Queue[bytes]":  # type: ignore[override]
        return self.active.frames

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.active.name

    def select(self, mode: str) -> None:
        """Stop the active source, switch, and drop frames queued by either source."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; use one of {MODES}")
        if mode == self._mode:
            return
        self.active.stop()
        self._mode = mode
        _drain(self.simulated.frames)  # nothing from the other source may leak into the pipeline
        _drain(self.network.frames)

    def start(self):
        return self.active.start()

    def stop(self):
        return self.active.stop()

    def set_params(self, params: Sequence[ParamDef]) -> None:
        self.simulated.set_params(params)
        self.network.set_params(params)


def make_switchable(pipeline, client: TelemetryClient) -> SwitchableSource:
    """Make pipeline.source a SwitchableSource (a no-op if it already is one)."""
    if isinstance(pipeline.source, SwitchableSource):
        return pipeline.source
    switch = SwitchableSource(pipeline.source, NetworkFrameSource(client))
    pipeline.source = switch
    return switch
