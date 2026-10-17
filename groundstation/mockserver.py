"""MOCK servers for COP-1 and CORTEX, for tests and offline demos.

This is a test double, NOT the real CORTEX or COP-1 software. It was written
only from the COP-1 and CORTEX interface documents, so it validates our reading of the
documents and nothing more. It never crashes on malformed
input, and stop() always returns within 2 seconds.

The telemetry frames come from groundstation.source.SimFrameSource (config
driven), so they decode with the same parameter file the client uses.
"""
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import load_config
from .protocol import (FrameReader, Message, ProtocolOptions, START_WORD,
                       build_message)
from .source import SimFrameSource

DEFAULT_CONFIG = Path(__file__).resolve().parent / "samples" / "sample_config_10.json"
_SERVICES = ("control", "monitoring", "telecommand", "telemetry")
_U32 = 0xFFFFFFFF
_SERVER_STALL_S = 2.0  # drop a stalled partial message after this long


@dataclass
class MockFaults:
    """Deliberate misbehaviour switches (0 / False means well-behaved)."""
    garbage_every_n: int = 0          # 3 junk bytes before every nth telemetry message
    stall_once: bool = False          # one stray start word with a huge size, before streaming
    drop_after_n_messages: int = 0    # close the connection after n telemetry messages
    bad_frame_length_every_n: int = 0  # every nth message declares frame_length > payload
    tm_error_code: int = 0            # 1, 2 or 3: nack the first start request, stream nothing
    decoder_status: int = 0
    frame_check_word: int = 0


class _Conn:
    """State of one client connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()  # serialises sends and the counters below
        self.closed = False
        self.stream_stop = threading.Event()
        self.stream_thread: Optional[threading.Thread] = None
        self.sequence = 0
        self.first_start = True

    def send(self, data: bytes) -> bool:
        """Send bytes; False if the peer is gone."""
        with self.lock:
            return self._send_locked(data)

    def _send_locked(self, data: bytes) -> bool:
        if self.closed:
            return False
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            self.closed = True
            return False

    def close(self) -> None:
        self.closed = True
        self.stream_stop.set()
        for action in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                action()
            except OSError:
                pass


class MockServer:
    """A mock COP-1 or CORTEX service. See the module docstring."""

    def __init__(self, service: str, *, host: str = "127.0.0.1", port: int = 0,
                 opts: Optional[ProtocolOptions] = None, config_path: Optional[str] = None,
                 frame_rate_hz: float = 20.0, faults: Optional[MockFaults] = None,
                 shared_params: Optional[dict] = None) -> None:
        if service not in _SERVICES:
            raise ValueError(f"service must be one of {_SERVICES}")
        if frame_rate_hz <= 0:
            raise ValueError("frame_rate_hz must be > 0")
        self.service = service
        self.host = host
        self._requested_port = port
        self.opts = opts or ProtocolOptions()
        self.frame_rate_hz = frame_rate_hz
        self.faults = faults or MockFaults()
        self._params: dict = shared_params if shared_params is not None else {}
        self._lock = threading.Lock()
        self._received: "list[Message]" = []
        self._stray_sent = False
        self._flow_id = getattr(self.opts, f"{service}_flow_id")
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._conns: "list[_Conn]" = []
        self._threads: "list[threading.Thread]" = []
        self._port = 0
        # Frame source for telemetry, reused from source.py (not modified).
        self._frames = None
        if service == "telemetry":
            self._frames = SimFrameSource(load_config(str(config_path or DEFAULT_CONFIG)),
                                          rate_hz=frame_rate_hz, seed=1)

    # ------------------------------------------------------------ properties
    @property
    def port(self) -> int:
        """The actual bound port (0 before start())."""
        return self._port

    @property
    def received(self) -> "list[Message]":
        """A copy of every framed message the server received."""
        with self._lock:
            return list(self._received)

    @property
    def params(self) -> dict:
        """A copy of the control state (configuration parameters)."""
        with self._lock:
            return dict(self._params)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Bind, listen and accept clients on a daemon thread."""
        if self._listener is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((self.host, self._requested_port))
        listener.listen(8)
        listener.settimeout(0.1)
        self._listener = listener
        self._port = listener.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                               name=f"mock-{self.service}-accept")
        self._accept_thread.start()

    def stop(self) -> None:
        """Close everything and join the threads (idempotent, under 2 s)."""
        self._stop.set()
        deadline = time.monotonic() + 1.8
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            conns, threads = list(self._conns), list(self._threads)
        for conn in conns:
            conn.close()
        if self._accept_thread is not None:
            threads.append(self._accept_thread)
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                sock, _ = listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            conn = _Conn(sock)
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True,
                                      name=f"mock-{self.service}-conn")
            with self._lock:
                self._conns.append(conn)
                self._threads.append(thread)
            thread.start()

    # ------------------------------------------------------------ connection
    def _serve(self, conn: _Conn) -> None:
        reader = FrameReader()
        conn.sock.settimeout(0.1)
        pending_since: Optional[float] = None
        try:
            while not self._stop.is_set() and not conn.closed:
                try:
                    chunk = conn.sock.recv(4096)
                except (socket.timeout, TimeoutError):
                    chunk = None
                except OSError:
                    break
                if chunk == b"":
                    break
                events = reader.feed(chunk) if chunk else []
                for ev in events:
                    if isinstance(ev, Message):
                        self._on_message(conn, ev)
                # Do not let a stray start word with a big size block the connection.
                if reader.pending_size is None:
                    pending_since = None
                elif pending_since is None:
                    pending_since = time.monotonic()
                elif time.monotonic() - pending_since > _SERVER_STALL_S:
                    reader.resync()
                    pending_since = None
        except Exception:  # noqa: BLE001 - a mock must never crash on bad input
            pass
        finally:
            conn.close()
            stream = conn.stream_thread
            if stream is not None and stream is not threading.current_thread():
                stream.join(timeout=0.5)

    def _on_message(self, conn: _Conn, msg: Message) -> None:
        with self._lock:
            self._received.append(msg)
        try:
            if msg.flow_id != self._flow_id:
                self._negative(conn)
                return
            getattr(self, f"_handle_{self.service}")(conn, msg)
        except Exception:  # noqa: BLE001 - malformed body: answer negatively, keep going
            self._negative(conn)

    def _negative(self, conn: _Conn) -> None:
        """The 20-byte negative response, body [-1]."""
        conn.send(build_message(self._flow_id, [-1]))

    def _reply(self, conn: _Conn, body: "list[int]") -> None:
        conn.send(build_message(self._flow_id, body))

    # ------------------------------------------------------------ control
    def _handle_control(self, conn: _Conn, msg: Message) -> None:
        """Control request -> acknowledgement or negative response."""
        body = msg.body
        if len(body) < 4 or body[0] != 1:
            self._negative(conn)
            return
        count = body[3]
        if count < 0 or len(body) != 4 + 2 * count:
            self._negative(conn)
            return
        pairs = [(body[4 + 2 * i], body[5 + 2 * i]) for i in range(count)]
        if any(not 0 <= pid <= 39 for pid, _ in pairs):
            self._negative(conn)
            return
        with self._lock:
            for pid, value in pairs:
                self._params[pid] = value & _U32
        self._reply(conn, [1, 0, body[2] & _U32, 0])

    # ------------------------------------------------------------ monitoring
    def _handle_monitoring(self, conn: _Conn, msg: Message) -> None:
        """Monitoring request -> monitoring message."""
        body = msg.body
        if len(body) != 3 or body[0] != 0:
            self._negative(conn)
            return
        with self._lock:
            config = [self._params.get(pid, 0) for pid in range(40)]
        status = [0] * 20
        self._reply(conn, [0, 0, body[2] & _U32, *config, *status])

    # ------------------------------------------------------------ telecommand
    def _checksum_ok(self, msg: Message) -> bool:
        """Independent check: the selected words, checksum included, sum to 0 mod 2**32.

        Rule from the cop1.checksum_word docstring: whole_packet sums every word,
        excluding_postamble every word but the last, payload_only words[3:-1].
        """
        words = struct.unpack(f">{len(msg.raw) // 4}I", msg.raw)
        mode = self.opts.checksum_mode
        if mode == "whole_packet":
            selected = words
        elif mode == "excluding_postamble":
            selected = words[:-1]
        else:
            selected = words[3:-1]
        return sum(selected) % 2 ** 32 == 0

    def _handle_telecommand(self, conn: _Conn, msg: Message) -> None:
        """Telecommand requests -> negative, accept or confirm replies."""
        body = msg.body
        op = body[0]
        if op in (20, 30, 40, 50):
            if not self._valid_data_request(op, body) or not self._checksum_ok(msg):
                self._negative(conn)
                return
            self._accept(conn, op, body[1], body[2])
            if op == 20:
                self._confirm(conn, op, body[1], body[2])
        elif 2 <= op <= 13:  # directives and NOP
            if len(body) != 4 or not 0 <= body[1] <= 63:
                self._negative(conn)
                return
            self._accept(conn, op, body[1], body[2])
            self._confirm(conn, op, body[1], body[2])
        elif op == 14:  # output flow subscription
            if len(body) != 5 or not 0 <= body[1] <= 63 or not 0 <= body[3] <= 6 \
                    or not 0 <= body[4] <= 2:
                self._negative(conn)
                return
            self._accept(conn, op, body[1], body[2])
        else:
            self._negative(conn)

    def _valid_data_request(self, op: int, body: "tuple[int, ...]") -> bool:
        """Length and field limits of the AD, BD, CLTU and Transfer Frame requests."""
        if len(body) < 5:
            return False
        field4, length = body[1], body[3]
        words = -(-length // 4) if length >= 0 else -1
        if words < 0 or len(body) != 4 + words + 1:  # header, data words, checksum
            return False
        if op in (20, 30):
            limit = 1017 if self.opts.fec_in_frame else 1019
            return 1 <= length <= limit and 0 <= field4 <= 63
        if op == 40:
            return 1 <= length <= 1284 and 0 <= field4 <= 63
        return 6 <= length <= 1024 and field4 in (0, 5, 6, 7, 8)

    def _accept(self, conn: _Conn, op: int, vcid: int, tag: int) -> None:
        """Accept response, body [0, op, vcid, tag, 0]."""
        self._reply(conn, [0, op, vcid, tag & _U32, 0])

    def _confirm(self, conn: _Conn, op: int, vcid: int, tag: int) -> None:
        """Positive confirm response, body [1, op, vcid, tag, 0]."""
        self._reply(conn, [1, op, vcid, tag & _U32, 0])

    # ------------------------------------------------------------ telemetry
    def _nack(self, conn: _Conn, code: int) -> None:
        """Negative telemetry acknowledgement."""
        self._reply(conn, [code])

    def _stop_stream(self, conn: _Conn) -> None:
        conn.stream_stop.set()
        thread = conn.stream_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        conn.stream_thread = None

    def _handle_telemetry(self, conn: _Conn, msg: Message) -> None:
        """Telemetry request -> telemetry messages or acknowledgements."""
        body = msg.body
        streaming = conn.stream_thread is not None and not conn.stream_stop.is_set()
        valid = (msg.size == 64 and 0 <= body[0] <= 5 and 0 <= body[1] <= 1024
                 and body[2] in (0, 1, 2, 0x80))
        if not valid:
            self._stop_stream(conn)
            self._nack(conn, 1)
            return
        flow = body[2]
        if flow == 0x80:  # stop: finish streaming first so no message follows the ack
            self._stop_stream(conn)
            self._reply(conn, [0])
            return
        if streaming:  # a second start while streaming
            self._stop_stream(conn)
            self._nack(conn, 1)
            return
        if conn.first_start:
            conn.first_start = False
            if self.faults.tm_error_code in (1, 2, 3):
                self._nack(conn, self.faults.tm_error_code)
                return
        if self.faults.stall_once:
            with self._lock:
                first = not self._stray_sent
                self._stray_sent = True
            if first:
                conn.send(struct.pack(">iI", START_WORD, 1_000_000))
        if flow == 1:
            self._send_telemetry(conn)
            return
        conn.stream_stop.clear()
        conn.stream_thread = threading.Thread(target=self._stream, args=(conn,), daemon=True,
                                              name="mock-telemetry-stream")
        conn.stream_thread.start()

    def _stream(self, conn: _Conn) -> None:
        period = 1.0 / self.frame_rate_hz
        while not conn.stream_stop.is_set() and not conn.closed:
            if not self._send_telemetry(conn):
                return
            conn.stream_stop.wait(period)

    def _send_telemetry(self, conn: _Conn) -> bool:
        """Build and send one telemetry message. False if the link is gone."""
        faults = self.faults
        frame = self._frames.next_frame()
        payload = frame + b"\x00" * (-len(frame) % 4)
        with conn.lock:
            conn.sequence += 1
            count = conn.sequence
            now = time.time()
            frame_length = len(frame)
            if faults.bad_frame_length_every_n and count % faults.bad_frame_length_every_n == 0:
                frame_length = len(payload) + 4  # declares more than the payload holds
            words = [int(now) & _U32, int((now % 1) * 1_000_000), count,
                     faults.frame_check_word & _U32, 1, 0, 0, frame_length, 16,
                     faults.decoder_status, 0, 0, 0]
            data = build_message(self._flow_id, words + list(
                struct.unpack(f">{len(payload) // 4}I", payload)))
            if faults.garbage_every_n and count % faults.garbage_every_n == 0:
                data = bytes([1, 2, 3]) + data  # junk without a start word
            ok = conn._send_locked(data)
        if ok and faults.drop_after_n_messages and count >= faults.drop_after_n_messages:
            conn.close()
            return False
        return ok
