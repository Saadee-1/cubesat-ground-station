"""Tests for groundstation.mockserver itself."""
import random
import socket
import time

import pytest

from groundstation.clients import MonitoringClient, TelemetryClient
from groundstation.cop1 import MonitoringReport
from groundstation.cortex import TelemetryMessage
from groundstation.mockserver import MockServer
from groundstation.net import ConnState, Received
from groundstation.protocol import build_message


def wait_for(predicate, timeout=3.0):
    """Poll every 5 ms until predicate() is true; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Seen:
    """Accumulates a client's parsed events."""

    def __init__(self, client):
        self.client = client
        self.parsed = []

    def count(self, kind):
        for e in self.client.drain():
            if isinstance(e, Received):
                self.parsed.append(e.parsed)
        return sum(isinstance(p, kind) for p in self.parsed)


@pytest.fixture
def env():
    mocks, clients, socks = [], [], []

    class Env:
        def mock(self, service, **kwargs):
            server = MockServer(service, **kwargs)
            server.start()
            mocks.append(server)
            return server

        def client(self, cls, mock):
            client = cls(port=mock.port)
            clients.append(client)
            client.connect()
            assert wait_for(lambda: client.state == ConnState.CONNECTED)
            return client, Seen(client)

        def raw(self, mock):
            sock = socket.create_connection(("127.0.0.1", mock.port), timeout=2.0)
            socks.append(sock)
            return sock

    yield Env()
    for sock in socks:
        sock.close()
    for client in clients:
        client.disconnect()
    for server in mocks:
        server.stop()


def test_port_is_nonzero_only_after_start():
    server = MockServer("monitoring")
    assert server.port == 0
    server.start()
    try:
        assert server.port != 0
    finally:
        server.stop()


def test_unknown_service_is_rejected():
    with pytest.raises(ValueError):
        MockServer("bogus")


def test_stop_is_idempotent_and_fast_with_a_connected_client(env):
    mock = env.mock("telemetry")
    client, seen = env.client(TelemetryClient, mock)
    client.start()
    assert wait_for(lambda: seen.count(TelemetryMessage) >= 1)
    started = time.monotonic()
    mock.stop()
    mock.stop()
    assert time.monotonic() - started < 2.0


def test_stop_without_start_is_fine():
    server = MockServer("control")
    server.stop()
    server.stop()


def test_survives_garbage_and_instant_disconnects(env):
    mock = env.mock("monitoring")
    rng = random.Random(5)
    noisy = env.raw(mock)
    noisy.sendall(rng.randbytes(1000))
    noisy.close()
    for _ in range(3):
        env.raw(mock).close()  # connects and immediately closes
    client, seen = env.client(MonitoringClient, mock)  # the server still works
    client.request()
    assert wait_for(lambda: seen.count(MonitoringReport) >= 1)


def test_malformed_but_framed_bodies_get_negative_responses(env):
    mock = env.mock("monitoring")
    sock = env.raw(mock)
    sock.settimeout(2.0)
    sock.sendall(build_message(0, []))            # empty body
    sock.sendall(build_message(0, [9, 9]))         # wrong body
    sock.sendall(build_message(77, [0, 0, 1]))     # wrong flow id
    data = b""
    while len(data) < 60:
        data += sock.recv(100)
    assert data == build_message(0, [-1]) * 3
    assert len(mock.received) == 3


def test_received_lists_framed_messages(env):
    mock = env.mock("monitoring")
    client, seen = env.client(MonitoringClient, mock)
    tag = client.request()
    assert wait_for(lambda: len(mock.received) == 1)
    message = mock.received[0]
    assert message.body == (0, 0, tag)
    mock.received.clear()  # it is a copy: the server's own list is unaffected
    assert len(mock.received) == 1


def test_two_simultaneous_telemetry_clients_both_receive(env):
    mock = env.mock("telemetry")
    first, seen_a = env.client(TelemetryClient, mock)
    second, seen_b = env.client(TelemetryClient, mock)
    first.start()
    second.start()
    assert wait_for(lambda: seen_a.count(TelemetryMessage) >= 3 and seen_b.count(TelemetryMessage) >= 3)
    # Each connection has its own sequence counter, starting at 1.
    assert seen_a.parsed[0].sequence == 1 and seen_b.parsed[0].sequence == 1

