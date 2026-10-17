"""Tests for groundstation.clients against MockServer (127.0.0.1, port 0)."""
import time
from pathlib import Path

import pytest

from groundstation import config, decode
from groundstation.clients import (ControlClient, MonitoringClient, TelecommandClient,
                                   TelemetryClient)
from groundstation.cop1 import (AcceptReject, ConfirmResponse, ControlAck, MonitoringReport,
                                NegativeResponse)
from groundstation.cortex import (TelemetryMessage, TelemetryNegativeAck, TelemetryStopAck,
                                  interpret_frame_check)
from groundstation.mockserver import MockFaults, MockServer
from groundstation.net import (ConnState, ParseFailure, Received, StateChanged, StreamProblem)
from groundstation.protocol import ProtocolOptions
from groundstation.protocol import build_message, to_i32

SAMPLE_10 = Path(__file__).resolve().parent.parent / "groundstation" / "samples" / "sample_config_10.json"


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

    def parsed(self, kind):
        """Parsed payloads of type `kind`, in arrival order."""
        self.pump()
        return [e.parsed for e in self.events if isinstance(e, Received) and isinstance(e.parsed, kind)]

    def of(self, kind):
        self.pump()
        return [e for e in self.events if isinstance(e, kind)]

    def wait_parsed(self, kind, count=1, timeout=3.0):
        return self.wait(lambda ev: sum(isinstance(e, Received) and isinstance(e.parsed, kind)
                                        for e in ev) >= count, timeout)


@pytest.fixture
def env():
    """Factory for started mocks and connected clients; everything is closed at the end."""
    mocks, clients = [], []

    class Env:
        def mock(self, service, **kwargs):
            server = MockServer(service, **kwargs)
            server.start()
            mocks.append(server)
            return server

        def client(self, cls, mock, **kwargs):
            client = cls(port=mock.port, **kwargs)
            clients.append(client)
            client.connect()
            assert wait_for(lambda: client.state == ConnState.CONNECTED), client.drain()
            return client, Collector(client)

    yield Env()
    for client in clients:
        client.disconnect()
    for server in mocks:
        server.stop()


# ------------------------------------------------------------ monitoring and control
def test_monitoring_request_gets_report_with_same_tag(env):
    client, col = env.client(MonitoringClient, env.mock("monitoring"))
    tag = client.request()
    assert col.wait_parsed(MonitoringReport)
    report = col.parsed(MonitoringReport)[0]
    assert report.tag == tag
    assert len(report.config) == 40 and len(report.status) == 20


def test_tags_increase_per_request(env):
    client, col = env.client(MonitoringClient, env.mock("monitoring"))
    tags = [client.request() for _ in range(3)]
    assert tags == [1, 2, 3]
    assert col.wait_parsed(MonitoringReport, 3)
    assert [r.tag for r in col.parsed(MonitoringReport)] == tags


def test_control_then_monitoring_shares_state(env):
    shared = {}
    control_mock = env.mock("control", shared_params=shared)
    monitoring_mock = env.mock("monitoring", shared_params=shared)
    control, ccol = env.client(ControlClient, control_mock)
    monitoring, mcol = env.client(MonitoringClient, monitoring_mock)
    tag = control.set_parameters({0: 1023, 3: 0x00010BFE})
    assert ccol.wait_parsed(ControlAck)
    ack = ccol.parsed(ControlAck)[0]
    assert ack.tag == tag
    assert control_mock.params == {0: 1023, 3: 0x00010BFE}
    monitoring.request()
    assert mcol.wait_parsed(MonitoringReport)
    report = mcol.parsed(MonitoringReport)[0]
    assert report.config_named()["Spacecraft identifier"] == 1023
    assert report.tm_port() == (1, 3070)


def test_control_invalid_parameter_id_is_negative(env):
    mock = env.mock("control")
    client, col = env.client(ControlClient, mock)
    client.set_parameters({0: 1})
    assert col.wait_parsed(ControlAck)
    from groundstation.cop1 import build_control_request
    client.send(build_control_request(9, {39: 1}, client.opts), "valid max id", 9)
    assert col.wait_parsed(ControlAck, 2)
    # id 40 cannot be built with the protocol builder, so send raw words instead.
    from groundstation.protocol import build_message
    client.send(build_message(1, [1, 0, 10, 1, 40, 5]), "invalid id", 10)
    assert col.wait_parsed(NegativeResponse)
    assert 40 not in mock.params


def test_flow_id_mismatch_gets_negative_response(env):
    client, col = env.client(MonitoringClient, env.mock("monitoring"),
                             opts=ProtocolOptions(monitoring_flow_id=5))
    client.request()
    assert col.wait_parsed(NegativeResponse)


# ------------------------------------------------------------ telecommand
def test_telecommand_requests_are_accepted_and_confirmed(env):
    client, col = env.client(TelecommandClient, env.mock("telecommand"))
    sends = {
        "bd": client.send_bd(3, b"hello"),
        "cltu": client.send_cltu(2, b"\x01\x02\x03"),
        "frame": client.send_frame(0, b"abcdefgh"),
        "ad": client.send_ad(1, b"adadad"),
        "nop": client.send_nop(4),
        "directive": client.send_directive(6, 1, 0),
        "subscribe": client.subscribe(1, 3, 1),
    }
    assert col.wait_parsed(AcceptReject, len(sends))
    accepts = {a.tag: a for a in col.parsed(AcceptReject)}
    for name, tag in sends.items():
        assert tag in accepts and accepts[tag].accepted is True, name
    wanted = {sends["ad"], sends["nop"], sends["directive"]}
    assert col.wait(lambda ev: wanted <= {e.parsed.tag for e in ev
                                          if isinstance(e, Received) and isinstance(e.parsed, ConfirmResponse)})
    confirms = {c.tag: c for c in col.parsed(ConfirmResponse)}
    assert all(confirms[t].positive for t in wanted)
    col.wait(lambda ev: False, timeout=0.5)  # BD and CLTU must NOT get a confirm
    confirmed = {c.tag for c in col.parsed(ConfirmResponse)}
    assert confirmed == wanted
    assert sends["bd"] not in confirmed and sends["cltu"] not in confirmed


@pytest.mark.parametrize("client_mode", ["whole_packet", "excluding_postamble", "payload_only"])
@pytest.mark.parametrize("server_mode", ["whole_packet", "excluding_postamble", "payload_only"])
def test_checksum_mode_must_match(env, client_mode, server_mode):
    mock = env.mock("telecommand", opts=ProtocolOptions(checksum_mode=server_mode))
    client, col = env.client(TelecommandClient, mock,
                             opts=ProtocolOptions(checksum_mode=client_mode))
    tag = client.send_bd(3, bytes.fromhex("deadbeef01"))
    if client_mode == server_mode:
        assert col.wait_parsed(AcceptReject)
        assert col.parsed(AcceptReject)[0].tag == tag
    else:
        assert col.wait_parsed(NegativeResponse)
        assert not col.parsed(AcceptReject)


def test_oversized_cltu_without_validation_is_negative(env):
    client, col = env.client(TelecommandClient, env.mock("telecommand"))
    client.send_cltu(1, b"x" * 2000, validate=False)
    assert col.wait_parsed(NegativeResponse)


def test_nonzero_flow_id_round_trip(env):
    opts = ProtocolOptions(telecommand_flow_id=0x1234)
    client, col = env.client(TelecommandClient, env.mock("telecommand", opts=opts), opts=opts)
    client.send_bd(3, b"hello")
    assert col.wait_parsed(AcceptReject)


# ------------------------------------------------------------ telemetry
def test_telemetry_stream_decodes_with_the_config(env):
    client, col = env.client(TelemetryClient, env.mock("telemetry"))
    client.start()
    assert col.wait_parsed(TelemetryMessage, 3)
    messages = col.parsed(TelemetryMessage)
    sequences = [m.sequence for m in messages]
    assert sequences == sorted(set(sequences)) and sequences[0] == 1
    # Integration: decode the first frame with the same config the mock used.
    params = config.load_config(str(SAMPLE_10))
    results = decode.decode_frame(messages[0].frame, params)
    assert len(results) == len(params)
    assert all(r.status in ("OK", "OUT_OF_RANGE") for r in results), [r.status for r in results]


def test_telemetry_stop_ends_the_stream(env):
    client, col = env.client(TelemetryClient, env.mock("telemetry"))
    client.start()
    assert col.wait_parsed(TelemetryMessage, 2)
    client.stop()
    assert col.wait_parsed(TelemetryStopAck)
    col.wait(lambda ev: False, timeout=0.5)
    ack_index = next(i for i, e in enumerate(col.events)
                     if isinstance(e, Received) and isinstance(e.parsed, TelemetryStopAck))
    after = [e for e in col.events[ack_index + 1:]
             if isinstance(e, Received) and isinstance(e.parsed, TelemetryMessage)]
    assert after == []


def test_single_message_per_request(env):
    client, col = env.client(TelemetryClient, env.mock("telemetry"))
    client.start(data_flow=1)
    assert col.wait_parsed(TelemetryMessage, 1)
    col.wait(lambda ev: False, timeout=0.3)
    assert len(col.parsed(TelemetryMessage)) == 1
    client.start(data_flow=1)
    assert col.wait_parsed(TelemetryMessage, 2)
    assert [m.sequence for m in col.parsed(TelemetryMessage)] == [1, 2]


@pytest.mark.parametrize("code, meaning", [
    (1, "Invalid syntax"), (2, "TM chain not mounted"), (3, "No matching OTT data")])
def test_telemetry_negative_acknowledgements(env, code, meaning):
    mock = env.mock("telemetry", faults=MockFaults(tm_error_code=code))
    client, col = env.client(TelemetryClient, mock)
    client.start()
    assert col.wait_parsed(TelemetryNegativeAck)
    nack = col.parsed(TelemetryNegativeAck)[0]
    assert (nack.code, nack.meaning) == (code, meaning)
    col.wait(lambda ev: False, timeout=0.2)
    assert not col.parsed(TelemetryMessage)


def test_second_start_while_streaming_is_nack_1(env):
    client, col = env.client(TelemetryClient, env.mock("telemetry"))
    client.start()
    assert col.wait_parsed(TelemetryMessage)
    client.start()
    assert col.wait_parsed(TelemetryNegativeAck)
    assert col.parsed(TelemetryNegativeAck)[0].code == 1


def test_invalid_start_requests_are_nack_1(env):
    from groundstation.protocol import build_message
    client, col = env.client(TelemetryClient, env.mock("telemetry"))
    client.send(build_message(0, [6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]), "bad channel")
    assert col.wait_parsed(TelemetryNegativeAck)
    client.start(data_flow=4)  # offline flows are not served by the mock
    assert col.wait_parsed(TelemetryNegativeAck, 2)
    assert [n.code for n in col.parsed(TelemetryNegativeAck)] == [1, 1]


def test_garbage_between_messages_is_reported_and_skipped(env):
    mock = env.mock("telemetry", faults=MockFaults(garbage_every_n=3))
    client, col = env.client(TelemetryClient, mock)
    client.start()
    assert col.wait_parsed(TelemetryMessage, 10)
    sequences = [m.sequence for m in col.parsed(TelemetryMessage)]
    assert sequences == list(range(1, len(sequences) + 1))
    assert col.of(StreamProblem)


def test_stall_once_is_recovered(env):
    mock = env.mock("telemetry", faults=MockFaults(stall_once=True))
    client, col = env.client(TelemetryClient, mock, stall_timeout=0.3)
    client.start()
    assert col.wait_parsed(TelemetryMessage, 5, timeout=3.0)
    sequences = [m.sequence for m in col.parsed(TelemetryMessage)]
    assert sequences[0] == 1 and sequences == sorted(set(sequences))
    assert any(p.error.reason == "resync forced" for p in col.of(StreamProblem))


def test_drop_after_n_messages(env):
    mock = env.mock("telemetry", faults=MockFaults(drop_after_n_messages=5))
    client, col = env.client(TelemetryClient, mock)
    client.start()
    assert col.wait(lambda ev: any(isinstance(e, StateChanged) and e.detail == "closed by peer"
                                   for e in ev))
    assert len(col.parsed(TelemetryMessage)) == 5
    last = col.of(StateChanged)[-1]
    assert (last.state, last.detail) == (ConnState.DISCONNECTED, "closed by peer")


def test_bad_frame_length_gives_parse_failures_and_stream_continues(env):
    mock = env.mock("telemetry", faults=MockFaults(bad_frame_length_every_n=4))
    client, col = env.client(TelemetryClient, mock)
    client.start()
    assert col.wait(lambda ev: sum(isinstance(e, ParseFailure) for e in ev) >= 2
                    and sum(isinstance(e, Received) and isinstance(e.parsed, TelemetryMessage)
                            for e in ev) >= 8)
    failures = col.of(ParseFailure)
    assert all("frame length exceeds payload" in f.error for f in failures)
    sequences = [m.sequence for m in col.parsed(TelemetryMessage)]
    assert 4 not in sequences and 8 not in sequences
    assert sequences[0] == 1 and max(sequences) > 8


@pytest.mark.parametrize("check_word, expected", [(1, "error"), (0, "ok")])
def test_frame_check_wiring(env, check_word, expected):
    mock = env.mock("telemetry", faults=MockFaults(decoder_status=0x02, frame_check_word=check_word))
    client, col = env.client(TelemetryClient, mock)
    client.start(data_flow=1)
    assert col.wait_parsed(TelemetryMessage)
    message = col.parsed(TelemetryMessage)[0]
    assert message.frame_check_raw == check_word and message.decoder_status == 0x02
    assert interpret_frame_check(message, ProtocolOptions()).status == expected


@pytest.mark.parametrize("mode", ["whole_packet", "excluding_postamble", "payload_only"])
def test_nonzero_flow_id_round_trip_in_every_checksum_mode(env, mode):
    opts = ProtocolOptions(telecommand_flow_id=0x1234, checksum_mode=mode)
    client, col = env.client(TelecommandClient, env.mock("telecommand", opts=opts), opts=opts)
    tag = client.send_bd(3, bytes.fromhex("deadbeef01"))
    assert col.wait_parsed(AcceptReject)
    assert col.parsed(AcceptReject)[0].tag == tag


def test_requests_reach_the_server_with_exactly_the_given_fields(env):
    mock = env.mock("telecommand")
    client, col = env.client(TelecommandClient, mock)
    t_directive = client.send_directive(8, 5, 77)
    t_nop = client.send_nop(4)
    t_subscribe = client.subscribe(4, 6, 2)
    assert wait_for(lambda: len(mock.received) == 3)
    assert [m.body for m in mock.received] == [
        (8, 5, t_directive, 77), (13, 4, t_nop, 0), (14, 4, t_subscribe, 6, 2)]


def test_telemetry_requests_carry_every_field(env):
    mock = env.mock("telemetry")
    client, col = env.client(TelemetryClient, mock)
    client.start(channel=2, buffered_blocks=100, data_flow=1,
                 frame_mask=0xFFFF0000AABBCCDD, expected_value=0x1122334455667788)
    assert wait_for(lambda: len(mock.received) == 1)
    expected = [2, 100, 1, 0xFFFF0000, 0xAABBCCDD, 0x11223344, 0x55667788, 0, 0, 0, 0, 0]
    assert mock.received[0].body == tuple(to_i32(w) for w in expected)
    client.stop(channel=3)
    assert wait_for(lambda: len(mock.received) == 2)
    assert mock.received[1].body == (3, 0, 0x80) + (0,) * 9


def test_validate_false_lets_invalid_requests_reach_the_server_and_be_rejected(env):
    client, col = env.client(TelecommandClient, env.mock("telecommand"))
    client.send_bd(3, b"x" * 1020, validate=False)
    client.send_ad(3, b"x" * 1020, validate=False)
    client.send_frame(3, b"x" * 6, validate=False)      # codeblock length 3 is not allowed
    client.send_frame(0, b"x" * 1025, validate=False)   # one byte over the frame limit
    client.send_bd(64, b"x", validate=False)            # VCID above 63
    assert col.wait_parsed(NegativeResponse, 5)
    assert not col.parsed(AcceptReject)


def test_invalid_subscription_fields_are_rejected_by_the_server(env):
    client, col = env.client(TelecommandClient, env.mock("telecommand"))
    for tag, body in enumerate([[14, 64, 1, 0, 0], [14, 0, 2, 7, 0], [14, 0, 3, 0, 3]], start=1):
        client.send(build_message(0, body), "invalid subscription", tag)
    assert col.wait_parsed(NegativeResponse, 3)
    assert not col.parsed(AcceptReject)


def test_control_client_uses_its_options(env):
    opts = ProtocolOptions(control_flow_id=9)
    client, col = env.client(ControlClient, env.mock("control", opts=opts), opts=opts)
    tag = client.set_parameters({0: 5})
    assert col.wait_parsed(ControlAck)
    assert col.parsed(ControlAck)[0].tag == tag


def test_options_can_be_replaced_while_connected(env):
    client, col = env.client(MonitoringClient, env.mock("monitoring"))
    client.opts = ProtocolOptions(monitoring_flow_id=5)
    assert client.opts.monitoring_flow_id == 5
    client.request()
    assert col.wait_parsed(NegativeResponse)


def test_tag_counter_wraps_to_zero_after_the_largest_value():
    client = MonitoringClient()
    client._next_tag = 0xFFFFFFFF
    assert [client.next_tag() for _ in range(3)] == [0xFFFFFFFF, 0, 1]
