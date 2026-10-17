"""TCP client plumbing: connection state, events and a worker thread.

No GUI code here. A GUI reads the `events` queue (a queue.Queue of the frozen
dataclasses below) from its own thread, so widgets are never touched by the
worker. Framing and parsing come from groundstation.protocol.
"""
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .protocol import FrameReader, Message, ProtocolError, StreamError

_RECV_BYTES = 4096
_PEER_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class ConnState(Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class ClientError(Exception):
    """A client method was used in the wrong state or failed to send."""


@dataclass(frozen=True)
class StateChanged:
    state: ConnState
    detail: str


@dataclass(frozen=True)
class Sent:
    raw: bytes
    description: str
    tag: Optional[int]


@dataclass(frozen=True)
class Received:
    message: Message
    parsed: object
    at: float  # time.monotonic() when the message was completed


@dataclass(frozen=True)
class StreamProblem:
    error: StreamError


@dataclass(frozen=True)
class ParseFailure:
    message: Message
    error: str


# Events that may be dropped when the queue is full. StateChanged never is.
_DROPPABLE = (Received, Sent, StreamProblem, ParseFailure)


def describe_words(raw: bytes) -> "list[str]":
    """One debug line per 32-bit word, e.g. "[ 3] 0x00000001  1".

    Shows the word offset, the hex value and the signed decimal value. A
    trailing partial word is shown as "[ n] raw 0xAABB".
    """
    lines = []
    full = len(raw) // 4
    for i in range(full):
        (signed,) = struct.unpack(">i", raw[4 * i:4 * i + 4])
        lines.append(f"[{i:2d}] 0x{signed & 0xFFFFFFFF:08X}  {signed}")
    tail = raw[4 * full:]
    if tail:
        lines.append(f"[{full:2d}] raw 0x{tail.hex().upper()}")
    return lines


def _close_socket(sock: Optional[socket.socket]) -> None:
    """Shutdown then close; shutdown wakes a thread blocked in recv. Errors ignored."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class TcpClient:
    """A client with a background reader thread and an event queue."""

    def __init__(self, name: str, host: str, port: int,
                 parse: Callable[[Message], object], *,
                 connect_timeout: float = 3.0, stall_timeout: float = 2.0,
                 poll_interval: float = 0.2, max_queued_events: int = 20000) -> None:
        self.name = name
        self.host = host
        self.port = port
        self._parse = parse
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        self.poll_interval = poll_interval
        self.max_queued_events = max_queued_events
        self.events: "queue.Queue[object]" = queue.Queue()
        self.dropped_events = 0
        self._state = ConnState.DISCONNECTED
        self._lock = threading.Lock()       # guards state, socket, thread, generation
        self._send_lock = threading.Lock()  # one sendall at a time
        self._drop_lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        # Each connect() gets a new generation number. A worker whose generation
        # is out of date (the user disconnected) stays silent and exits.
        self._gen = 0

    # ---------------------------------------------------------------- state
    @property
    def state(self) -> ConnState:
        return self._state

    def configure(self, host: str, port: int) -> None:
        """Change the target; only allowed while DISCONNECTED or ERROR."""
        if not isinstance(host, str) or not host.strip():
            raise ClientError("host must not be empty")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ClientError("port must be 1..65535")
        with self._lock:
            if self._state not in (ConnState.DISCONNECTED, ConnState.ERROR):
                raise ClientError(f"cannot configure while {self._state.value}")
            self.host, self.port = host, port

    def _emit(self, event: object) -> None:
        """Queue an event, dropping bulk events when the queue is too full."""
        if isinstance(event, _DROPPABLE) and self.events.qsize() >= self.max_queued_events:
            with self._drop_lock:
                self.dropped_events += 1
            return
        self.events.put(event)

    def _change_state(self, state: ConnState, detail: str) -> None:
        """Set the state and queue the event. Caller must hold self._lock."""
        self._state = state
        self.events.put(StateChanged(state, detail))  # always delivered

    # ---------------------------------------------------------------- public
    def connect(self) -> None:
        """Start connecting in the background; returns immediately."""
        with self._lock:
            if self._state not in (ConnState.DISCONNECTED, ConnState.ERROR):
                raise ClientError(f"cannot connect while {self._state.value}")
            self._gen += 1
            gen, host, port = self._gen, self.host, self.port
            self._change_state(ConnState.CONNECTING, f"connecting to {host}:{port}")
            self._thread = threading.Thread(
                target=self._run, args=(gen, host, port),
                name=f"{self.name}-reader", daemon=True)
            self._thread.start()

    def disconnect(self) -> None:
        """Close the connection and stop the worker. Safe to call at any time."""
        with self._lock:
            sock, thread = self._sock, self._thread
            self._sock = None
            self._gen += 1  # silences the worker
            if self._state != ConnState.DISCONNECTED:
                self._change_state(ConnState.DISCONNECTED, "closed by user")
        _close_socket(sock)  # also wakes a worker blocked in recv
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def send(self, raw: bytes, description: str = "", tag: Optional[int] = None) -> None:
        """Send bytes on the open connection. Raises ClientError if that fails."""
        with self._send_lock:
            with self._lock:
                if self._state != ConnState.CONNECTED or self._sock is None:
                    raise ClientError(f"not connected (state {self._state.value})")
                sock, gen = self._sock, self._gen
            try:
                sock.sendall(raw)
            except OSError as exc:
                self._fail(gen, f"send failed: {exc}")
                raise ClientError(f"send failed: {exc}") from exc
        self._emit(Sent(bytes(raw), description, tag))

    def drain(self, max_events: Optional[int] = None) -> list:
        """Pop everything currently queued, without blocking."""
        out: list = []
        while max_events is None or len(out) < max_events:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # ---------------------------------------------------------------- worker
    def _fail(self, gen: int, detail: str) -> None:
        """Go to ERROR (once) unless this worker is out of date or already done."""
        with self._lock:
            if gen != self._gen or self._state not in (ConnState.CONNECTING, ConnState.CONNECTED):
                return
            sock, self._sock = self._sock, None
            self._change_state(ConnState.ERROR, detail)
        _close_socket(sock)

    def _peer_closed(self, gen: int, detail: str) -> None:
        """Go to DISCONNECTED because the peer went away."""
        with self._lock:
            if gen != self._gen or self._state != ConnState.CONNECTED:
                return
            sock, self._sock = self._sock, None
            self._change_state(ConnState.DISCONNECTED, detail)
        _close_socket(sock)

    def _handle(self, events: list) -> None:
        """Turn reader events into client events (parsing each message)."""
        for ev in events:
            if isinstance(ev, StreamError):
                self._emit(StreamProblem(ev))
                continue
            try:
                parsed = self._parse(ev)
            except ProtocolError as exc:
                self._emit(ParseFailure(ev, str(exc)))  # the connection stays usable
                continue
            self._emit(Received(ev, parsed, time.monotonic()))

    def _run(self, gen: int, host: str, port: int) -> None:
        """Worker thread: connect, then read until closed or told to stop."""
        sock: Optional[socket.socket] = None
        try:
            try:
                sock = socket.create_connection((host, port), self.connect_timeout)
            except OSError as exc:  # includes timeouts and refused connections
                self._fail(gen, f"connect failed: {exc}")
                return
            with self._lock:
                if gen != self._gen:  # user disconnected while we were connecting
                    _close_socket(sock)
                    return
                self._sock = sock
                self._change_state(ConnState.CONNECTED, f"connected to {host}:{port}")
            sock.settimeout(self.poll_interval)
            reader = FrameReader()
            pending_since: Optional[float] = None
            while gen == self._gen:
                events: list = []
                try:
                    chunk = sock.recv(_RECV_BYTES)
                except (socket.timeout, TimeoutError):
                    chunk = None  # nothing arrived: normal, just check the stall timer
                except _PEER_GONE as exc:
                    self._peer_closed(gen, f"connection reset by peer: {exc}")
                    return
                except OSError as exc:
                    self._fail(gen, f"receive failed: {exc}")
                    return
                if chunk == b"":
                    self._peer_closed(gen, "closed by peer")
                    return
                if chunk:
                    events = reader.feed(chunk)
                    self._handle(events)
                    if events:  # the front of the buffer changed: restart the stall timer
                        pending_since = None
                # Stall policy: a stray start word with a big size must not block
                # later messages forever.
                if reader.pending_size is None:
                    pending_since = None
                elif pending_since is None:
                    pending_since = time.monotonic()
                elif time.monotonic() - pending_since > self.stall_timeout:
                    self._handle(reader.resync())
                    pending_since = None
        except Exception as exc:  # noqa: BLE001 - never die silently
            self._fail(gen, f"internal error: {type(exc).__name__}: {exc}")
            _close_socket(sock)  # in case the socket was never handed over
