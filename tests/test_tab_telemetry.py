"""Tests for groundstation.tab_telemetry (stream statistics, forwarding, faults, layout)."""
import dataclasses
import time
import types
from pathlib import Path
from tkinter import ttk
import tkinter as tk

import pytest

from groundstation.clients import TelemetryClient
from groundstation.config import load_config
from groundstation.cortex import (CHANNELS, DummyTelemetry, TelemetryMessage, TelemetryNegativeAck,
                                  TelemetryStopAck, UnknownTmMessage)
from groundstation.decode import decode_frame
from groundstation.guibase import RED, checkbutton_selected, set_checkbutton, set_entry
from groundstation.mockserver import MockFaults, MockServer
from groundstation.net import ConnState, StateChanged
from groundstation.netsource import MODE_NETWORK, MODE_SIMULATED
from groundstation.protocol import ProtocolOptions, to_i32
from groundstation.source import SimFrameSource
from groundstation.tab_telemetry import (DATA_FLOWS, SequenceTracker, TelemetryTab, parse_uint)
from groundstation.tm import TelemetryPipeline
from gui_util import gui_env, has_log_line, pending_after_count, settle, wait_until  # noqa: F401
from test_app_gui import descendants, laid_out, settle_layout, window  # noqa: F401

SAMPLE_10 = Path(__file__).resolve().parent.parent / "groundstation" / "samples" / "sample_config_10.json"
PARAMS = load_config(str(SAMPLE_10))


def _chars(*codes):
    return "".join(chr(c) for c in codes)


def build(gui_env, mock=None, connect=False, store=None, sim_rate=50):
    """A TelemetryTab on its own pipeline; returns (tab, pipeline)."""
    pipeline = TelemetryPipeline(SimFrameSource(PARAMS, rate_hz=sim_rate), PARAMS)

    def factory(frame, client, store_, pump_ms):
        return TelemetryTab(frame, client, store_, pipeline, pump_ms=pump_ms)

    tab = gui_env.make_tab(factory, TelemetryClient, mock=mock, store=store, connect=connect)
    return tab, pipeline


def stream_mock(gui_env, **kwargs):
    kwargs.setdefault("frame_rate_hz", 50)
    return gui_env.mock("telemetry", **kwargs)


def button(tab, text):
    return next(w for w in descendants(tab.panel) if isinstance(w, ttk.Button) and w.cget("text") == text)


def msg(sequence, *, sync=1, decoder=0, check=0, transmitted=0, frame=b"\x00" * 8, consistent=True,
        bit_slip=0, delay=0, tag1=0, tag2=0):
    return TelemetryMessage(
        time_tag_1=tag1, time_tag_2=tag2, sequence=sequence, frame_check_raw=check, sync_status=sync,
        bit_slip=bit_slip, tm_delay=delay, frame_length=len(frame), sync_word_length=16,
        decoder_status=decoder, transmitted_data=transmitted, frame=frame, length_consistent=consistent)


def feed(tab, *parsed):
    for p in parsed:
        tab.on_received(types.SimpleNamespace(parsed=p))


# ------------------------------------------------------------ sequence tracking
def tracker_after(*sequences):
    tracker = SequenceTracker()
    kinds = [tracker.observe(s) for s in sequences]
    return tracker, kinds


def test_consecutive_sequences_have_no_gap():
    tracker, kinds = tracker_after(1, 2, 3, 4)
    assert kinds == ["first", "ok", "ok", "ok"]
    assert (tracker.gaps, tracker.missed, tracker.out_of_order) == (0, 0, 0)


def test_one_gap_of_three_missed():
    tracker, kinds = tracker_after(1, 2, 6, 7)
    assert kinds == ["first", "ok", "gap", "ok"]
    assert (tracker.gaps, tracker.missed, tracker.out_of_order) == (1, 3, 0)


def test_wrap_at_2_to_the_32_is_not_a_gap():
    tracker, kinds = tracker_after(0xFFFFFFFE, 0xFFFFFFFF, 0, 1)
    assert kinds == ["first", "ok", "ok", "ok"]
    assert (tracker.gaps, tracker.missed, tracker.out_of_order) == (0, 0, 0)


def test_gap_across_the_wrap_counts_the_missed_messages():
    tracker, kinds = tracker_after(0xFFFFFFFF, 2)
    assert kinds == ["first", "gap"] and (tracker.gaps, tracker.missed) == (1, 2)


def test_repeated_sequence_is_out_of_order_not_a_gap():
    tracker, kinds = tracker_after(5, 5, 6)
    assert kinds == ["first", "out_of_order", "ok"]
    assert (tracker.gaps, tracker.missed, tracker.out_of_order) == (0, 0, 1)


def test_backwards_sequence_is_out_of_order_not_a_huge_gap():
    tracker, kinds = tracker_after(10, 5, 6)
    assert kinds == ["first", "out_of_order", "ok"]  # a restarted stream carries on from the new number
    assert (tracker.gaps, tracker.missed, tracker.out_of_order) == (0, 0, 1)
    tracker, kinds = tracker_after(1, 0x80000002)  # difference above 2**31 counts as backwards
    assert kinds == ["first", "out_of_order"] and tracker.missed == 0
    tracker, kinds = tracker_after(0, 0x80000000)  # exactly 2**31 is still a (huge) gap
    assert kinds == ["first", "gap"] and tracker.missed == 0x7FFFFFFF


def test_tracker_reset_and_restart():
    tracker, _ = tracker_after(1, 2, 9)
    assert tracker.gaps == 1
    tracker.restart()
    assert tracker.observe(100) == "first" and tracker.gaps == 1  # restart keeps the counters
    tracker.reset()
    assert tracker.last is None and (tracker.gaps, tracker.missed, tracker.out_of_order) == (0, 0, 0)


def test_tab_counts_gaps_from_events(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(1), msg(2), msg(6), msg(7))
    c = tab.counters
    assert (c["received"], c["gaps"], c["missed"], c["out_of_order"], c["last_sequence"]) == (4, 1, 3, 0, 7)
    feed(tab, msg(7), msg(3))
    assert (c["gaps"], c["missed"], c["out_of_order"], c["last_sequence"]) == (1, 3, 2, 3)
    assert "Gaps 1 (missed 3) | out of order 2" in tab.status_text()


def test_tab_wraps_at_2_to_the_32(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(0xFFFFFFFE), msg(0xFFFFFFFF), msg(0))
    assert (tab.counters["gaps"], tab.counters["missed"], tab.counters["out_of_order"]) == (0, 0, 0)


def test_start_resets_tracking_and_counters(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(1), msg(2), msg(9))
    assert tab.counters["gaps"] == 1 and tab.counters["received"] == 3
    tab.network.starts += 1  # what a successful Start from the panel does ...
    tab.network.streaming = True  # ... it also marks the stream as running
    tab._poll_source()
    assert tab.stream_state == "requested"
    assert tab.counters["received"] == 0 and tab.counters["gaps"] == 0 and tab.counters["missed"] == 0
    feed(tab, msg(500))  # the first message of the new stream cannot be a gap
    assert tab.counters["gaps"] == 0 and tab.counters["last_sequence"] == 500


@pytest.mark.parametrize("event", [StateChanged(ConnState.CONNECTED, "c"),
                                   StateChanged(ConnState.DISCONNECTED, "d"),
                                   StateChanged(ConnState.ERROR, "e")])
def test_connect_and_disconnect_restart_sequence_tracking(gui_env, event):
    tab, _ = build(gui_env)
    feed(tab, msg(1), msg(2), msg(9))
    tab.client.events.put(event)
    tab._pump()
    feed(tab, msg(1000))
    assert tab.counters["gaps"] == 1 and tab.counters["missed"] == 6  # only the earlier gap; 1000 is "first"
    assert tab.counters["received"] == 4  # the counters stay visible after the link is lost


# ------------------------------------------------------------ frame check counters and forwarding
CHECK_CASES = [
    # (decoder status, check word, expected status)
    (0x01, 0x00000000, "ok"), (0x01, 0x01000000, "error"), (0x01, 0x02000000, "error"),   # Reed-Solomon
    (0x08, 0x00000000, "ok"), (0x08, 0x02000000, "error"),                                # Turbo
    (0x10, 0x00000000, "ok"), (0x10, 0x0000FFFF, "error"),                                # LDPC
    (0x02, 0x00000000, "ok"), (0x02, 0x00000001, "error"),                                # CRC / checksum
]


def test_frame_check_counters_for_every_decoder_type(gui_env):
    tab, _ = build(gui_env)
    for number, (decoder, check, _) in enumerate(CHECK_CASES, start=1):
        feed(tab, msg(number, decoder=decoder, check=check))
    feed(tab, msg(100, sync=0, decoder=0x01, check=0x01000000))    # no sync: not significant
    feed(tab, msg(101, sync=4, decoder=0x01, check=0x01000000))    # sync 4: not significant
    feed(tab, msg(102, sync=1, decoder=0x00, check=0x01000000))    # no decoder: not significant
    c = tab.counters
    assert c["check_ok"] == sum(1 for *_, s in CHECK_CASES if s == "ok") == 4
    assert c["check_error"] == sum(1 for *_, s in CHECK_CASES if s == "error") == 5
    assert c["check_not_significant"] == 3
    assert c["received"] == len(CHECK_CASES) + 3
    assert "frame check ok 4 / error 5 / not significant 3" in tab.status_text()


def test_byte_numbering_option_changes_the_interpretation_of_the_same_message(gui_env):
    store = gui_env.store()
    tab, _ = build(gui_env, store=store)
    message = msg(1, decoder=0x01, check=0x01000000)  # byte 0 (MSB first) has bit 0 set: capacity exceeded
    feed(tab, message)
    assert (tab.counters["check_ok"], tab.counters["check_error"]) == (0, 1)
    store.set(dataclasses.replace(store.get(), cortex_byte0_is_msb=False))  # now byte 0 is the LEAST significant
    feed(tab, dataclasses.replace(message, sequence=2))
    assert (tab.counters["check_ok"], tab.counters["check_error"]) == (1, 1)


def test_length_inconsistent_messages_are_counted(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(1), msg(2, consistent=False), msg(3, consistent=False))
    assert tab.counters["length_inconsistent"] == 2 and "length mismatch 2" in tab.status_text()


def network_tab(gui_env, **kwargs):
    tab, pipeline = build(gui_env, **kwargs)
    tab.set_source_mode("Network")
    return tab, pipeline


def test_forward_checkbox_defaults_off_and_forwards_every_frame(gui_env):
    tab, _ = network_tab(gui_env)
    assert not checkbutton_selected(tab.forward_check)
    for number, (decoder, check, _) in enumerate(CHECK_CASES, start=1):
        feed(tab, msg(number, decoder=decoder, check=check, frame=bytes([number]) * 8))
    assert tab.counters["forwarded"] == len(CHECK_CASES) == tab.network.frames.qsize()


def test_forward_only_ok_forwards_just_the_ok_frames(gui_env):
    tab, _ = network_tab(gui_env)
    set_checkbutton(tab.forward_check, True)
    for number, (decoder, check, status) in enumerate(CHECK_CASES, start=1):
        feed(tab, msg(number, decoder=decoder, check=check, frame=bytes([number]) * 8))
    forwarded = []
    while not tab.network.frames.empty():
        forwarded.append(tab.network.frames.get_nowait())
    ok_numbers = [n for n, (_, _, status) in enumerate(CHECK_CASES, start=1) if status == "ok"]
    assert forwarded == [bytes([n]) * 8 for n in ok_numbers]
    assert tab.counters["forwarded"] == len(ok_numbers) and tab.counters["received"] == len(CHECK_CASES)
    feed(tab, msg(50, sync=0, decoder=0x01, check=0x01000000, frame=b"\xee" * 8))  # not significant: not "ok"
    assert tab.network.frames.empty()


def test_nothing_is_forwarded_in_simulated_mode(gui_env):
    tab, _ = build(gui_env)
    assert tab.switch.mode == MODE_SIMULATED
    feed(tab, msg(1), msg(2))
    assert tab.counters["forwarded"] == 0 and tab.network.frames.empty() and tab.switch.frames.empty()
    assert tab.counters["received"] == 2  # still counted


# ------------------------------------------------------------ dummy, negative ack, stop ack
def test_dummy_messages_are_counted_but_never_forwarded(gui_env):
    tab, _ = network_tab(gui_env)
    feed(tab, DummyTelemetry(time_tag_1=1, time_tag_2=2, sequence=3, sync_status=3),
         DummyTelemetry(time_tag_1=1, time_tag_2=2, sequence=4, sync_status=3))
    assert tab.counters["dummy"] == 2 and tab.counters["forwarded"] == 0 and tab.counters["received"] == 0
    assert tab.network.frames.empty()
    assert "dummy 2" in tab.status_text()


def test_negative_acknowledgement_shows_the_meaning_and_logs_an_error(gui_env):
    tab, _ = build(gui_env)
    tab.network.streaming = True
    feed(tab, TelemetryNegativeAck(code=2, meaning="TM chain not mounted"))
    assert tab.stream_state == "request refused" and "stream: request refused" in tab.status_text()
    assert "TM chain not mounted" in tab.message_text() and "code 2" in tab.message_text()
    assert str(tab.message.cget("fg")) == RED
    assert has_log_line(tab, "Telemetry request refused, code 2: TM chain not mounted.")
    assert tab.error_count == 1 and tab.network.streaming is False


def test_stop_acknowledgement_sets_stopped(gui_env):
    tab, _ = build(gui_env)
    tab.network.streaming = True
    feed(tab, TelemetryStopAck())
    assert tab.stream_state == "stopped" and "stream: stopped" in tab.status_text()
    assert tab.network.streaming is False


def test_unknown_telemetry_message_counts_as_an_error(gui_env, tk_root):
    tab, _ = build(gui_env)
    assert tab.is_error_response(UnknownTmMessage(raw=b"", reason="x")) is True
    assert tab.is_error_response(TelemetryStopAck()) is False


def test_status_block_shows_the_last_values_and_raw_time_tags(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(77, sync=1, decoder=0x02, bit_slip=3, delay=9, tag1=0xDEADBEEF, tag2=0x1))
    text = tab.status_text()
    assert "last sequence 77" in text and "Last sync status 1" in text and "decoder status 2" in text
    assert "bit slip 3" in text and "TM delay 9" in text
    assert "time tags (raw words) 0xDEADBEEF 0x00000001" in text


# ------------------------------------------------------------ end to end
def test_network_stream_reaches_the_panel_table_with_the_decoded_values(gui_env, tk_root):
    mock = stream_mock(gui_env)
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    tab.set_source_mode("Network")
    assert tab.source_box.get() == "Network" and tab.switch.name == "Network (CORTEX telemetry)"
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: tab.counters["forwarded"] >= 8 and tab.stream_state == "streaming")
    assert mock.received[0].body[:3] == (0, 0, 0)  # channel A, 0 buffered blocks, realtime permanent
    button(tab, "Stop").invoke()
    assert wait_until(tk_root, lambda: tab.stream_state == "stopped")
    # Let the panel decode everything that was forwarded, then compare its table with decode_frame.
    assert wait_until(tk_root, lambda: pipeline.frames_total == tab.counters["forwarded"])
    expected = decode_frame(tab.last_frame, pipeline.params)
    rows = tab.panel.rows
    assert len(rows) == len(expected) == 10
    for row, value in zip(rows, expected):
        shown = row.labels["Value"].cget("text")
        assert shown == ("--" if value.value is None else f"{value.value:.{value.decimals}f}"), value.name
        assert row.labels["Raw"].cget("text") == ("--" if value.raw is None else (
            f"{value.raw:.6g}" if isinstance(value.raw, float) else str(value.raw)))
        assert row.labels["Status"].cget("text") == value.status
        assert row.labels["Unit"].cget("text") == value.unit
    assert all(v.status in ("OK", "OUT_OF_RANGE") for v in expected)
    # After Stop no more frames arrive.
    received = tab.counters["received"]
    settle(tk_root, 1.0)
    assert tab.counters["received"] == received
    assert pipeline.frames_total == tab.counters["forwarded"]


def test_panel_start_and_stop_drive_the_simulated_source_in_simulated_mode(gui_env, tk_root):
    tab, pipeline = build(gui_env)
    sim = tab.switch.simulated
    try:
        button(tab, "Start").invoke()
        assert wait_until(tk_root, lambda: pipeline.frames_total >= 3)
        assert sim.is_running() and tab.stream_state == "idle"  # no network request was sent
        button(tab, "Stop").invoke()
        assert not sim.is_running()
    finally:
        sim.stop()


def test_start_in_network_mode_while_disconnected_shows_the_error_inline(gui_env, tk_root):
    tab, pipeline = network_tab(gui_env)
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: "not connected" in tab.message_text())
    assert str(tab.message.cget("fg")) == RED and tab.stream_state == "idle"
    assert tab.network.starts == 0 and tab.network.streaming is False


def test_load_config_in_the_panel_still_works_and_pipeline_params_are_untouched(gui_env):
    tab, pipeline = build(gui_env)
    before = pipeline.params
    assert len(tab.panel.rows) == 10
    tab.panel.rebuild(PARAMS[:5])  # what "Load config..." does after set_params
    assert len(tab.panel.rows) == 5 and pipeline.params == before  # the tab never touches parameters
    pipeline.set_params(PARAMS[:5])
    assert tab.switch.network.frames.empty()


# ------------------------------------------------------------ server faults
def start_network_stream(tab, tk_root):
    tab.set_source_mode("Network")
    button(tab, "Start").invoke()


def test_bad_frame_length_is_logged_and_the_stream_continues(gui_env, tk_root):
    mock = stream_mock(gui_env, faults=MockFaults(bad_frame_length_every_n=4))
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    start_network_stream(tab, tk_root)
    assert wait_until(tk_root, lambda: tab.counters["received"] >= 9)
    assert has_log_line(tab, "Parse failure: frame length exceeds payload")
    assert tab.error_count >= 2
    assert tab.counters["last_sequence"] >= 9
    last = tab.counters["last_sequence"]
    assert last % 4 != 0
    assert tab.counters["gaps"] == tab.counters["missed"] == last // 4 >= 2  # every 4th message never arrived intact
    button(tab, "Stop").invoke()  # compare only after the stream has ended, or the numbers keep moving
    assert wait_until(tk_root, lambda: tab.stream_state == "stopped")
    settle(tk_root, 0.3)  # frames that were already in flight
    assert tab.counters["forwarded"] == tab.counters["received"]
    assert wait_until(tk_root, lambda: pipeline.frames_total == tab.counters["forwarded"])


def test_garbage_between_messages_is_reported_and_frames_still_arrive(gui_env, tk_root):
    mock = stream_mock(gui_env, faults=MockFaults(garbage_every_n=3))
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    start_network_stream(tab, tk_root)
    assert wait_until(tk_root, lambda: tab.counters["received"] >= 10)
    assert has_log_line(tab, "Stream problem: garbage before start")
    assert tab.counters["gaps"] == 0 and tab.counters["out_of_order"] == 0
    assert wait_until(tk_root, lambda: pipeline.frames_total >= 10)


def test_error_code_from_the_server_is_shown(gui_env, tk_root):
    mock = stream_mock(gui_env, faults=MockFaults(tm_error_code=3))
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    start_network_stream(tab, tk_root)
    assert wait_until(tk_root, lambda: tab.stream_state == "request refused")
    assert "No matching OTT data" in tab.message_text()
    assert has_log_line(tab, "code 3: No matching OTT data")
    settle(tk_root, 0.3)
    assert tab.counters["received"] == 0 and pipeline.frames_total == 0 and tab.network.streaming is False


def test_dropped_connection_means_link_lost_and_shutdown_does_not_hang(gui_env, tk_root):
    mock = stream_mock(gui_env, faults=MockFaults(drop_after_n_messages=5))
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    start_network_stream(tab, tk_root)
    assert wait_until(tk_root, lambda: tab.stream_state == "link lost")
    assert tab.counters["received"] == 5  # the counters stay visible
    assert tab.network.streaming is False and tab.state_label.cget("text") == "DISCONNECTED"
    assert "stream: link lost" in tab.status_text()
    started = time.monotonic()
    tab.shutdown()
    assert time.monotonic() - started < 2.0
    assert len(mock.received) == 1  # no stop request was sent on a dead link


# ------------------------------------------------------------ switching sources
def test_switching_back_and_forth_during_a_stream_does_not_leak_frames(gui_env, tk_root):
    mock = stream_mock(gui_env)
    tab, pipeline = build(gui_env, mock=mock, connect=True)
    sim = tab.switch.simulated
    try:
        start_network_stream(tab, tk_root)
        assert wait_until(tk_root, lambda: tab.counters["forwarded"] >= 3)
        tab.set_source_mode("Simulated")  # stops the network stream and drops queued frames
        assert tab.switch.mode == "Simulated" and tab.switch.frames is sim.frames
        assert tab.network.frames.empty() and sim.frames.empty()
        forwarded = tab.counters["forwarded"]
        settle(tk_root, 0.5)  # frames still in flight from the server must not be forwarded now
        assert tab.counters["forwarded"] == forwarded and tab.network.frames.empty() and sim.frames.empty()
        assert wait_until(tk_root, lambda: any(m.body[2] == 0x80 for m in mock.received))  # a stop was sent
        button(tab, "Start").invoke()  # now the simulator streams
        assert wait_until(tk_root, lambda: pipeline.frames_total >= 3)
        tab.set_source_mode("Network")
        assert not sim.is_running()
        assert sim.frames.empty() and tab.network.frames.empty()
        total = pipeline.frames_total
        settle(tk_root, 0.5)
        assert pipeline.frames_total == total  # nothing from the simulator reaches the pipeline any more
    finally:
        sim.stop()


def test_switching_the_source_resets_the_statistics(gui_env):
    tab, _ = build(gui_env)
    feed(tab, msg(1), msg(5))
    tab.set_source_mode("Network")
    assert tab.counters["received"] == 0 and tab.counters["gaps"] == 0
    assert tab.stream_state == "idle" and "Source: Network" in tab.status_text()


# ------------------------------------------------------------ request fields
INITIAL = {"channel": 0, "buffered_blocks": 0, "data_flow": 0, "frame_mask": 0, "expected_value": 0}


def source_settings(tab):
    n = tab.network
    return {"channel": n.channel, "buffered_blocks": n.buffered_blocks, "data_flow": n.data_flow,
            "frame_mask": n.frame_mask, "expected_value": n.expected_value}


def test_choices_come_from_cortex(gui_env):
    tab, _ = build(gui_env)
    assert "".join(tab.channel_box.cget("values")) == CHANNELS == "ABCDEF"
    flows = {int(v.split(" ", 1)[0]) for v in tab.flow_box.cget("values")}
    assert flows == {0, 1, 2, 4, 5, 6} == set(DATA_FLOWS)
    assert str(tab.channel_box.cget("state")) == "readonly" and str(tab.flow_box.cget("state")) == "readonly"
    assert tuple(tab.source_box.cget("values")) == ("Simulated", "Network")
    assert TelemetryClient().port == 3070  # the default port of the client the app creates


def test_valid_fields_reach_the_source(gui_env):
    tab, _ = build(gui_env)
    tab.channel_box.set("C")
    set_entry(tab.blocks_entry, "0x64")
    tab.flow_box.set("1 realtime single")
    set_entry(tab.mask_entry, "0xFFFF0000AABBCCDD")
    set_entry(tab.expected_entry, "18446744073709551615")
    assert tab.apply_settings() is True
    assert source_settings(tab) == {"channel": 2, "buffered_blocks": 100, "data_flow": 1,
                                    "frame_mask": 0xFFFF0000AABBCCDD, "expected_value": 2 ** 64 - 1}
    assert tab.message_text() == ""


BAD = ["", "8_0", "+3", "-1", "0x_1F", "0x", "1e3", "0b1", "1.5", " ", _chars(0x663), _chars(0xFF11)]


@pytest.mark.parametrize("field, bad", [("blocks_entry", b) for b in BAD + ["1025", "0x401"]]
                         + [("mask_entry", b) for b in BAD + ["18446744073709551616", "0x10000000000000000"]]
                         + [("expected_entry", b) for b in BAD + ["18446744073709551616"]])
def test_invalid_numbers_never_reach_the_source(gui_env, field, bad):
    mock = stream_mock(gui_env)
    tab, _ = build(gui_env, mock=mock, connect=True)
    tab.set_source_mode("Network")
    set_entry(getattr(tab, field), bad)
    assert tab.apply_settings() is False
    assert source_settings(tab) == INITIAL
    assert str(tab.message.cget("fg")) == RED and tab.message_text()
    button(tab, "Start").invoke()  # Start refuses too, and nothing is sent
    assert tab.network.starts == 0 and tab.network.streaming is False
    time.sleep(0.05)
    assert mock.received == []


def test_a_bad_field_keeps_the_previous_valid_settings(gui_env):
    tab, _ = build(gui_env)
    set_entry(tab.blocks_entry, "7")
    assert tab.apply_settings() is True and tab.network.buffered_blocks == 7
    tab.channel_box.set("D")
    set_entry(tab.blocks_entry, "7_0")
    assert tab.apply_settings() is False
    assert tab.network.buffered_blocks == 7 and tab.network.channel == 0  # nothing was half applied


@pytest.mark.parametrize("text, limit, value", [("0", 10, 0), (" 10 ", 10, 10), ("0x0A", 10, 10),
                                                 ("18446744073709551615", 2 ** 64 - 1, 2 ** 64 - 1)])
def test_parse_uint_accepts(text, limit, value):
    assert parse_uint(text, "x", limit) == value


@pytest.mark.parametrize("text", BAD + ["11"])
def test_parse_uint_rejects(text):
    with pytest.raises(ValueError):
        parse_uint(text, "x", 10)


def test_the_start_button_uses_the_values_typed_in_the_fields(gui_env, tk_root):
    mock = stream_mock(gui_env)
    tab, _ = build(gui_env, mock=mock, connect=True)
    tab.set_source_mode("Network")
    tab.channel_box.set("E")
    set_entry(tab.blocks_entry, "12")
    tab.flow_box.set("1 realtime single")
    set_entry(tab.mask_entry, "0x10")
    set_entry(tab.expected_entry, "20")
    button(tab, "Start").invoke()  # no explicit apply: Start validates and applies by itself
    assert wait_until(tk_root, lambda: len(mock.received) == 1)
    assert mock.received[0].body[:7] == (4, 12, 1, 0, 0x10, 0, 20)


@pytest.mark.parametrize("letter, number", [(c, i) for i, c in enumerate("ABCDEF")])
def test_every_channel_is_requested_and_stopped_with_its_own_number(gui_env, tk_root, letter, number):
    mock = stream_mock(gui_env)
    tab, _ = build(gui_env, mock=mock, connect=True)
    tab.set_source_mode("Network")
    tab.channel_box.set(letter)
    tab.channel_box.event_generate("<<ComboboxSelected>>")  # what picking the letter in the list does
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == 1)
    assert mock.received[0].body == (number, 0, 0) + (0,) * 9, "start request carries the chosen channel"
    button(tab, "Stop").invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == 2)
    assert mock.received[1].body == (number, 0, 0x80) + (0,) * 9, "stop is for the SAME channel"
    assert [m.body[0] for m in mock.received] == [number, number]


# ------------------------------------------------------------ layout
@pytest.mark.parametrize("scale", [1.0, 2.0])
@pytest.mark.parametrize("size", ["1000x720", "900x600"])
def test_nothing_is_clipped_and_status_block_and_panel_are_visible(window, tk_root, size, scale):
    with laid_out(tk_root, size, scale):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[3]
        settle_layout(tk_root, notebook, tab)
        limit = tk_root.winfo_rootx() + tk_root.winfo_width()
        checked = 0
        for w in descendants(tab):
            if not isinstance(w, (ttk.Button, ttk.Entry, tk.Entry, ttk.Checkbutton)):
                continue
            if w.winfo_height() < w.winfo_reqheight():
                continue  # squeezed vertically in a small window, not horizontal clipping
            name = f"{w.winfo_class()} {str(w)}"
            assert w.winfo_ismapped(), name
            assert w.winfo_width() >= w.winfo_reqwidth(), (name, w.winfo_width(), w.winfo_reqwidth())
            assert w.winfo_rootx() + w.winfo_width() <= limit, name
            checked += 1
        assert checked >= 10, checked
        if scale == 1.0:  # at 200 percent a small window cannot hold everything; the default scale must
            assert tab.status_label.winfo_ismapped()
            assert tab.status_label.winfo_height() >= tab.status_label.winfo_reqheight()
            assert tab.panel.winfo_ismapped() and tab.panel.winfo_height() > 100
            statusbar = tab.panel.winfo_children()[-1]  # the panel's own bottom status bar
            assert statusbar.winfo_ismapped() and statusbar.winfo_height() >= statusbar.winfo_reqheight()
            for text in ("Start", "Stop", "Load config..."):
                b = button(tab, text)
                assert b.winfo_ismapped() and b.winfo_width() >= b.winfo_reqwidth()


# 900x600 expects 4 lines, not 5: the table canvas is a fixed number of pixels (scaled by dpi) while the log is
# measured in text lines, so the log's share of a small window depends on the font. With a 16 px line
# (Windows) it gets 5 lines; with a 17 px line (typical Linux) it gets 78 px, which is 4. 4 is what every
# font keeps; 1000x720 (8 lines) is unchanged.
@pytest.mark.parametrize("size, minimum", [("900x600", 4), ("1000x720", 8)])
def test_the_log_keeps_enough_lines(window, tk_root, size, minimum):
    with laid_out(tk_root, size, 1.0):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[3]
        settle_layout(tk_root, notebook, tab)
        text = tab.log.text
        line_height = int(tk_root.tk.call("font", "metrics", text.cget("font"), "-linespace"))
        assert text.winfo_height() // line_height >= minimum, (text.winfo_height(), line_height)


@pytest.fixture
def window10(tk_root):
    """The app window on a ten-parameter pipeline (the shared `window` fixture has only five rows)."""
    from groundstation.app import build_window
    source = SimFrameSource(PARAMS)
    pipeline = TelemetryPipeline(source, PARAMS)
    created = []

    def build():
        build_window(tk_root, pipeline)
        notebook = [w for w in tk_root.winfo_children() if isinstance(w, ttk.Notebook)][-1]
        created.append(notebook)
        return notebook

    yield build
    for tab in getattr(tk_root, "service_tabs", []):
        tab.shutdown()
    for notebook in created:
        notebook.destroy()
    source.stop()
    tk_root.update()


def visible_table_rows(tab):
    """Parameter rows whose whole bounding box lies inside the table canvas viewport."""
    canvas = tab.panel._canvas
    top, bottom = canvas.winfo_rooty(), canvas.winfo_rooty() + canvas.winfo_height()
    count = 0
    for row in tab.panel.rows:
        cells = list(row.labels.values())
        if (min(c.winfo_rooty() for c in cells) >= top
                and max(c.winfo_rooty() + c.winfo_height() for c in cells) <= bottom):
            count += 1
    return count


@pytest.mark.parametrize("size, min_rows, min_lines", [("1000x720", 8, 5), ("900x600", 4, 3)])
def test_the_table_and_the_log_get_enough_room_and_nothing_is_clipped(
        window10, tk_root, size, min_rows, min_lines):
    with laid_out(tk_root, size, 1.0):
        notebook = window10()
        tab = tk_root.service_tabs[3]
        settle_layout(tk_root, notebook, tab)
        assert len(tab.panel.rows) == 10
        assert visible_table_rows(tab) >= min_rows, (visible_table_rows(tab), tab.panel._canvas.winfo_height())
        text = tab.log.text
        line_height = int(tk_root.tk.call("font", "metrics", text.cget("font"), "-linespace"))
        assert text.winfo_height() // line_height >= min_lines, (text.winfo_height(), line_height)
        # Nothing is clipped: the status block, the panel's status bar and every control are whole.
        assert tab.status_label.winfo_height() >= tab.status_label.winfo_reqheight()
        statusbar = tab.panel.winfo_children()[-1]
        assert statusbar.winfo_ismapped() and statusbar.winfo_height() >= statusbar.winfo_reqheight()
        assert tab.log.winfo_ismapped() and text.winfo_ismapped()
        limit = tk_root.winfo_rootx() + tk_root.winfo_width()
        for w in descendants(tab):
            if isinstance(w, (ttk.Button, ttk.Entry, tk.Entry, ttk.Checkbutton)):
                name = f"{w.winfo_class()} {str(w)}"
                assert w.winfo_ismapped(), name
                assert w.winfo_width() >= w.winfo_reqwidth(), (name, w.winfo_width(), w.winfo_reqwidth())
                assert w.winfo_height() >= w.winfo_reqheight(), (name, w.winfo_height(), w.winfo_reqheight())
                assert w.winfo_rootx() + w.winfo_width() <= limit, name


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_the_table_request_scales_with_the_display_scaling(window10, tk_root, scale):
    from groundstation.tab_telemetry import PANEL_TABLE_HEIGHT
    with laid_out(tk_root, "1000x720", scale):
        window10()
        tab = tk_root.service_tabs[3]
        base = float(tk_root.tk.call("tk", "scaling")) / (96 / 72)       # Tk scaling relative to 96 dpi
        wanted = round(PANEL_TABLE_HEIGHT * tab.winfo_fpixels("1i") / 96.0)
        assert int(tab.panel._canvas.cget("height")) == wanted
        assert abs(wanted - PANEL_TABLE_HEIGHT * base) <= 1              # i.e. it follows the scaling factor


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("size", ["1000x720", "900x600"])
def test_every_control_is_unclipped_at_every_scale(window10, tk_root, size, scale):
    """Horizontal clipping only: at high scaling a small window cannot show everything vertically."""
    with laid_out(tk_root, size, scale):
        notebook = window10()
        tab = tk_root.service_tabs[3]
        settle_layout(tk_root, notebook, tab)
        limit = tk_root.winfo_rootx() + tk_root.winfo_width()
        checked = 0
        for w in descendants(tab):
            if not isinstance(w, (ttk.Button, ttk.Entry, tk.Entry, ttk.Checkbutton, ttk.Combobox)):
                continue
            if w.winfo_height() < w.winfo_reqheight():
                continue                                                  # squeezed vertically, not clipped sideways
            name = f"{w.winfo_class()} {str(w)}"
            assert w.winfo_ismapped(), name
            assert w.winfo_width() >= w.winfo_reqwidth(), (name, w.winfo_width(), w.winfo_reqwidth())
            assert w.winfo_rootx() + w.winfo_width() <= limit, name
            checked += 1
        assert checked >= 10, checked


def test_the_status_block_wraps_instead_of_running_off_the_window(window, tk_root):
    with laid_out(tk_root, "900x600", 2.0):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[3]
        settle_layout(tk_root, notebook, tab)
        assert int(tab.status_label.cget("wraplength")) > 100
        assert int(tab.status_label.cget("wraplength")) <= tab.status_label.winfo_width()


# ------------------------------------------------------------ destroy and shutdown
def test_destroy_cancels_callbacks_unsubscribes_and_leaves_no_thread(gui_env, tk_root):
    errors = []
    previous = tk_root.report_callback_exception
    tk_root.report_callback_exception = lambda *exc: errors.append(exc)
    try:
        store = gui_env.store()
        mock = stream_mock(gui_env)
        subscribers = len(store._subscribers)
        baseline = pending_after_count(tk_root)
        tab, pipeline = build(gui_env, mock=mock, connect=True, store=store)
        assert pending_after_count(tk_root) == baseline + 2  # the tab's pump and the panel's own poll
        client = tab.client
        tab.shutdown()
        assert client.state == ConnState.DISCONNECTED
        assert client._thread is None or not client._thread.is_alive()
        tab.destroy()
        tk_root.update_idletasks()
        assert pending_after_count(tk_root) == baseline
        assert len(store._subscribers) == subscribers
        assert tab.network.prepare is None
        settle(tk_root, 0.3)
        store.set(dataclasses.replace(store.get(), fec_in_frame=True))
        assert errors == []
    finally:
        tk_root.report_callback_exception = previous


def test_shutdown_while_streaming_sends_a_stop_request_first(gui_env, tk_root):
    mock = stream_mock(gui_env)
    tab, _ = build(gui_env, mock=mock, connect=True)
    tab.set_source_mode("Network")
    tab.channel_box.set("B")
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: tab.stream_state == "streaming")
    tab.shutdown()
    assert wait_until(tk_root, lambda: len(mock.received) >= 2)
    assert mock.received[-1].body == (1, 0, 0x80) + (0,) * 9  # the stop for channel B
    assert tab.client.state == ConnState.DISCONNECTED


def test_shutdown_when_not_streaming_sends_nothing(gui_env, tk_root):
    mock = stream_mock(gui_env)
    tab, _ = build(gui_env, mock=mock, connect=True)
    tab.shutdown()
    time.sleep(0.1)
    assert mock.received == []


def test_link_loss_resets_the_stream_state_and_tells_the_source(gui_env):
    tab, _ = build(gui_env)
    tab.network.streaming = True
    tab.network.starts = 1
    tab._poll_source()
    assert tab.stream_state == "requested"
    feed(tab, msg(1))
    assert tab.stream_state == "streaming"
    tab.client.events.put(StateChanged(ConnState.ERROR, "receive failed"))
    tab._pump()
    assert tab.stream_state == "link lost" and tab.network.streaming is False
    assert "stream: link lost" in tab.status_text()


def test_stream_state_goes_idle_requested_streaming_stopped(gui_env):
    tab, _ = build(gui_env)
    assert tab.stream_state == "idle" and "stream: idle" in tab.status_text()
    tab.network.streaming, tab.network.starts = True, 1
    tab._poll_source()
    assert tab.stream_state == "requested"
    feed(tab, msg(1))
    assert tab.stream_state == "streaming"
    tab.network.streaming = False  # the panel's Stop button
    tab._poll_source()
    assert tab.stream_state == "stopped"


# ------------------------------------------------------------ app wiring
def test_default_window_has_a_telemetry_tab_on_the_telemetry_client(window, tk_root):
    panel, notebook, source = window()
    tabs = [notebook.nametowidget(n) for n in notebook.tabs()]
    assert isinstance(tabs[3], TelemetryTab) and tabs[3] is tk_root.service_tabs[3] and tabs[3].panel is panel
    assert isinstance(tabs[3].client, TelemetryClient)
    assert (tabs[3].client.host, tabs[3].client.port) == ("127.0.0.1", 3070) and tabs[3].port_entry.get() == "3070"
    assert len(tk_root.service_tabs) == 4


def test_a_plain_pipeline_keeps_working_and_starts_in_simulated_mode(window, tk_root):
    panel, notebook, sim = window()
    tab = tk_root.service_tabs[3]
    pipeline = tab.pipeline
    assert tab.switch is pipeline.source and tab.switch.simulated is sim and tab.switch.mode == "Simulated"
    assert tab.source_box.get() == "Simulated"
    pipeline.source.start()  # exactly what the program does at launch
    try:
        assert wait_until(tk_root, lambda: pipeline.frames_total >= 1, timeout=2.0)
        assert panel.rows[0].last_update is not None  # the panel shows the simulated values
    finally:
        sim.stop()


def test_host_and_port_offset_apply_to_the_telemetry_tab(window, tk_root):
    window(host="10.1.2.3", port_offset=100)
    assert (tk_root.service_tabs[3].client.host, tk_root.service_tabs[3].client.port) == ("10.1.2.3", 3170)


def test_a_ready_made_telemetry_client_can_be_passed_in(window, tk_root):
    client = TelemetryClient("192.0.2.9", 4000)
    window(clients={"telemetry": client})
    assert tk_root.service_tabs[3].client is client and tk_root.service_tabs[3].network.client is client


def test_an_already_switchable_pipeline_is_not_wrapped_twice(gui_env):
    tab, pipeline = build(gui_env)
    first = pipeline.source
    again = gui_env.make_tab(lambda f, c, s, pump_ms: TelemetryTab(f, c, s, pipeline, pump_ms=pump_ms),
                             TelemetryClient)
    assert pipeline.source is first and again.switch is first


def test_close_path_stops_a_running_network_stream(window, tk_root, gui_env):
    from groundstation.app import shutdown_window
    mock = stream_mock(gui_env)
    panel, notebook, sim = window()
    tab = tk_root.service_tabs[3]
    set_entry(tab.port_entry, str(mock.port))
    tab.toggle_connection()
    assert wait_until(tk_root, lambda: tab.state_label.cget("text") == "CONNECTED")
    tab.set_source_mode("Network")
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: tab.stream_state == "streaming")
    shutdown_window(tk_root, tab.pipeline.source)
    assert wait_until(tk_root, lambda: any(m.body[2] == 0x80 for m in mock.received))
    assert tab.client.state == ConnState.DISCONNECTED
