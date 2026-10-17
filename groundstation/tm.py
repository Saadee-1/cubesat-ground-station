"""Telemetry pipeline: source -> decode."""
import queue
import threading
import time
from typing import Optional

from .config import ParamDef
from .decode import (STATUS_CONVERSION_ERROR, STATUS_OUT_OF_RANGE,
                     STATUS_SHORT_FRAME, ParamValue, decode_frame)
from .source import FrameSource


class TelemetryPipeline:
    """Drains frames from a source and decodes them with the current config."""

    def __init__(self, source: FrameSource, params: list[ParamDef]) -> None:
        self.source = source
        self._params = list(params)
        self._lock = threading.Lock()  # protects _params and the counters
        self.frames_total = 0
        self.frames_with_errors = 0    # frames with a SHORT_FRAME / CONVERSION_ERROR
        self.frames_out_of_range = 0   # frames with only OUT_OF_RANGE problems
        self.last_update_time: Optional[float] = None  # wall-clock time of last frame

    @property
    def params(self) -> list[ParamDef]:
        """A copy of the active parameter list."""
        with self._lock:
            return list(self._params)

    def set_params(self, new_params: list[ParamDef]) -> None:
        """Swap the configuration; also tells the source (a sim must repack frames)."""
        with self._lock:
            self._params = list(new_params)
            # New configuration, new statistics.
            self.frames_total = 0
            self.frames_with_errors = 0
            self.frames_out_of_range = 0
        self.source.set_params(new_params)
        # Frames already queued were built for the OLD layout; throw them away.
        while True:
            try:
                self.source.frames.get_nowait()
            except queue.Empty:
                break

    def poll(self) -> list[list[ParamValue]]:
        """Decode every queued frame; return one list of ParamValue per frame."""
        params = self.params  # snapshot so a swap mid-poll cannot mix configs
        results: list[list[ParamValue]] = []
        while True:
            try:
                frame = self.source.frames.get_nowait()
            except queue.Empty:
                break
            decoded = decode_frame(frame, params)
            with self._lock:
                self.frames_total += 1
                statuses = {v.status for v in decoded}
                if statuses & {STATUS_SHORT_FRAME, STATUS_CONVERSION_ERROR}:
                    # Real errors: the frame could not be fully decoded.
                    self.frames_with_errors += 1
                elif STATUS_OUT_OF_RANGE in statuses:
                    # Decoded fine, but some value is outside its limits.
                    self.frames_out_of_range += 1
                self.last_update_time = time.time()
            results.append(decoded)
        return results
