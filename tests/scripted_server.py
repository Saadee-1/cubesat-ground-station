"""A tiny scripted TCP server for tests that need a server which misbehaves on purpose.

Used for: a server that accepts and never answers (reply timeouts), servers that close at once,
send garbage or oversized size words (hostile input), and servers that answer only for one
checksum mode (the self check probe). The script runs on its own thread per connection.
"""
import socket
import threading
import time
from typing import Callable, Optional

import pytest


def silent(server: "ScriptedServer", conn: socket.socket) -> None:
    """Accept, read and discard everything, never answer."""
    server.read_until_closed(conn)


class ScriptedServer:
    """Listens on a free local port and runs `script(server, conn)` for every accepted connection."""

    def __init__(self, script: Optional[Callable] = None) -> None:
        self.script = script or silent
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.connections: "list[socket.socket]" = []
        self.received = bytearray()
        self.errors: "list[BaseException]" = []  # exceptions raised by the script
        self.stopping = threading.Event()
        self._threads: "list[threading.Thread]" = []
        self._accepter = threading.Thread(target=self._accept_loop, daemon=True, name="scripted-accept")

    def start(self) -> "ScriptedServer":
        self._accepter.start()
        return self

    def _accept_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections.append(conn)
            thread = threading.Thread(target=self._run_script, args=(conn,), daemon=True, name="scripted-conn")
            self._threads.append(thread)
            thread.start()

    def _run_script(self, conn: socket.socket) -> None:
        try:
            self.script(self, conn)
        except OSError:
            pass  # the peer or stop() closed the socket under the script: not a script error
        except BaseException as exc:  # noqa: BLE001 - recorded so a test can assert there were none
            self.errors.append(exc)

    # ------------------------------------------------------------ helpers for scripts and tests
    def read_until_closed(self, conn: socket.socket, on_data: Optional[Callable[[bytes], None]] = None) -> None:
        conn.settimeout(0.05)
        while not self.stopping.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.received += data
            if on_data is not None:
                on_data(data)

    def send_last(self, data: bytes) -> None:
        """Send bytes to the most recent connection (a late reply)."""
        self.connections[-1].sendall(data)

    def wait_for_connection(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connections:
                return True
            time.sleep(0.005)
        return bool(self.connections)

    def wait_for_bytes(self, count: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= count:
                return True
            time.sleep(0.005)
        return len(self.received) >= count

    def stop(self) -> None:
        self.stopping.set()
        for conn in list(self.connections):
            try:
                conn.close()
            except OSError:
                pass
        try:
            self.listener.close()
        except OSError:
            pass
        self._accepter.join(timeout=2.0)
        for thread in self._threads:
            thread.join(timeout=2.0)


@pytest.fixture
def scripted():
    """Factory: scripted(script) -> started ScriptedServer; every server is stopped afterwards."""
    servers = []

    def make(script: Optional[Callable] = None) -> ScriptedServer:
        server = ScriptedServer(script).start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()
