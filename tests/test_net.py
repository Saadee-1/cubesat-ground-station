"""Tests for groundstation.net using a tiny raw socket server (no MockServer)."""
import random
import socket
import struct
import threading
import time
import types

import pytest

from groundstation.net import (
    ClientError, ConnState, ParseFailure, Received, Sent, StateChanged, StreamProblem,
    TcpClient, describe_words)
from groundstation import net as net_module
from groundstation.cortex import parse_tm_message
from groundstation.protocol import POSTAMBLE_WORD, START_WORD, ProtocolError, build_message

MONITORING_REQUEST = bytes.fromhex("499602d20000001c00000000000000000000000000000007b669fd2e")


def good_message(flow=0, tag=7):
    """A 28-byte message built with struct only."""
    return struct.pack(">7i", START_WORD, 28, flow, 0, 0, tag, POSTAMBLE_WORD)


def wait_for(predicate, timeout=3.0):
    """Poll every 5 ms until predicate() is true; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Collector:
    """Drains a client's queue into a list so tests can look at everything seen."""

    def __init__(self, client):
        self.client = client
        self.events = []

    def pump(self):
        self.events += self.client.drain()

    def wait(self, predicate, timeout=3.0):
        def check():
            self.pump()
            return predicate(self.events)
        return wait_for(check, timeout)

    def of(self, kind):
        self.pump()
        return [e for e in self.events if isinstance(e, kind)]

    def states(self):
        return [e.state for e in self.of(StateChanged)]


class RawServer:
    """One-connection raw TCP server; `behaviour(conn)` runs on its own thread."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda conn: self.release.wait(10))
        self.release = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.conn, _ = self.listener.accept()
            self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.behaviour(self.conn)
        except OSError:
            pass

    def close(self):
        self.release.set()
        for sock in (self.conn, self.listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.thread.join(timeout=2.0)


@pytest.fixture
def make():
    """Factory for (client, collector, server); everything is closed at test end."""
    created = []

    def factory(behaviour=None, parse=lambda msg: msg, **kwargs):
        server = RawServer(behaviour)
        client = TcpClient("test", "127.0.0.1", server.port, parse, **kwargs)
        created.append((client, server))
        return client, Collector(client), server

    yield factory
    for client, server in created:
        client.disconnect()
        server.close()


def connect_and_wait(client):
    client.connect()
    assert wait_for(lambda: client.state == ConnState.CONNECTED)


def test_connect_disconnect_event_order(make):
    client, col, _ = make()
    client.connect()
    first = client.events.get_nowait()  # queued before connect() returned
    assert first == StateChanged(ConnState.CONNECTING, first.detail)
    client.events.put(first)
    assert wait_for(lambda: client.state == ConnState.CONNECTED)
    client.disconnect()
    assert col.states() == [ConnState.CONNECTING, ConnState.CONNECTED, ConnState.DISCONNECTED]
    assert col.of(StateChanged)[-1].detail == "closed by user"
    client.disconnect()  # idempotent: no second event
    assert col.of(StateChanged)[-1].state == ConnState.DISCONNECTED
    assert col.states().count(ConnState.DISCONNECTED) == 1


def test_connect_to_closed_port_ends_in_error(make):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    client = TcpClient("test", "127.0.0.1", port, lambda m: m, connect_timeout=1.0)
    started = time.monotonic()
    client.connect()
    try:
        assert wait_for(lambda: client.state == ConnState.ERROR, timeout=2.0)
        assert time.monotonic() - started < 2.0
        errors = [e for e in client.drain() if isinstance(e, StateChanged) and e.state == ConnState.ERROR]
        assert len(errors) == 1 and errors[0].detail
    finally:
        client.disconnect()


def test_wrong_state_calls_raise(make):
    client, _, _ = make()
    with pytest.raises(ClientError):
        client.send(b"1234")  # not connected
    client.connect()
    with pytest.raises(ClientError):
        client.connect()  # already connecting or connected
    assert wait_for(lambda: client.state == ConnState.CONNECTED)
    with pytest.raises(ClientError):
        client.configure("127.0.0.1", 1234)
    client.disconnect()
    client.configure("127.0.0.1", 1234)  # allowed again
    assert client.port == 1234


@pytest.mark.parametrize("host, port", [("", 80), ("  ", 80), ("h", 0), ("h", 65536), ("h", "80")])
def test_configure_validates(make, host, port):
    client, _, _ = make()
    with pytest.raises(ClientError):
        client.configure(host, port)


def test_send_emits_sent_event_and_reaches_server(make):
    got = []

    def behaviour(conn):
        conn.settimeout(2.0)
        got.append(conn.recv(100))

    client, col, _ = make(behaviour)
    connect_and_wait(client)
    client.send(b"abcd", "test send", 5)
    assert wait_for(lambda: got)
    assert got[0] == b"abcd"
    sent = col.of(Sent)
    assert sent and sent[0].raw == b"abcd" and sent[0].description == "test send" and sent[0].tag == 5


def test_peer_close_gives_disconnected(make):
    client, col, _ = make(lambda conn: conn.close())
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, StateChanged) and e.state == ConnState.DISCONNECTED
                                   for e in ev))
    last = col.of(StateChanged)[-1]
    assert (last.state, last.detail) == (ConnState.DISCONNECTED, "closed by peer")
    assert client.state == ConnState.DISCONNECTED


def test_one_byte_sends_deliver_message_once(make):
    def behaviour(conn):
        for i in range(len(MONITORING_REQUEST)):
            conn.sendall(MONITORING_REQUEST[i:i + 1])
        threading.Event().wait(0.2)

    client, col, _ = make(behaviour)
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, Received) for e in ev))
    col.wait(lambda ev: False, timeout=0.2)  # give a duplicate a chance to show up
    received = col.of(Received)
    assert len(received) == 1 and received[0].message.raw == MONITORING_REQUEST
    assert received[0].at > 0


def test_garbage_before_message_gives_stream_problem(make):
    client, col, _ = make(lambda conn: conn.sendall(bytes([0, 1, 2]) + MONITORING_REQUEST))
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, Received) for e in ev))
    assert col.of(Received)[0].message.raw == MONITORING_REQUEST
    problems = col.of(StreamProblem)
    assert problems and problems[0].error.reason == "garbage before start"


def test_parse_failure_keeps_connection_usable(make):
    def parse(msg):
        if msg.flow_id == 7:
            raise ProtocolError("flow 7 not wanted")
        return msg.flow_id

    client, col, _ = make(lambda conn: conn.sendall(good_message(flow=7) + good_message(flow=0)),
                          parse=parse)
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, Received) for e in ev))
    failures = col.of(ParseFailure)
    assert len(failures) == 1 and failures[0].error == "flow 7 not wanted"
    assert failures[0].message.flow_id == 7
    assert col.of(Received)[0].parsed == 0
    assert client.state == ConnState.CONNECTED


def test_stall_is_resynchronised(make):
    stray = struct.pack(">iI", START_WORD, 1_000_000)
    client, col, _ = make(lambda conn: conn.sendall(stray + good_message() * 20), stall_timeout=0.3)
    client.connect()
    assert col.wait(lambda ev: sum(isinstance(e, Received) for e in ev) >= 20, timeout=3.0)
    assert len(col.of(Received)) == 20
    assert any(p.error.reason == "resync forced" for p in col.of(StreamProblem))


def test_queue_pressure_drops_bulk_events_but_not_states(make):
    client, col, _ = make(lambda conn: conn.sendall(good_message() * 50), max_queued_events=5)
    client.connect()
    assert wait_for(lambda: client.dropped_events > 0)
    assert wait_for(lambda: client.dropped_events >= 45)  # 3 Received fit after 2 state events
    client.disconnect()  # queue is full, the state event must still arrive
    assert col.states() == [ConnState.CONNECTING, ConnState.CONNECTED, ConnState.DISCONNECTED]
    assert client.dropped_events > 0


def test_disconnect_stops_worker_and_threads(make):
    base = threading.active_count()
    client, _, server = make()
    connect_and_wait(client)
    worker = client._thread
    started = time.monotonic()
    client.disconnect()
    assert time.monotonic() - started < 2.0
    assert not worker.is_alive()
    server.close()
    assert wait_for(lambda: threading.active_count() == base)


def test_disconnect_while_connecting_is_silent_afterwards(make):
    client, col, _ = make()
    client.connect()
    client.disconnect()
    before = col.states()
    assert before[0] == ConnState.CONNECTING and before[-1] == ConnState.DISCONNECTED
    col.wait(lambda ev: False, timeout=0.3)  # a late worker must not add events
    assert col.states() == before
    assert client.state == ConnState.DISCONNECTED


def test_describe_words_known_message():
    raw = struct.pack(">5i", START_WORD, 20, 0, -1, POSTAMBLE_WORD)
    assert describe_words(raw) == [
        "[ 0] 0x499602D2  1234567890",
        "[ 1] 0x00000014  20",
        "[ 2] 0x00000000  0",
        "[ 3] 0xFFFFFFFF  -1",
        "[ 4] 0xB669FD2E  -1234567890",
    ]


def test_describe_words_with_tail_and_empty():
    raw = struct.pack(">3i", 1, 2, -3) + bytes([0xAA, 0xBB])
    assert describe_words(raw) == [
        "[ 0] 0x00000001  1", "[ 1] 0x00000002  2", "[ 2] 0xFFFFFFFD  -3", "[ 3] raw 0xAABB"]
    assert describe_words(b"") == []


def _tm_message(seq):
    return build_message(0, [0, 0, seq, 0, 1, 0, 0, 10, 16, 0, 0, 0, 0,
                             0x00010203, 0x04050607, 0x08090000])


class VirtualClock:
    """The clock the client's stall timer reads (net.time.monotonic), moved only by the test.

    With the real clock a test sender that is starved for longer than the stall timeout makes the
    client see a genuinely stalled message and resynchronise, which is correct client behaviour but
    makes the test depend on scheduler timing. Here "time" passes only when the sender sends, so CPU
    load cannot change the outcome, while a message that stays pending across enough virtual time
    still trips the timer.
    """

    def __init__(self):
        self.now = 1000.0
        self.reads = 0       # how often the client has read the clock (see wait_for_pending_note)

    def monotonic(self):
        self.reads += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


def wait_for_pending_note(clock, timeout=5.0):
    """Wait until the client has noted when a partial message began.

    While nothing is pending the client never reads the clock, and no message completes in these tests, so
    the first read IS that note (pending_since = time.monotonic()). Moving the virtual time before it
    would make the message look young and the test would then depend on how soon the reader thread runs."""
    return wait_for(lambda: clock.reads >= 1, timeout=timeout)


@pytest.fixture
def virtual_clock(monkeypatch):
    clock = VirtualClock()
    monkeypatch.setattr(net_module, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=time.sleep))
    return clock


def test_slow_but_steady_stream_is_never_resynced(virtual_clock):
    """Every chunk ends inside the next message (8 to 71 bytes of it already in), so a message is always pending, but each one completes within a few chunks. The stall timer must restart whenever a message completes: about 200 chunks of 4 ms is 0.8 s of virtual time, four times the 0.2 s timeout."""
    total, spacing = 300, 0.004
    stream = b"".join(_tm_message(i) for i in range(1, total + 1))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    rng = random.Random(5)

    def serve():
        conn, _ = listener.accept()
        pos = 0
        while pos < len(stream):
            target = (pos // 80 + rng.randrange(0, 4)) * 80 + rng.randrange(8, 72)
            target = min(max(target, pos + 1), len(stream))
            virtual_clock.advance(spacing)   # the 4 ms between chunks, in virtual time
            conn.sendall(stream[pos:target])
            pos = target
            time.sleep(0.0005)               # only to let the reader run; never part of the proof
        time.sleep(0.5)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = TcpClient("t", "127.0.0.1", port, parse_tm_message,
                       stall_timeout=0.2, max_queued_events=10 ** 6)
    client.connect()
    got, problems = [], []
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline and len(got) < total:
            for ev in client.drain():
                if isinstance(ev, Received):
                    got.append(ev.parsed.sequence)
                elif isinstance(ev, StreamProblem):
                    problems.append(ev.error.reason)
            time.sleep(0.005)
    finally:
        client.disconnect()
        listener.close()
        thread.join(timeout=2)
    assert virtual_clock.now - 1000.0 >= 3 * 0.2, "the stream must outlast the timeout several times over"
    assert got == list(range(1, total + 1))
    assert problems == []


def test_a_message_that_stays_incomplete_for_longer_than_the_timeout_is_resynced(virtual_clock):
    """The other half of the proof: the same virtual clock DOES trip the timer for a real stall."""
    first = _tm_message(1)
    second = _tm_message(2)
    release = threading.Event()

    def behaviour(conn):
        conn.sendall(first[:20])           # the start of a message, then silence
        release.wait(10)
        conn.sendall(second)

    server = RawServer(behaviour)
    client = TcpClient("t", "127.0.0.1", server.port, parse_tm_message, stall_timeout=0.2)
    col = Collector(client)
    try:
        client.connect()
        assert wait_for(lambda: client.state == ConnState.CONNECTED)
        assert wait_for_pending_note(virtual_clock)
        col.wait(lambda ev: False, timeout=0.5)       # real time passes, virtual time does not
        assert not any(p.error.reason == "resync forced" for p in col.of(StreamProblem))
        virtual_clock.advance(0.25)                   # the message has now been pending for 0.25 s
        assert col.wait(lambda ev: any(isinstance(e, StreamProblem) and e.error.reason == "resync forced"
                                       for e in ev), timeout=5)
        release.set()
        assert col.wait(lambda ev: any(isinstance(e, Received) for e in ev), timeout=5)
        assert [e.parsed.sequence for e in col.of(Received)] == [2]   # the stream recovered
    finally:
        release.set()
        client.disconnect()
        server.close()


def test_a_pause_shorter_than_the_timeout_is_not_a_stall(virtual_clock):
    first = _tm_message(1)
    release = threading.Event()

    def behaviour(conn):
        conn.sendall(first[:20])
        release.wait(10)
        conn.sendall(first[20:])

    server = RawServer(behaviour)
    client = TcpClient("t", "127.0.0.1", server.port, parse_tm_message, stall_timeout=0.2)
    col = Collector(client)
    try:
        client.connect()
        assert wait_for(lambda: client.state == ConnState.CONNECTED)
        assert wait_for_pending_note(virtual_clock)
        virtual_clock.advance(0.15)                   # 0.15 s < 0.2 s
        col.wait(lambda ev: False, timeout=0.6)       # three poll cycles of the reader
        assert not any(p.error.reason == "resync forced" for p in col.of(StreamProblem))
        release.set()
        assert col.wait(lambda ev: any(isinstance(e, Received) for e in ev), timeout=5)
        assert [e.parsed.sequence for e in col.of(Received)] == [1] and not col.of(StreamProblem)
    finally:
        release.set()
        client.disconnect()
        server.close()


def test_peer_reset_gives_disconnected_with_reset_detail(make):
    def behaviour(conn):
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        time.sleep(0.1)  # let the client block in recv first
        conn.close()     # linger 0 turns close() into a TCP reset

    client, col, _ = make(behaviour)
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, StateChanged) and e.state == ConnState.DISCONNECTED
                                   for e in ev))
    last = col.of(StateChanged)[-1]
    assert last.state == ConnState.DISCONNECTED
    assert last.detail.startswith("connection reset by peer")


def test_send_oserror_goes_to_error_and_raises_client_error(make):
    client, col, _ = make()
    connect_and_wait(client)
    real = client._sock

    class BrokenSocket:
        def sendall(self, data):
            raise BrokenPipeError("simulated")

        def shutdown(self, how):
            real.shutdown(how)

        def close(self):
            real.close()

    with client._lock:
        client._sock = BrokenSocket()
    with pytest.raises(ClientError, match="send failed"):
        client.send(b"abcd", "will fail", 1)
    assert client.state == ConnState.ERROR
    assert col.of(Sent) == []
    errors = [e for e in col.of(StateChanged) if e.state == ConnState.ERROR]
    assert len(errors) == 1 and errors[0].detail.startswith("send failed")


def test_unexpected_parse_exception_goes_to_error_not_silent_death(make):
    def parse(msg):
        raise RuntimeError("boom")

    client, col, _ = make(lambda conn: conn.sendall(good_message()), parse=parse)
    client.connect()
    assert col.wait(lambda ev: any(isinstance(e, StateChanged) and e.state == ConnState.ERROR
                                   for e in ev))
    errors = [e for e in col.of(StateChanged) if e.state == ConnState.ERROR]
    assert len(errors) == 1 and errors[0].detail == "internal error: RuntimeError: boom"
    assert client.state == ConnState.ERROR
    assert wait_for(lambda: not client._thread.is_alive())


def test_connect_uses_connect_timeout(monkeypatch):
    seen = {}

    def fake_create_connection(address, timeout=None, *args, **kwargs):
        seen["call"] = (address, timeout)
        raise OSError("no route")

    monkeypatch.setattr(net_module.socket, "create_connection", fake_create_connection)
    client = TcpClient("t", "127.0.0.1", 9, lambda m: m, connect_timeout=1.5)
    client.connect()
    try:
        assert wait_for(lambda: client.state == ConnState.ERROR)
        assert seen["call"] == (("127.0.0.1", 9), 1.5)
        assert [e.detail for e in client.drain() if isinstance(e, StateChanged)][-1].startswith("connect failed")
    finally:
        client.disconnect()


def test_queue_pressure_exact_accounting(make):
    client, col, _ = make(lambda conn: conn.sendall(good_message() * 50), max_queued_events=5)
    client.connect()
    assert wait_for(lambda: client.dropped_events == 47)  # 2 state events + 3 messages fit
    assert client.events.qsize() == 5
    client.disconnect()
    assert len(col.of(Received)) == 3
    assert col.states() == [ConnState.CONNECTING, ConnState.CONNECTED, ConnState.DISCONNECTED]
