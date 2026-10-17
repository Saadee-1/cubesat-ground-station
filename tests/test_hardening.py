"""Small behaviours that each have a test which fails without them.

a) Control: the Value box follows the selected row.
b) Telecommand hex parser: every group of digits must be even.
c) Telecommand: exactly one log entry per reply.
d) Telemetry: frames that arrive after Stop never reach the table and are counted.
"""
import re
import socket
import struct
import time
from pathlib import Path

import pytest

from groundstation import guibase
from groundstation.clients import ControlClient, TelecommandClient, TelemetryClient
from groundstation.config import load_config
from groundstation.cop1 import (AcceptReject, AdditionalInfo, AlertIndication, CltuEcho, ConfirmResponse,
                                NegativeResponse, SegmentEcho, SuspendIndication)
from groundstation.guibase import set_checkbutton, set_entry
from groundstation.net import Received, Sent
from groundstation.protocol import ProtocolOptions, build_message, parse_message
from groundstation.cortex import build_stop_request, build_tm_request
from groundstation.source import SimFrameSource
from groundstation.tab_control import ControlTab
from groundstation.tab_telecommand import TelecommandTab, describe_reply, parse_hex_bytes
from groundstation.tab_telemetry import TelemetryTab
from groundstation.tm import TelemetryPipeline
from gui_util import gui_env, has_log_line, settle, wait_until  # noqa: F401
from scripted_server import scripted  # noqa: F401

PARAMS = load_config(str(Path(guibase.__file__).parent / "samples" / "sample_config_10.json"))
TIMESTAMPED = re.compile(r"^\d\d:\d\d:\d\d ")


# ------------------------------------------------------------ a) Control: the Value box follows the row
def control_tab(gui_env):
    return gui_env.make_tab(ControlTab, ControlClient)


def stage(tab, pid, text):
    tab.select(pid)
    set_entry(tab.value_entry, text)
    tab.stage_button.invoke()


def test_value_box_shows_the_staged_value_of_the_selected_row_or_nothing(gui_env):
    tab = control_tab(gui_env)
    stage(tab, 7, "5")
    stage(tab, 9, "0x10")
    tab.select(3)
    assert tab.value_entry.get() == ""               # row 3 has nothing staged: not row 9's text
    tab.select(7)
    assert tab.value_entry.get() == "0x00000005"
    tab.select(9)
    assert tab.value_entry.get() == "0x00000010"
    tab.select(3)
    assert tab.value_entry.get() == ""


def test_text_typed_for_one_row_does_not_follow_the_selection(gui_env):
    tab = control_tab(gui_env)
    tab.select(3)
    set_entry(tab.value_entry, "12345")              # typed, not staged
    tab.select(4)
    assert tab.value_entry.get() == ""
    stage(tab, 4, "7")
    tab.select(3)
    assert tab.value_entry.get() == ""
    assert tab.staged == {4: 7}


def test_value_box_after_unstage_clear_and_send(gui_env):
    tab = control_tab(gui_env)
    stage(tab, 7, "5")
    tab.select(7)
    tab.unstage_button.invoke()
    tab.select(8)
    tab.select(7)
    assert tab.value_entry.get() == ""
    stage(tab, 7, "6")
    tab.clear_button.invoke()
    tab.select(8)
    tab.select(7)
    assert tab.value_entry.get() == ""


def test_a_real_click_selection_updates_the_value_box_too(gui_env):
    tab = control_tab(gui_env)
    stage(tab, 7, "5")
    tab.tree.selection_set("3")                      # the <<TreeviewSelect>> path, not select()
    gui_env.root.update()
    assert tab.value_entry.get() == ""
    tab.tree.selection_set("7")
    gui_env.root.update()
    assert tab.value_entry.get() == "0x00000005"
    tab.tree.selection_set(())
    gui_env.root.update()
    assert tab.value_entry.get() == "" and tab.selected_label.cget("text") == "Selected: none"


def test_the_staged_text_can_be_restaged_unchanged(gui_env):
    tab = control_tab(gui_env)
    stage(tab, 7, "0xDEADBEEF")
    tab.select(3)
    tab.select(7)
    tab.stage_button.invoke()                        # whatever the box shows must be a valid value
    assert tab.staged == {7: 0xDEADBEEF}


# ------------------------------------------------------------ b) hex groups
@pytest.mark.parametrize("text, expected", [
    ("AB CD", b"\xab\xcd"), ("AB,CD", b"\xab\xcd"), ("AB:CD", b"\xab\xcd"), ("0xABCD", b"\xab\xcd"),
    ("0xAB CD", b"\xab\xcd"), ("0x AB CD", b"\xab\xcd"), ("ABCD", b"\xab\xcd"), ("ab cd", b"\xab\xcd"),
    ("AB  CD", b"\xab\xcd"), (" AB CD ", b"\xab\xcd"), ("AB, CD", b"\xab\xcd"), ("AB : CD", b"\xab\xcd"),
    ("AB\tCD", b"\xab\xcd"), ("0102 03", b"\x01\x02\x03"), ("00", b"\x00"), ("ABCD EF", b"\xab\xcd\xef"),
    ("01:02,03 04", b"\x01\x02\x03\x04"), ("0X0A", b"\x0a"), (",AB,", b"\xab"), (":AB:CD:", b"\xab\xcd")])
def test_hex_groups_that_are_even_pass(text, expected):
    assert parse_hex_bytes(text) == expected


@pytest.mark.parametrize("text", [
    "0 1", "A BCD", "ABC D", "A", "AB C", "0x A", "ABC", "AB CDE", "0xA BCD", "A,B", "A:B", "A B C D",
    "0xA", "ABC,DE", "1 23 45", "AB CD E", "0x0 1"])
def test_an_odd_group_is_rejected_with_a_clear_message(text):
    with pytest.raises(ValueError, match="even number of digits") as caught:
        parse_hex_bytes(text)
    assert "in every group" in str(caught.value)


def test_the_odd_group_is_named_in_the_message():
    with pytest.raises(ValueError) as caught:
        parse_hex_bytes("AB C DE")
    assert "'C'" in str(caught.value) and "has 1" in str(caught.value)


def test_the_live_length_label_shows_the_group_problem(gui_env):
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient)
    set_entry(tab.data_entry, "0 1")
    tab.update_length_label()
    assert "even number of digits" in tab.length_text()
    set_entry(tab.data_entry, "AB CD")
    tab.update_length_label()
    assert tab.length_text().startswith("2 bytes")


def test_send_refuses_an_odd_group_and_sends_nothing(gui_env, scripted):
    server = scripted()
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient, port=server.port, connect=True)
    set_entry(tab.data_entry, "A BCD")
    tab.send_button.invoke()
    assert tab.message_text().startswith("Not sent:") and "even number" in tab.message_text()
    settle(gui_env.root, 0.1)
    assert bytes(server.received) == b"" and tab.requests == {}


# ------------------------------------------------------------ c) one log entry per reply
def entries(tab):
    return [line for line in tab.log.lines() if TIMESTAMPED.match(line)]


def received_event(body, parsed):
    msg = parse_message(build_message(0, body))
    return Received(message=msg, parsed=parsed, at=0.0)


NORMAL_REPLIES = [
    ("accept", [0, 30, 1, 5, 0], AcceptReject(op=30, vcid=1, tag=5, accepted=True)),
    ("reject", [0, 30, 1, 5, 1], AcceptReject(op=30, vcid=1, tag=5, accepted=False)),
    ("confirm", [1, 20, 1, 5, 0], ConfirmResponse(op=20, vcid=1, tag=5, positive=True)),
    ("segment echo", [4, 30, 5, 9, 2, 0x01020000], SegmentEcho(op=30, field=1, tag=5, length=2, data=b"\x01\x02")),
    ("cltu echo", [5, 0, 5, 9, 1, 2, 3, 0xAA000000],
     CltuEcho(vcid=1, tag=5, frame_sequence=3, error_location=2, length=1, data=b"\xaa")),
    ("alert", [2, 0, 1, 5], AlertIndication(vcid=1, code=5, name="CLCW")),
    ("suspend", [3, 0, 2], SuspendIndication(vcid=2)),
    ("additional info", [6, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
     AdditionalInfo(vcid=1, last_clcw=2, state=3, last_event=4, v_s=5, nn_r=6, n_r=7, timer_initial=8,
                    timeout_type=9, transmission_limit=10, transmission_count=11, suspended=0, sliding_window=12)),
]


@pytest.mark.parametrize("raw_words", [False, True])
@pytest.mark.parametrize("name, body, parsed", NORMAL_REPLIES, ids=[n for n, _, _ in NORMAL_REPLIES])
def test_every_normal_reply_writes_exactly_one_log_entry(gui_env, name, body, parsed, raw_words):
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient)
    tab._add_row(5, "BD segment", "1", "5")
    set_checkbutton(tab.log.raw_check, raw_words)
    before = len(entries(tab))
    tab._dispatch(received_event(body, parsed))
    new = entries(tab)[before:]
    assert len(new) == 1, new
    assert "RECEIVED" in new[0] and describe_reply(parsed) in new[0] and " bytes)" in new[0]
    lines = tab.log.lines()
    raw_lines = [line for line in lines if line.startswith("    [")]
    assert bool(raw_lines) == raw_words              # the raw-word toggle behaves as before
    assert tab.received_count == 1


def test_the_unrecognised_failure_replies_keep_their_extra_notes_but_one_received_entry(gui_env):
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient)
    tab._add_row(1, "BD segment", "1", "5")
    tab._dispatch(received_event([-1], NegativeResponse()))
    received = [e for e in entries(tab) if "RECEIVED" in e]
    assert len(received) == 1                        # the error line and the attribution note are separate topics
    assert has_log_line(tab, "Server answered with NegativeResponse") and has_log_line(tab, "oldest pending")


def test_a_real_round_trip_gives_one_received_entry_per_reply(gui_env, tk_root):
    mock = gui_env.mock("telecommand")
    tab = gui_env.make_tab(TelecommandTab, TelecommandClient, mock=mock, connect=True)
    tab.set_operation("AD segment")                  # the mock answers an AD segment with accept AND confirm
    set_entry(tab.data_entry, "01 02")
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.received_count == 2)
    settle(tk_root, 0.1)
    assert len([e for e in entries(tab) if "RECEIVED" in e]) == 2
    assert len([e for e in entries(tab) if "SENT" in e]) == 1
    assert tab.status_of(1).startswith("confirmed")


# ------------------------------------------------------------ d) Telemetry: frames after Stop
def telemetry_tab(gui_env, server=None, mock=None):
    pipeline = TelemetryPipeline(SimFrameSource(PARAMS, rate_hz=50), PARAMS)
    port = server.port if server is not None else None
    tab = gui_env.make_tab(lambda f, c, s, pump_ms: TelemetryTab(f, c, s, pipeline, pump_ms=pump_ms),
                           TelemetryClient, mock=mock, port=port, connect=server is not None or mock is not None)
    tab.set_source_mode("Network")
    return tab, pipeline


def frame_body(sequence):
    return [111, 222, sequence, 0, 1, 0, 0, 8, 16, 0, 0, 0, 0, 0x01020304, 0x05060708]


def frame_event(sequence):
    from groundstation.cortex import parse_tm_message
    msg = parse_message(build_message(0, frame_body(sequence)))
    return Received(message=msg, parsed=parse_tm_message(msg), at=0.0)


def sent_event(raw):
    return Sent(raw=raw, description="TM request", tag=None)


def start_raw(channel=0):
    return build_tm_request(channel, 0, 0, ProtocolOptions())


def stop_raw(channel=0):
    return build_stop_request(channel, ProtocolOptions())


def test_frames_behind_a_stop_request_are_ignored_counted_and_the_first_is_logged(gui_env):
    tab, pipeline = telemetry_tab(gui_env)
    tab._dispatch(sent_event(start_raw()))
    for n in (1, 2, 3):
        tab._dispatch(frame_event(n))
    assert tab.counters["received"] == tab.counters["forwarded"] == 3 and tab.counters["ignored_after_stop"] == 0
    assert tab.network.frames.qsize() == 3
    tab._dispatch(sent_event(stop_raw()))
    for n in (4, 5, 6, 7):
        tab._dispatch(frame_event(n))
    c = tab.counters
    assert c["ignored_after_stop"] == 4
    assert c["received"] == c["forwarded"] == 3      # nothing after the Stop reached the table
    assert tab.network.frames.qsize() == 3
    assert c["last_sequence"] == 3 and c["gaps"] == 0  # and they did not disturb the sequence statistics
    assert "ignored after stop 4" in tab.status_text()
    logged = [line for line in tab.log.lines() if "arrived after Stop" in line]
    assert len(logged) == 1 and "sequence 4" in logged[0]


def test_a_new_start_makes_frames_count_again(gui_env):
    tab, pipeline = telemetry_tab(gui_env)
    tab._dispatch(sent_event(start_raw()))
    tab._dispatch(frame_event(1))
    tab._dispatch(sent_event(stop_raw()))
    tab._dispatch(frame_event(2))
    assert tab.counters["ignored_after_stop"] == 1
    tab._dispatch(sent_event(start_raw()))           # Start again, no pump in between
    tab._dispatch(frame_event(3))
    tab._dispatch(frame_event(4))
    assert tab.counters["ignored_after_stop"] == 1   # the earlier one stays counted
    assert tab.counters["received"] == 3 and tab.counters["forwarded"] == 3
    assert tab.network.frames.qsize() == 3


def test_the_stop_of_another_channel_and_a_start_of_another_flow_are_still_stop_and_start(gui_env):
    tab, _ = telemetry_tab(gui_env)
    tab._dispatch(sent_event(stop_raw(channel=3)))
    tab._dispatch(frame_event(1))
    assert tab.counters["ignored_after_stop"] == 1
    tab._dispatch(sent_event(build_tm_request(2, 10, 5, ProtocolOptions())))   # offline single flow
    tab._dispatch(frame_event(2))
    assert tab.counters["forwarded"] == 1


def test_frames_without_any_stop_are_forwarded_as_today(gui_env):
    tab, _ = telemetry_tab(gui_env)
    for n in (1, 2, 3):
        tab._dispatch(frame_event(n))                # unsolicited frames, no Start seen
    assert tab.counters["forwarded"] == 3 and tab.counters["ignored_after_stop"] == 0


def test_an_unparseable_sent_event_is_harmless(gui_env):
    tab, _ = telemetry_tab(gui_env)
    tab._dispatch(sent_event(b"not a message"))
    tab._dispatch(sent_event(b""))
    tab._dispatch(frame_event(1))
    assert tab.counters["forwarded"] == 1


def test_switching_the_source_forgets_a_stop(gui_env):
    tab, _ = telemetry_tab(gui_env)
    tab._dispatch(sent_event(stop_raw()))
    tab.set_source_mode("Simulated")
    tab.set_source_mode("Network")
    tab._dispatch(frame_event(1))
    assert tab.counters["forwarded"] == 1 and tab.counters["ignored_after_stop"] == 0


def stubborn_server(server, conn):
    """Streams frames at about 100 per second from the first Start and NEVER honours Stop."""
    state = {"go": False}

    sequence = 0
    conn.settimeout(0.01)
    while not server.stopping.is_set():
        try:
            data = conn.recv(4096)
            if not data:
                return
            server.received += data
            state["go"] = True
        except socket.timeout:
            pass
        except OSError:
            return
        if state["go"]:
            sequence += 1
            conn.sendall(build_message(0, frame_body(sequence)))
            time.sleep(0.01)


def test_a_server_that_ignores_stop_never_moves_the_table_again_and_the_counter_shows_it(gui_env, scripted, tk_root):
    from test_tab_telemetry import button
    server = scripted(stubborn_server)
    tab, pipeline = telemetry_tab(gui_env, server=server)
    button(tab, "Start").invoke()
    assert wait_until(tk_root, lambda: tab.counters["forwarded"] >= 5 and tab.stream_state == "streaming")
    before_stop = tab.counters["forwarded"]
    button(tab, "Stop").invoke()
    assert wait_until(tk_root, lambda: tab.counters["ignored_after_stop"] >= 5)
    forwarded = tab.counters["forwarded"]
    assert forwarded >= before_stop                  # frames forwarded before the Stop stay counted
    assert wait_until(tk_root, lambda: tab.network.frames.empty() and pipeline.frames_total == forwarded)
    shown_before = pipeline.frames_total
    updates_before = [row.last_update for row in tab.panel.rows]
    first_ignored = tab.counters["ignored_after_stop"]
    settle(tk_root, 0.5)
    assert tab.counters["ignored_after_stop"] > first_ignored      # the server really is still sending
    assert tab.counters["forwarded"] == forwarded and pipeline.frames_total == shown_before
    assert [row.last_update for row in tab.panel.rows] == updates_before   # the table did not move
    assert str(tab.stream_state) == "stopped"
    # A new Start from the panel counts frames again and starts the ignored counter from zero.
    button(tab, "Start").invoke()
    # Wait for the reset itself (the old counters are still there until the pump sees the new Start), then
    # for new frames; the ignored counter must be zero in the new stream.
    assert wait_until(tk_root, lambda: tab.counters["ignored_after_stop"] == 0 and tab.counters["forwarded"] >= 3)
    assert tab.stream_state in ("requested", "streaming")
    settle(tk_root, 0.2)
    assert tab.counters["ignored_after_stop"] == 0 and tab.counters["forwarded"] >= 3
