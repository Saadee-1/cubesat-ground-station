"""A server that accepts the connection and never answers must be visible.

Every test uses a fake clock (guibase.now is replaced), so none of them sleeps for a timeout.
The server is a ScriptedServer that accepts and discards everything.
"""
import types
from pathlib import Path

import pytest

from groundstation import guibase, tab_telecommand
from groundstation.clients import ControlClient, MonitoringClient, TelecommandClient, TelemetryClient
from groundstation.config import load_config
from groundstation.cop1 import AcceptReject, ControlAck, MonitoringReport, NegativeResponse
from groundstation.cortex import TelemetryMessage
from groundstation.guibase import (DEFAULT_REPLY_TIMEOUT_S, HINT, OptionsStore, RED, parse_reply_timeout,
                                   set_checkbutton, set_entry)
from groundstation.net import ConnState, StateChanged
from groundstation.source import SimFrameSource
from groundstation.tab_control import ControlTab
from groundstation.tab_monitoring import MonitoringTab, TIMEOUT_TEXT
from groundstation.tab_telecommand import (OP_AD, OP_BD, OP_CLTU, OP_DIRECTIVE, OP_FRAME, OP_NOP, OP_SUBSCRIBE,
                                           OPERATIONS, STATUS_TIMEOUT, TelecommandTab)
from groundstation.tab_telemetry import TelemetryTab
from groundstation.tm import TelemetryPipeline
from gui_util import gui_env, has_log_line, settle, wait_until  # noqa: F401
from scripted_server import scripted  # noqa: F401

PARAMS = load_config(str(Path(guibase.__file__).parent / "samples" / "sample_config_10.json"))


class FakeClock:
    """Stands in for guibase.now: time only moves when the test says so."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(guibase, "now", fake)
    return fake


def reply(parsed):
    return types.SimpleNamespace(parsed=parsed)


def timeout_lines(tab, fragment):
    return [line for line in tab.log.lines() if fragment in line]


# ------------------------------------------------------------ tab builders (connected to a silent server)
def control_tab(gui_env, server, store=None):
    tab = gui_env.make_tab(ControlTab, ControlClient, port=server.port, connect=True, store=store)
    return tab


def stage_and_send(tab, *pairs):
    for pid, value in pairs:
        tab.select(pid)
        set_entry(tab.value_entry, str(value))
        tab.stage_button.invoke()
    tab.send_button.invoke()
    return max(tab.pending_tags)  # the newest tag is the highest here (tags count up from 1)


def monitoring_tab(gui_env, server, store=None):
    return gui_env.make_tab(MonitoringTab, MonitoringClient, port=server.port, connect=True, store=store)


def telecommand_tab(gui_env, server, store=None):
    return gui_env.make_tab(TelecommandTab, TelecommandClient, port=server.port, connect=True, store=store)


def telemetry_tab(gui_env, server, store=None):
    pipeline = TelemetryPipeline(SimFrameSource(PARAMS, rate_hz=50), PARAMS)
    tab = gui_env.make_tab(lambda f, c, s, pump_ms: TelemetryTab(f, c, s, pipeline, pump_ms=pump_ms),
                           TelemetryClient, port=server.port, connect=True, store=store)
    tab.set_source_mode("Network")
    return tab


def send_bd(tab, data="01"):
    set_entry(tab.data_entry, data)
    tab.send_button.invoke()
    return max(tab.requests)


def report(tag=1):
    return MonitoringReport(tag=tag, config=(0,) * 40, status=(0,) * 20)


def frame_message(sequence):
    return TelemetryMessage(time_tag_1=0, time_tag_2=0, sequence=sequence, frame_check_raw=0, sync_status=1,
                            bit_slip=0, tm_delay=0, frame_length=8, sync_word_length=16, decoder_status=0,
                            transmitted_data=0, frame=b"\x00" * 8, length_consistent=True)


# ------------------------------------------------------------ the setting itself
def test_default_and_range_of_the_setting():
    store = OptionsStore()
    assert store.reply_timeout == DEFAULT_REPLY_TIMEOUT_S == 5.0
    for good in (0.5, 5, 300, 300.0, 12.25):
        store.set_reply_timeout(good)
        assert store.reply_timeout == float(good)
    for bad in (0.49, 0, -1, 300.01, 1000, float("nan"), float("inf"), -float("inf")):
        before = store.reply_timeout
        with pytest.raises(ValueError):
            store.set_reply_timeout(bad)
        assert store.reply_timeout == before


@pytest.mark.parametrize("text, value", [("0.5", 0.5), ("5", 5.0), (" 2.5 ", 2.5), ("300", 300.0),
                                         ("10.25", 10.25), ("00.5", 0.5), ("0300", 300.0)])
def test_timeout_parser_accepts(text, value):
    assert parse_reply_timeout(text) == value


@pytest.mark.parametrize("text", [
    "", " ", "0", "0.0", "0.49", "0.4999", "300.0001", "301", "1000", "-1", "-0.5", "+5", "5_0", "1_0.5",
    "1e1", "1E1", "5e-1", "nan", "inf", "Infinity", ".5", "5.", "5,5", "5.5.5", "0x10", "five", "5 s",
    "5\n6", chr(0x665), "5." + chr(0x665), chr(0xFF15), "٥٠"])
def test_timeout_parser_rejects(text):
    with pytest.raises(ValueError, match="0.5 to 300"):
        parse_reply_timeout(text)


# ------------------------------------------------------------ the dialog
def test_dialog_shows_the_current_value_and_applies_a_new_one(gui_env):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    assert dialog.timeout_entry.get() == "5"
    set_entry(dialog.timeout_entry, "12.5")
    assert dialog.apply() is True
    assert store.reply_timeout == 12.5 and dialog.message.cget("text") == "Applied."
    store.set_reply_timeout(8)
    dialog.destroy()
    assert gui_env.dialog(store).timeout_entry.get() == "8"  # a new dialog starts from the stored value


@pytest.mark.parametrize("text", ["", "0.1", "301", "5_0", "-3", "+3", "1e1", "nan", ".5", chr(0x665)])
def test_dialog_rejects_a_bad_timeout_and_keeps_the_old_value_and_everything_else(gui_env, text):
    store = gui_env.store()
    store.set_reply_timeout(7)
    before = store.get()
    dialog = gui_env.dialog(store)
    set_checkbutton(dialog.fec_check, not before.fec_in_frame)  # a valid change in the same Apply
    set_entry(dialog.timeout_entry, text)
    assert dialog.apply() is False
    assert store.reply_timeout == 7.0 and store.get() == before  # nothing was half applied
    assert str(dialog.message.cget("fg")) == RED and "0.5 to 300" in dialog.message.cget("text")


def test_dialog_a_bad_flow_id_stops_a_valid_new_timeout_from_being_applied(gui_env):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    set_entry(dialog.timeout_entry, "9")
    set_entry(dialog.flow_entries["control_flow_id"], "not a number")
    assert dialog.apply() is False
    assert store.reply_timeout == 5.0  # all or nothing: the valid timeout waits for a valid form


def test_dialog_timeout_applies_only_to_requests_sent_afterwards(gui_env, scripted, clock):
    server = scripted()
    store = gui_env.store()
    tab = control_tab(gui_env, server, store)
    first = stage_and_send(tab, (1, 10))             # sent under the default 5 s
    dialog = gui_env.dialog(store)
    set_entry(dialog.timeout_entry, "20")
    assert dialog.apply() is True
    second = stage_and_send(tab, (2, 20))            # sent under 20 s
    clock.advance(6)
    tab.on_tick()
    assert tab.cell(1, "sent") == "no response (timeout)" and tab.cell(2, "sent") == "pending"
    clock.advance(15)                                # 21 s after the second request
    tab.on_tick()
    assert tab.cell(2, "sent") == "no response (timeout)"
    assert first != second
    assert [("5 s" in line) for line in timeout_lines(tab, "got no reply")] == [True, False]
    assert "20 s" in timeout_lines(tab, "got no reply")[1]


# ------------------------------------------------------------ Control
def test_control_pending_row_becomes_timeout_once_and_logs_one_line_with_the_hint(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    tag = stage_and_send(tab, (1, 10), (2, 20))
    assert tab.cell(1, "sent") == tab.cell(2, "sent") == "pending"
    clock.advance(4.9)
    tab.on_tick()
    assert tab.cell(1, "sent") == "pending" and not timeout_lines(tab, "got no reply")
    clock.advance(0.1)
    tab.on_tick()
    assert tab.cell(1, "sent") == tab.cell(2, "sent") == "no response (timeout)"
    for _ in range(5):                                # once only, however often the pump looks
        clock.advance(10)
        tab.on_tick()
    lines = timeout_lines(tab, "got no reply")
    assert len(lines) == 1 and f"tag {tag}" in lines[0] and HINT in lines[0]
    assert tab.error_count == 1


def test_control_late_ack_replaces_the_timeout(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    tag = stage_and_send(tab, (1, 10), (2, 20))
    clock.advance(30)
    tab.on_tick()
    assert tab.cell(1, "sent") == "no response (timeout)"
    tab.on_received(reply(ControlAck(tag=tag, status=0)))
    assert tab.cell(1, "sent") == tab.cell(2, "sent") == "ack 0"   # the timeout was never a discard
    assert tab.pending_tags == {} and tab.pending_due == {} and tab.timed_out_tags == set()
    clock.advance(30)
    tab.on_tick()
    assert tab.cell(1, "sent") == "ack 0" and len(timeout_lines(tab, "got no reply")) == 1


def test_control_reply_inside_the_timeout_never_shows_it(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    tag = stage_and_send(tab, (1, 10))
    clock.advance(4)
    tab.on_received(reply(ControlAck(tag=tag, status=0)))
    clock.advance(500)
    tab.on_tick()
    assert tab.cell(1, "sent") == "ack 0" and not timeout_lines(tab, "got no reply") and tab.error_count == 0


def test_control_late_tagless_negative_reply_goes_to_the_oldest_even_if_it_timed_out(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    stage_and_send(tab, (1, 10))
    clock.advance(6)
    stage_and_send(tab, (2, 20))                       # sent later, still inside its own timeout
    tab.on_tick()
    assert tab.cell(1, "sent") == "no response (timeout)" and tab.cell(2, "sent") == "pending"
    tab.on_received(reply(NegativeResponse()))
    assert tab.cell(1, "sent") == "rejected (-1)" and tab.cell(2, "sent") == "pending"


def test_control_disconnect_still_gives_no_response_disconnected(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    stage_and_send(tab, (1, 10))
    stage_and_send(tab, (2, 20))
    clock.advance(6)
    tab.on_tick()
    stage_and_send(tab, (3, 30))                       # one timed out, one still inside its timeout
    assert (tab.cell(1, "sent"), tab.cell(3, "sent")) == ("no response (timeout)", "pending")
    tab.client.disconnect()
    assert wait_until(gui_env.root, lambda: tab.cell(1, "sent") == "no response (disconnected)")
    assert [tab.cell(i, "sent") for i in (1, 2, 3)] == ["no response (disconnected)"] * 3
    assert tab.pending_due == {} and tab.timed_out_tags == set()


def test_control_resent_parameter_keeps_the_newer_pending_status(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    stage_and_send(tab, (1, 10))
    clock.advance(3)
    stage_and_send(tab, (1, 11))                       # the same id again, a newer request
    clock.advance(3)                                   # the first is 6 s old, the second 3 s
    tab.on_tick()
    assert tab.cell(1, "sent") == "pending"            # the older tag timing out must not hide the newer one
    assert len(timeout_lines(tab, "got no reply")) == 1


def test_control_pump_runs_the_deadline_check(gui_env, scripted, clock):
    server = scripted()
    tab = control_tab(gui_env, server)
    stage_and_send(tab, (1, 10))
    clock.advance(10)
    assert wait_until(gui_env.root, lambda: tab.cell(1, "sent") == "no response (timeout)", timeout=3)


def test_a_failing_deadline_check_does_not_kill_the_pump_and_is_logged_once(gui_env, scripted):
    server = scripted()
    tab = control_tab(gui_env, server)

    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("deadline check broke")

    tab.on_tick = boom
    # wait for several pumps instead of guessing how many fit into a fixed time on a loaded machine
    assert wait_until(gui_env.root, lambda: len(calls) >= 5, timeout=10)
    assert len(timeout_lines(tab, "internal error in the timeout check")) == 1
    assert tab._after_id is not None                   # the pump rescheduled itself every time
    tab.client.events.put(StateChanged(ConnState.ERROR, "x"))
    assert wait_until(gui_env.root, lambda: tab.state_label.cget("text") == "ERROR")  # and still dispatches


# ------------------------------------------------------------ Telecommand
def test_telecommand_pending_row_times_out_once_late_reply_wins(gui_env, scripted, clock):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    tag = send_bd(tab)
    assert tab.status_of(tag) == "pending"
    clock.advance(4.9)
    tab.on_tick()
    assert tab.status_of(tag) == "pending"
    clock.advance(0.1)
    for _ in range(4):
        tab.on_tick()
        clock.advance(1)
    assert tab.status_of(tag) == STATUS_TIMEOUT == "no response (timeout)"
    lines = timeout_lines(tab, "got no reply")
    assert len(lines) == 1 and f"tag {tag}" in lines[0] and HINT in lines[0]
    tab.on_received(reply(AcceptReject(op=30, vcid=0, tag=tag, accepted=True)))
    assert tab.status_of(tag) == "accepted"            # a late reply is processed, not discarded
    clock.advance(100)
    tab.on_tick()
    assert tab.status_of(tag) == "accepted" and len(timeout_lines(tab, "got no reply")) == 1


def test_telecommand_reply_inside_the_timeout_never_shows_it(gui_env, scripted, clock):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    tag = send_bd(tab)
    clock.advance(2)
    tab.on_received(reply(AcceptReject(op=30, vcid=0, tag=tag, accepted=True)))
    clock.advance(500)
    tab.on_tick()
    assert tab.status_of(tag) == "accepted" and not timeout_lines(tab, "got no reply")


def test_telecommand_a_row_that_was_accepted_is_not_timed_out_while_it_waits_for_the_confirm(
        gui_env, scripted, clock):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    tag = send_bd(tab)
    tab.on_received(reply(AcceptReject(op=30, vcid=0, tag=tag, accepted=True)))
    clock.advance(500)
    tab.on_tick()
    assert tab.status_of(tag) == "accepted" and tab.error_count == 0


def test_telecommand_timed_out_request_is_still_the_oldest_for_a_tagless_reply(gui_env, scripted, clock):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    first = send_bd(tab, "01")
    clock.advance(6)
    second = send_bd(tab, "02")
    tab.on_tick()
    assert (tab.status_of(first), tab.status_of(second)) == (STATUS_TIMEOUT, "pending")
    tab.on_received(reply(NegativeResponse()))
    assert (tab.status_of(first), tab.status_of(second)) == ("rejected (-1)", "pending")


def test_telecommand_disconnect_still_gives_no_response_disconnected(gui_env, scripted, clock):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    first = send_bd(tab, "01")
    clock.advance(6)
    second = send_bd(tab, "02")
    tab.on_tick()
    assert (tab.status_of(first), tab.status_of(second)) == (STATUS_TIMEOUT, "pending")
    tab.client.disconnect()
    assert wait_until(gui_env.root, lambda: tab.status_of(first) == "no response (disconnected)")
    assert tab.status_of(second) == "no response (disconnected)"


def fill_for(tab, operation):
    tab.set_operation(operation)
    if operation in (OP_BD, OP_AD, OP_CLTU):
        set_entry(tab.data_entry, "01")
    elif operation == OP_FRAME:
        set_entry(tab.data_entry, "00" * 8)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_telecommand_operation_currently_expects_a_reply_and_times_out(gui_env, scripted, clock, operation):
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    fill_for(tab, operation)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Sent "), tab.message_text()
    tag = max(tab.requests)
    clock.advance(6)
    tab.on_tick()
    assert tab.status_of(tag) == STATUS_TIMEOUT


def test_an_operation_that_does_not_expect_a_reply_is_never_timed_out(gui_env, scripted, clock, monkeypatch):
    monkeypatch.setattr(tab_telecommand, "EXPECTS_REPLY", frozenset(OPERATIONS) - {OP_NOP})
    server = scripted()
    tab = telecommand_tab(gui_env, server)
    fill_for(tab, OP_NOP)
    tab.send_button.invoke()
    nop = max(tab.requests)
    fill_for(tab, OP_BD)
    tab.send_button.invoke()
    bd = max(tab.requests)
    clock.advance(600)
    tab.on_tick()
    assert tab.status_of(nop) == "pending" and tab.status_of(bd) == STATUS_TIMEOUT
    assert len(timeout_lines(tab, "got no reply")) == 1


def test_telecommand_changing_the_timeout_applies_to_later_requests_only(gui_env, scripted, clock):
    server = scripted()
    store = gui_env.store()
    tab = telecommand_tab(gui_env, server, store)
    first = send_bd(tab, "01")
    store.set_reply_timeout(60)
    second = send_bd(tab, "02")
    clock.advance(6)
    tab.on_tick()
    assert (tab.status_of(first), tab.status_of(second)) == (STATUS_TIMEOUT, "pending")


# ------------------------------------------------------------ Monitoring
def test_monitoring_silent_server_sets_the_stale_text_even_before_any_report(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    assert tab.report is None
    tab.request()
    clock.advance(4.9)
    tab.on_tick()
    assert tab.stale_text() == ""
    clock.advance(0.1)
    tab.on_tick()
    assert tab.stale_text() == TIMEOUT_TEXT.format(seconds=5.0) and "5 s" in tab.stale_text()
    for _ in range(4):
        clock.advance(10)
        tab.on_tick()
    lines = timeout_lines(tab, "got no report")
    assert len(lines) == 1 and HINT in lines[0]


def test_monitoring_late_report_clears_the_timeout_text(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    tab.request()
    clock.advance(30)
    tab.on_tick()
    assert "no report within" in tab.stale_text()
    tab.on_received(reply(report()))
    assert tab.stale_text() == "" and tab.report is not None
    clock.advance(100)
    tab.on_tick()
    assert tab.stale_text() == ""                      # the answered request does not time out again


def test_monitoring_report_inside_the_timeout_never_shows_it(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    tab.request()
    clock.advance(3)
    tab.on_received(reply(report()))
    clock.advance(100)
    tab.on_tick()
    assert tab.stale_text() == "" and not timeout_lines(tab, "got no report")


def test_monitoring_auto_refresh_keeps_running_through_a_timeout(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    tab.auto_check.invoke()                            # starts auto refresh and sends the first request
    assert tab.auto_active and len(tab._waiting) == 1
    clock.advance(6)
    tab.on_tick()
    assert "no report within" in tab.stale_text()
    assert tab.auto_active and tab._auto_id is not None  # still scheduled
    tab._auto_tick()                                   # the next tick: another request goes out
    assert len(tab._waiting) == 1
    clock.advance(6)
    tab.on_tick()
    assert len(timeout_lines(tab, "got no report")) == 2  # once per request, not once per tick
    tab.on_received(reply(report()))
    assert tab.stale_text() == "" and tab.auto_active


def test_monitoring_a_negative_reply_is_an_answer_not_a_timeout(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    tab.on_received(reply(report()))
    tab.request()
    tab.on_received(reply(NegativeResponse()))
    clock.advance(100)
    tab.on_tick()
    assert tab.stale_text() == "stale (last request failed)"


def test_monitoring_disconnect_does_not_leave_a_false_timeout_behind(gui_env, scripted, clock):
    server = scripted()
    tab = monitoring_tab(gui_env, server)
    tab.request()
    tab.client.disconnect()
    assert wait_until(gui_env.root, lambda: tab.state_label.cget("text") == "DISCONNECTED")
    clock.advance(100)
    tab.on_tick()
    assert tab.stale_text() == "" and not timeout_lines(tab, "got no report")


def test_monitoring_changing_the_timeout_applies_to_later_requests_only(gui_env, scripted, clock):
    server = scripted()
    store = gui_env.store()
    tab = monitoring_tab(gui_env, server, store)
    tab.request()
    store.set_reply_timeout(60)
    tab.request()
    clock.advance(6)
    tab.on_tick()
    assert tab.stale_text() == "stale (no report within 5 s)"
    assert len(timeout_lines(tab, "got no report")) == 1
    clock.advance(60)
    tab.on_tick()
    assert len(timeout_lines(tab, "got no report")) == 2 and "60 s" in timeout_lines(tab, "got no report")[1]


# ------------------------------------------------------------ Telemetry
def start_requested(tab, gui_env):
    from test_tab_telemetry import button
    button(tab, "Start").invoke()
    assert wait_until(gui_env.root, lambda: tab.stream_state == "requested")


def test_telemetry_no_frame_note_after_the_timeout_and_cleared_by_the_first_frame(gui_env, scripted, clock):
    server = scripted()
    tab = telemetry_tab(gui_env, server)
    start_requested(tab, gui_env)
    counters = dict(tab.counters)
    clock.advance(4.9)
    tab.on_tick()
    assert "no frame yet" not in tab.status_text()
    clock.advance(0.1)
    tab.on_tick()
    assert "no frame yet, 5 s since the request" in tab.status_text()
    clock.advance(3)
    tab.on_tick()
    assert "no frame yet, 8 s since the request" in tab.status_text()
    assert tab.stream_state == "requested" and tab.counters == counters   # informational: nothing else moved
    assert tab.error_count == 0 and str(tab.message.cget("fg")) != "" and tab.message_text() == ""
    lines = timeout_lines(tab, "got no frame")
    assert len(lines) == 1 and HINT in lines[0]
    tab.on_received(reply(frame_message(1)))
    assert "no frame yet" not in tab.status_text() and tab.stream_state == "streaming"
    clock.advance(500)
    tab.on_tick()
    assert "no frame yet" not in tab.status_text() and len(timeout_lines(tab, "got no frame")) == 1


def test_telemetry_no_silence_alarm_while_streaming(gui_env, scripted, clock):
    server = scripted()
    tab = telemetry_tab(gui_env, server)
    start_requested(tab, gui_env)
    tab.on_received(reply(frame_message(1)))
    assert tab.stream_state == "streaming"
    clock.advance(10000)
    tab.on_tick()
    assert "no frame yet" not in tab.status_text() and not timeout_lines(tab, "got no frame")
    assert tab.error_count == 0


def test_telemetry_a_refusal_is_not_followed_by_the_note(gui_env, scripted, clock):
    from groundstation.cortex import TelemetryNegativeAck
    server = scripted()
    tab = telemetry_tab(gui_env, server)
    start_requested(tab, gui_env)
    tab.on_received(reply(TelemetryNegativeAck(code=1, meaning="x")))
    assert tab.stream_state == "request refused"
    clock.advance(500)
    tab.on_tick()
    assert "no frame yet" not in tab.status_text() and not timeout_lines(tab, "got no frame")


def test_telemetry_link_loss_while_waiting_removes_the_note(gui_env, scripted, clock):
    server = scripted()
    tab = telemetry_tab(gui_env, server)
    start_requested(tab, gui_env)
    clock.advance(6)
    tab.on_tick()
    assert "no frame yet" in tab.status_text()
    tab.client.disconnect()
    assert wait_until(gui_env.root, lambda: tab.stream_state == "link lost")
    assert "no frame yet" not in tab.status_text()


def test_telemetry_changing_the_timeout_applies_to_the_next_request(gui_env, scripted, clock):
    from test_tab_telemetry import button
    server = scripted()
    store = gui_env.store()
    tab = telemetry_tab(gui_env, server, store)
    store.set_reply_timeout(30)
    start_requested(tab, gui_env)
    clock.advance(6)
    tab.on_tick()
    assert "no frame yet" not in tab.status_text()      # 30 s applies to this request
    clock.advance(25)
    tab.on_tick()
    assert "no frame yet" in tab.status_text()
    button(tab, "Stop").invoke()
    assert wait_until(gui_env.root, lambda: tab.stream_state == "stopped")
    assert "no frame yet" not in tab.status_text()


def test_telemetry_pump_runs_the_deadline_check(gui_env, scripted, clock):
    server = scripted()
    tab = telemetry_tab(gui_env, server)
    start_requested(tab, gui_env)
    clock.advance(10)
    assert wait_until(gui_env.root, lambda: "no frame yet" in tab.status_text(), timeout=3)
