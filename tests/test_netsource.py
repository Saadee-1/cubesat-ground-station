"""Tests for groundstation.netsource (SwitchableSource and NetworkFrameSource)."""
import queue
import time
from pathlib import Path

import pytest

from groundstation.clients import TelemetryClient
from groundstation.config import load_config
from groundstation.mockserver import MockServer
from groundstation.net import ConnState
from groundstation.netsource import (MODE_NETWORK, MODE_SIMULATED, MODES, NetworkFrameSource,
                                     SwitchableSource, make_switchable)
from groundstation.protocol import to_i32
from groundstation.source import FrameSource, SimFrameSource
from groundstation.tm import TelemetryPipeline

SAMPLE_10 = Path(__file__).resolve().parent.parent / "groundstation" / "samples" / "sample_config_10.json"


class FakeSource(FrameSource):
    """Records every call so tests can see what the wrapper did."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.calls = []

    def start(self):
        self.calls.append("start")
        return "started " + self.name

    def stop(self):
        self.calls.append("stop")
        return "stopped " + self.name

    def set_params(self, params):
        self.calls.append(("set_params", tuple(params)))


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ------------------------------------------------------------ SwitchableSource
def make_switch():
    sim, net = FakeSource("sim"), FakeSource("net")
    return SwitchableSource(sim, net), sim, net


def test_default_mode_is_simulated_and_frames_is_the_active_queue():
    switch, sim, net = make_switch()
    assert switch.mode == MODE_SIMULATED == "Simulated" and MODES == ("Simulated", "Network")
    assert switch.frames is sim.frames and switch.frames is not net.frames
    assert switch.name == "sim" and switch.active is sim


def test_frames_follows_every_select():
    switch, sim, net = make_switch()
    for mode, expected in [("Network", net), ("Simulated", sim), ("Network", net), ("Simulated", sim)]:
        switch.select(mode)
        assert switch.mode == mode
        assert switch.frames is expected.frames and switch.name == expected.name and switch.active is expected
        switch.frames.put(b"x")  # what the pipeline reads is what the active source owns
        assert expected.frames.get_nowait() == b"x"


def test_select_stops_the_previously_active_source_first():
    switch, sim, net = make_switch()
    switch.select("Network")
    assert sim.calls == ["stop"] and net.calls == []
    switch.select("Simulated")
    assert sim.calls == ["stop"] and net.calls == ["stop"]
    switch.select("Simulated")  # already active: nothing is stopped again
    assert sim.calls == ["stop"] and net.calls == ["stop"]


def test_select_stops_before_switching():
    order = []

    class Probe(FakeSource):
        def stop(self):
            order.append(("stop", switch.mode))  # the mode must still be the OLD one here
            return super().stop()

    sim, net = Probe("sim"), FakeSource("net")
    switch = SwitchableSource(sim, net)
    switch.select("Network")
    assert order == [("stop", "Simulated")]


def test_unknown_mode_raises_and_changes_nothing():
    switch, sim, net = make_switch()
    for bad in ("", "network", "Both", None, 3):
        with pytest.raises(ValueError):
            switch.select(bad)
    assert switch.mode == "Simulated" and sim.calls == [] and net.calls == []


def test_select_drops_frames_queued_by_either_source():
    switch, sim, net = make_switch()
    sim.frames.put(b"old sim")
    net.frames.put(b"old net")
    switch.select("Network")
    assert sim.frames.empty() and net.frames.empty()
    net.frames.put(b"new net")
    switch.select("Simulated")
    assert sim.frames.empty() and net.frames.empty()


def test_start_and_stop_go_to_the_active_source_only():
    switch, sim, net = make_switch()
    assert switch.start() == "started sim" and switch.stop() == "stopped sim"
    switch.select("Network")
    sim.calls.clear()
    assert switch.start() == "started net" and switch.stop() == "stopped net"
    assert sim.calls == [] and net.calls == ["start", "stop"]


def test_set_params_reaches_both_sources():
    switch, sim, net = make_switch()
    params = load_config(str(SAMPLE_10))
    switch.set_params(params)
    assert sim.calls == [("set_params", tuple(params))] and net.calls == [("set_params", tuple(params))]
    switch.select("Network")
    sim.calls.clear(), net.calls.clear()
    switch.set_params(params[:2])
    assert sim.calls == [("set_params", tuple(params[:2]))] and net.calls == [("set_params", tuple(params[:2]))]


def test_pipeline_set_params_through_the_wrapper_drains_the_active_queue():
    switch, sim, net = make_switch()
    params = load_config(str(SAMPLE_10))
    pipeline = TelemetryPipeline(switch, params[:5])
    sim.frames.put(b"a"), sim.frames.put(b"b")
    pipeline.set_params(params)
    assert sim.frames.empty()
    assert sim.calls[-1] == ("set_params", tuple(params)) and net.calls[-1] == ("set_params", tuple(params))
    switch.select("Network")
    net.frames.put(b"c")
    pipeline.set_params(params[:5])
    assert net.frames.empty()  # now the network queue is the one being drained


def test_pipeline_polls_the_active_queue_after_a_switch():
    switch, sim, net = make_switch()
    params = load_config(str(SAMPLE_10))
    pipeline = TelemetryPipeline(switch, params)
    switch.select("Network")
    net.frames.put(bytes(64))
    sim.frames.put(bytes(64))  # not the active queue: must not be decoded
    assert len(pipeline.poll()) == 1
    assert sim.frames.qsize() == 1


def test_make_switchable_wraps_once():
    params = load_config(str(SAMPLE_10))
    sim = SimFrameSource(params)
    pipeline = TelemetryPipeline(sim, params)
    client = TelemetryClient()
    switch = make_switchable(pipeline, client)
    assert isinstance(switch, SwitchableSource) and pipeline.source is switch
    assert switch.simulated is sim and isinstance(switch.network, NetworkFrameSource)
    assert switch.network.client is client and switch.mode == "Simulated"
    assert make_switchable(pipeline, TelemetryClient()) is switch  # already wrapped: unchanged
    assert switch.network.client is client


def test_the_real_simulated_source_still_runs_through_the_wrapper():
    params = load_config(str(SAMPLE_10))
    sim = SimFrameSource(params, rate_hz=50)
    pipeline = TelemetryPipeline(sim, params)
    switch = make_switchable(pipeline, TelemetryClient())
    try:
        switch.start()
        assert wait_for(lambda: len(pipeline.poll()) > 0 or pipeline.frames_total > 0)
        switch.select("Network")  # stops the simulator and drops its frames
        assert not sim.is_running()
        time.sleep(0.2)
        assert switch.frames.empty() and sim.frames.empty()
    finally:
        sim.stop()


# ------------------------------------------------------------ NetworkFrameSource
@pytest.fixture
def served():
    mocks, clients = [], []

    def make(**kwargs):
        mock = MockServer("telemetry", **kwargs)
        mock.start()
        mocks.append(mock)
        client = TelemetryClient(port=mock.port)
        clients.append(client)
        client.connect()
        assert wait_for(lambda: client.state == ConnState.CONNECTED)
        return mock, client, NetworkFrameSource(client)

    yield make
    for client in clients:
        client.disconnect()
    for mock in mocks:
        mock.stop()


def test_defaults():
    source = NetworkFrameSource(TelemetryClient())
    assert source.name == "Network (CORTEX telemetry)"
    assert (source.channel, source.buffered_blocks, source.data_flow) == (0, 0, 0)
    assert (source.frame_mask, source.expected_value) == (0, 0)
    assert source.last_error == "" and source.streaming is False and source.starts == 0
    assert source.frames.empty()


def test_start_sends_exactly_the_configured_fields(served):
    mock, client, source = served(frame_rate_hz=50)
    source.channel, source.buffered_blocks, source.data_flow = 2, 100, 1
    source.frame_mask, source.expected_value = 0xFFFF0000AABBCCDD, 0x1122334455667788
    assert source.start() is True
    assert source.streaming is True and source.starts == 1 and source.last_error == ""
    assert wait_for(lambda: len(mock.received) == 1)
    expected = [2, 100, 1, 0xFFFF0000, 0xAABBCCDD, 0x11223344, 0x55667788, 0, 0, 0, 0, 0]
    assert mock.received[0].body == tuple(to_i32(w) for w in expected)


def test_stop_sends_a_stop_request_for_the_configured_channel(served):
    mock, client, source = served()
    source.channel = 3
    assert source.start() is True
    assert source.stop() is True
    assert source.streaming is False and source.last_error == ""
    assert wait_for(lambda: len(mock.received) == 2)
    assert mock.received[1].body == (3, 0, 0x80) + (0,) * 9


def test_start_and_stop_while_disconnected_return_false_and_never_raise():
    source = NetworkFrameSource(TelemetryClient())
    assert source.start() is False
    assert "not connected" in source.last_error and source.streaming is False and source.starts == 0
    source.last_error = ""
    assert source.stop() is False and "not connected" in source.last_error


def test_invalid_settings_return_false_with_the_protocol_message(served):
    mock, client, source = served()
    for attribute, value in [("channel", 6), ("buffered_blocks", 1025), ("data_flow", 3),
                             ("frame_mask", 2 ** 64), ("expected_value", -1)]:
        fresh = NetworkFrameSource(client)
        setattr(fresh, attribute, value)
        assert fresh.start() is False and fresh.last_error and fresh.streaming is False
    time.sleep(0.2)
    assert mock.received == []  # nothing invalid reached the server


def test_prepare_hook_can_refuse_a_start(served):
    mock, client, source = served()
    source.prepare = lambda: (setattr(source, "last_error", "bad field"), False)[1]
    assert source.start() is False and source.last_error == "bad field"
    assert source.starts == 0 and mock.received == []
    source.prepare = lambda: True
    assert source.start() is True and source.starts == 1


def test_a_failed_stop_still_marks_the_stream_as_ended():
    source = NetworkFrameSource(TelemetryClient())
    source.streaming = True
    assert source.stop() is False and source.streaming is False


def test_mark_stopped_sends_nothing(served):
    mock, client, source = served()
    source.start()
    source.mark_stopped()
    assert source.streaming is False
    assert wait_for(lambda: len(mock.received) == 1)
    time.sleep(0.1)
    assert len(mock.received) == 1


def test_push_frame_queues_bytes():
    source = NetworkFrameSource(TelemetryClient())
    source.push_frame(bytearray(b"\x01\x02"))
    assert source.frames.get_nowait() == b"\x01\x02"
    with pytest.raises(queue.Empty):
        source.frames.get_nowait()
