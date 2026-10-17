"""Hostile servers over real sockets, and well-formed messages nobody asked for.

Every test goes through the real TcpClient (and, for the unexpected messages, the real tabs). The scripted server does
the misbehaving. Required everywhere: no exception in any thread, a sensible final state, and a
client that can be reused after Disconnect and Connect.
"""
import struct
import threading
import time
from pathlib import Path

import pytest

from groundstation import guibase, net
from groundstation.clients import ControlClient, MonitoringClient, TelecommandClient, TelemetryClient
from groundstation.config import load_config
from groundstation.cop1 import (AcceptReject, ControlAck, MonitoringReport, NegativeResponse, SegmentEcho,
                                UnknownResponse)
from groundstation.cortex import TelemetryStopAck, UnknownTmMessage
from groundstation.guibase import HINT
from groundstation.mockserver import MockServer
from groundstation.net import ConnState, ParseFailure, Received, StateChanged, StreamProblem
from groundstation.protocol import MAX_MESSAGE_BYTES, START_BYTES, build_message
from groundstation.source import SimFrameSource
from groundstation.tab_control import ControlTab
from groundstation.tab_monitoring import MonitoringTab
from groundstation.tab_telecommand import TelecommandTab
from groundstation.tab_telemetry import TelemetryTab
from groundstation.tm import TelemetryPipeline
from gui_util import gui_env, has_log_line, settle, wait_until  # noqa: F401
from scripted_server import scripted  # noqa: F401

PARAMS = load_config(str(Path(guibase.__file__).parent / "samples" / "sample_config_10.json"))

# service -> (client class, probe that sends one request, a valid reply the server could send,
#             the type that reply must parse to)
SERVICES = {
    "control": (ControlClient, lambda c: c.set_parameters({1: 1}), [1, 0, 7, 0], ControlAck),
    "monitoring": (MonitoringClient, lambda c: c.request(), [-1], NegativeResponse),
    "telecommand": (TelecommandClient, lambda c: c.send_nop(0), [-1], NegativeResponse),
    "telemetry": (TelemetryClient, lambda c: c.stop(0), [0], TelemetryStopAck),
}
FLOWS = {"control": 1, "monitoring": 0, "telecommand": 0, "telemetry": 0}


@pytest.fixture
def thread_errors():
    """Collect every unhandled exception of every thread; the test asserts the list is empty."""
    errors = []
    previous = threading.excepthook
    threading.excepthook = lambda args: errors.append(args)
    yield errors
    threading.excepthook = previous


def wait_state(client, wanted, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.state in wanted:
            return True
        time.sleep(0.005)
    return client.state in wanted


def drain_for(client, seconds):
    """Everything the client queued during `seconds`."""
    events, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        events += client.drain()
        time.sleep(0.01)
    return events + client.drain()


def drain_until(client, done, timeout=5.0, extra=0.1):
    """Collect events until `done(events)` is true (or `timeout`), then `extra` seconds more for stragglers.

    Waits for what must happen instead of guessing how long it takes: a reader thread that is starved
    by a loaded machine only makes this slower, never wrong. `extra` is only for negative checks."""
    events, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline and not done(events):
        events += client.drain()
        time.sleep(0.005)
    return events + drain_for(client, extra)


def assert_reusable(client, service, errors):
    """Disconnect, point the client at a good mock server, connect, and get a real reply."""
    client.disconnect()
    assert wait_state(client, (ConnState.DISCONNECTED,))
    mock = MockServer(service)
    mock.start()
    try:
        client.configure("127.0.0.1", mock.port)
        client.connect()
        assert wait_state(client, (ConnState.CONNECTED,)), client.state
        SERVICES[service][1](client)
        deadline, replies = time.monotonic() + 3.0, []
        while time.monotonic() < deadline and not replies:
            replies = [e for e in client.drain() if isinstance(e, Received)]
            time.sleep(0.01)
        assert replies, "no reply from the good server after the hostile one"
    finally:
        client.disconnect()
        mock.stop()
    assert errors == []


def valid_reply(service):
    return build_message(FLOWS[service], SERVICES[service][2])


# ------------------------------------------------------------ scripted hostile servers
@pytest.mark.parametrize("service", SERVICES)
def test_server_closes_immediately_after_accept(service, scripted, thread_errors):
    server = scripted(lambda srv, conn: conn.close())
    client = SERVICES[service][0](port=server.port)
    client.connect()
    assert server.wait_for_connection()
    assert wait_state(client, (ConnState.DISCONNECTED, ConnState.ERROR)), client.state
    # the state is set a moment before its event is queued, so wait for the event itself
    events = drain_until(client, lambda ev: any(isinstance(e, StateChanged) and e.state in (
        ConnState.DISCONNECTED, ConnState.ERROR) for e in ev))
    states = [e.state for e in events if isinstance(e, StateChanged)]
    assert states and states[-1] in (ConnState.DISCONNECTED, ConnState.ERROR)
    assert_reusable(client, service, thread_errors)
    assert server.errors == []


@pytest.mark.parametrize("service", SERVICES)
def test_valid_header_then_silence_leaves_the_client_connected_and_usable(service, scripted, thread_errors):
    header = START_BYTES + struct.pack(">II", 268, FLOWS[service])  # a plausible start of a long message

    def script(srv, conn):
        conn.sendall(header)
        srv.read_until_closed(conn)

    server = scripted(script)
    client = SERVICES[service][0](port=server.port)
    client.connect()
    assert wait_state(client, (ConnState.CONNECTED,))
    drain_for(client, 0.3)
    assert client.state == ConnState.CONNECTED        # waiting for the rest is not an error
    SERVICES[service][1](client)                      # requests can still be sent
    assert server.wait_for_bytes(16)
    assert_reusable(client, service, thread_errors)
    assert server.errors == []


@pytest.mark.parametrize("service", SERVICES)
def test_start_word_with_a_size_above_the_cap_is_skipped_and_the_next_message_arrives(
        service, scripted, thread_errors):
    hostile = START_BYTES + struct.pack(">I", MAX_MESSAGE_BYTES + 4)

    def script(srv, conn):
        conn.sendall(hostile + valid_reply(service))
        srv.read_until_closed(conn)

    server = scripted(script)
    client = SERVICES[service][0](port=server.port)
    client.connect()
    assert wait_state(client, (ConnState.CONNECTED,))
    # wait for the valid message that follows the hostile bytes; the stream problem is queued before it
    events = drain_until(client, lambda ev: any(isinstance(e, Received) for e in ev))
    problems = [e for e in events if isinstance(e, StreamProblem)]
    assert any(e.error.reason == "invalid size" for e in problems), events
    received = [e for e in events if isinstance(e, Received)]
    assert len(received) == 1 and isinstance(received[0].parsed, SERVICES[service][3])
    assert client.state == ConnState.CONNECTED
    assert_reusable(client, service, thread_errors)


def wait_for_quiescence(client, server, at_least, quiet=0.5, deadline=15.0):
    """Wait until the server has sent everything, at least `at_least` events were produced, and the
    number of events (queued plus dropped) has not changed for `quiet` seconds. Nothing is drained."""
    end, last, last_change = time.monotonic() + deadline, None, time.monotonic()
    while time.monotonic() < end:
        total = client.events.qsize() + client.dropped_events
        if total != last:
            last, last_change = total, time.monotonic()
        elif server.done and total >= at_least and time.monotonic() - last_change >= quiet:
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("chunk", [64, 4096, 65536])
@pytest.mark.parametrize("service", SERVICES)
def test_a_megabyte_of_garbage_is_bounded_and_survivable_whatever_the_segmentation(
        service, chunk, scripted, thread_errors):
    """The overflow is made certain by NOT draining while the server sends, with a small cap.

    The count of events is not a property of the code: it depends on how the operating system cuts
    the stream into recv() calls. What the code does guarantee (net._run, FrameReader._drain):
    every recv() returns at most _RECV_BYTES; garbage without a start word is discarded down to the
    last 3 bytes in ONE StreamProblem per feed; so one StreamProblem discards at most _RECV_BYTES
    bytes and all but 3 of the bytes are discarded in the end. Hence the number of StreamProblems
    (queued or dropped) is at least ceil((total - 3) / _RECV_BYTES), for any segmentation."""
    total, cap = 1024 * 1024, 20
    guaranteed = -(-(total - 3) // net._RECV_BYTES)

    def script(srv, conn):
        time.sleep(0.1)
        data = bytes([0x11]) * chunk
        for _ in range(total // chunk):
            conn.sendall(data)
        srv.done = True
        srv.read_until_closed(conn)

    server = scripted(script)
    server.done = False
    client = SERVICES[service][0](port=server.port)
    client.max_queued_events = cap
    client.connect()
    assert wait_state(client, (ConnState.CONNECTED,))
    assert wait_for_quiescence(client, server, at_least=guaranteed), (
        server.done, client.events.qsize(), client.dropped_events, guaranteed)
    queued = client.events.qsize()                                   # nothing was drained, so this is the peak
    collected = client.drain()
    state_events = sum(isinstance(e, StateChanged) for e in collected)
    assert queued == len(collected)
    assert client.dropped_events > 0                                 # the cap was hit and the overflow counted
    assert queued <= cap + state_events                              # capped; only state events are exempt
    problems = sum(isinstance(e, StreamProblem) for e in collected)
    assert problems + client.dropped_events >= guaranteed            # the flood was really read, not lost
    kinds = {type(e) for e in collected}
    assert kinds <= {StateChanged, StreamProblem}, kinds             # garbage never becomes a message
    assert client.state == ConnState.CONNECTED
    client.max_queued_events = 20000
    assert_reusable(client, service, thread_errors)
    assert server.errors == []


@pytest.mark.parametrize("service", SERVICES)
def test_a_flood_of_valid_messages_the_tab_never_asked_for_does_not_hang_the_client(
        service, scripted, thread_errors):
    """5000 well-formed replies back to back: all are delivered or counted as dropped, nothing breaks."""
    reply = valid_reply(service)

    def script(srv, conn):
        time.sleep(0.1)
        conn.sendall(reply * 5000)
        srv.done = True
        srv.read_until_closed(conn)

    server = scripted(script)
    server.done = False
    client = SERVICES[service][0](port=server.port)
    client.connect()
    assert wait_state(client, (ConnState.CONNECTED,))
    deadline, received = time.monotonic() + 10, 0
    while time.monotonic() < deadline:
        received += sum(isinstance(e, Received) for e in client.drain(max_events=2000))
        if server.done and received + client.dropped_events >= 5000:
            break
        time.sleep(0.005)
    assert received + client.dropped_events == 5000, (received, client.dropped_events)
    assert_reusable(client, service, thread_errors)


# ------------------------------------------------------------ the GUI under a flood
def test_the_gui_pump_survives_garbage_and_keeps_running(gui_env, scripted, tk_root, thread_errors):
    """How many stream problems the tab sees depends on how the operating system cuts the stream into
    recv() calls and on the speed of the pump, so only what the code guarantees is asserted: at least
    one problem was shown (as an error with the hint), the pump is still scheduled, the client is still
    connected. That the log never exceeds MAX_LOG_LINES is proven, with an exact number of lines, by
    tests/test_guibase.py::test_log_is_bounded_and_keeps_the_newest_line."""
    total, chunk = 256 * 1024, 64

    def script(srv, conn):
        time.sleep(0.1)
        data = bytes([0x11]) * chunk
        for _ in range(total // chunk):
            conn.sendall(data)
        srv.done = True
        srv.read_until_closed(conn)

    server = scripted(script)
    server.done = False
    tab = gui_env.make_tab(ControlTab, ControlClient, port=server.port, connect=True, pump_ms=50)
    started = time.monotonic()
    assert wait_until(tk_root, lambda: server.done and tab.error_count >= 1 and tab.client.events.empty(),
                      timeout=30)
    assert time.monotonic() - started < 30
    assert tab.log.line_count() <= guibase.MAX_LOG_LINES
    assert tab.error_count >= 1 and tab._after_id is not None         # the pump kept running
    assert tab.client.state == ConnState.CONNECTED
    assert has_log_line(tab, HINT)
    assert thread_errors == []


# ------------------------------------------------------------ well-formed but unexpected messages
def telemetry_frame_message(sequence=1):
    body = [111, 222, sequence, 0, 1, 0, 0, 8, 16, 0, 0, 0, 0, 0x01020304, 0x05060708]
    return build_message(0, body)


def unexpected_batch():
    config, status = [0] * 40, [0] * 20
    return [
        telemetry_frame_message(1),                                      # telemetry data on any other connection
        build_message(0, [0, 0, 999, *config, *status]),                 # a monitoring report nobody asked for
        build_message(1, [1, 0, 4242, 0]),                               # a control ack with a tag nobody sent
        build_message(0, [0, 30, 1, 4242, 0]),                           # an accept with a tag nobody sent
        build_message(0, [4, 30, 4242, 9, 2, 0x01020000]),               # an echo nobody asked for
        build_message(0, [99, 1, 2, 3]),                                 # an unknown layout
        build_message(0, [2, 0, 1, 5]),                                  # an alert
        build_message(0, [6, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),  # additional information
    ]


def sending_script(batch):
    def script(srv, conn):
        time.sleep(0.15)
        for raw in batch:
            conn.sendall(raw)
        srv.done = True
        srv.read_until_closed(conn)
    return script


def make_batch_server(scripted):
    server = scripted(sending_script(unexpected_batch()))
    server.done = False
    return server


def pump_until_batch_handled(tab, tk_root, server):
    assert wait_until(tk_root, lambda: server.done and tab.received_count + tab.client.dropped_events >= 8, timeout=5)
    settle(tk_root, 0.1)


def test_control_tab_logs_unexpected_messages_and_keeps_its_own_requests(gui_env, scripted, tk_root, thread_errors):
    server = make_batch_server(scripted)
    tab = gui_env.make_tab(ControlTab, ControlClient, port=server.port, connect=True)
    tab.select(1)
    guibase.set_entry(tab.value_entry, "5")
    tab.stage_button.invoke()
    tab.send_button.invoke()
    sent_tag = max(tab.pending_tags)
    pump_until_batch_handled(tab, tk_root, server)
    assert tab._after_id is not None and tab.client.state == ConnState.CONNECTED
    assert has_log_line(tab, "tag 4242 not sent by this tab")
    assert tab.received_count == 8 and tab.error_count >= 1
    # By design (the FIFO guess) failure-shaped replies are attributed to the oldest request, so after
    # the unrecognised messages that request is no longer pending; the table must be consistent.
    assert tab.cell(1, "sent") in ("pending", "unrecognised reply")
    assert (sent_tag in tab.pending_tags) == (tab.cell(1, "sent") == "pending")
    assert tab.pending_due.keys() <= set(tab.pending_tags)
    assert thread_errors == []


def test_control_tab_a_real_ack_after_the_noise_is_still_understood(gui_env, scripted, tk_root, thread_errors):
    """Noise that carries a tag nobody sent leaves the one real request alone."""
    holder = {}

    def script(srv, conn):
        time.sleep(0.15)
        conn.sendall(build_message(1, [1, 0, 4242, 0]) * 3)               # three acks for tags nobody sent
        while not holder.get("tag") and not srv.stopping.is_set():
            time.sleep(0.01)
        conn.sendall(build_message(1, [1, 0, holder["tag"], 0]))          # then the real one
        srv.read_until_closed(conn)

    server = scripted(script)
    tab = gui_env.make_tab(ControlTab, ControlClient, port=server.port, connect=True)
    tab.select(1)
    guibase.set_entry(tab.value_entry, "5")
    tab.stage_button.invoke()
    tab.send_button.invoke()
    holder["tag"] = max(tab.pending_tags)
    assert wait_until(tk_root, lambda: tab.cell(1, "sent") == "ack 0", timeout=5)
    assert len([line for line in tab.log.lines() if "tag 4242 not sent by this tab" in line]) == 3
    assert thread_errors == []


def test_monitoring_tab_ignores_what_is_not_a_report_and_shows_a_real_one_after(
        gui_env, scripted, tk_root, thread_errors):
    server = make_batch_server(scripted)
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, port=server.port, connect=True)
    pump_until_batch_handled(tab, tk_root, server)
    # The batch contains one real-looking monitoring report (tag 999): it is shown, the rest is only logged.
    assert tab.report is not None and tab.report.tag == 999
    assert tab._after_id is not None and tab.client.state == ConnState.CONNECTED
    assert thread_errors == []


def test_telecommand_tab_unknown_tags_never_touch_its_rows(gui_env, scripted, tk_root, thread_errors):
    server = make_batch_server(scripted)
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient, port=server.port, connect=True)
    tab._add_row(1, "BD segment", "1", "5")
    tab._add_row(2, "BD segment", "1", "5")
    pump_until_batch_handled(tab, tk_root, server)
    assert has_log_line(tab, "tag 4242 not sent by this tab")
    assert tab._after_id is not None and tab.client.state == ConnState.CONNECTED
    assert len(tab.requests) == 2 and set(tab.requests) == {1, 2}
    # Several messages in the batch are failure-shaped for this service (a telemetry frame, a report, an
    # unknown layout). By design each one is attributed to the OLDEST pending request (the unverified
    # first-in-first-out guess), so rows may read "unrecognised reply"; every row must stay consistent.
    allowed = ("pending", "unrecognised reply")
    assert tab.status_of(1) in allowed and tab.status_of(2) in allowed
    assert tab.status_of(1) == "unrecognised reply"      # the oldest is hit first
    assert has_log_line(tab, "the unrecognised reply has no tag, so the oldest pending request")
    assert thread_errors == []


def test_telemetry_tab_survives_replies_of_other_services(gui_env, scripted, tk_root, thread_errors):
    server = make_batch_server(scripted)
    pipeline = TelemetryPipeline(SimFrameSource(PARAMS, rate_hz=50), PARAMS)
    tab = gui_env.make_tab(lambda f, c, s, pump_ms: TelemetryTab(f, c, s, pipeline, pump_ms=pump_ms),
                           TelemetryClient, port=server.port, connect=True)
    tab.set_source_mode("Network")
    pump_until_batch_handled(tab, tk_root, server)
    assert tab._after_id is not None and tab.client.state == ConnState.CONNECTED
    # The telemetry parser classifies by SIZE (68 bytes or more is telemetry), so on this
    # connection the real frame counts, the 268 byte report reads as a dummy message, and the 76 byte
    # additional-information message reads as a short frame. Nothing is forwarded twice and nothing breaks.
    assert tab.counters["received"] == 2 and tab.counters["forwarded"] == 2
    assert tab.counters["dummy"] == 1
    assert tab.stream_state == "idle"                                         # nothing was requested
    assert thread_errors == []
