"""Tests for groundstation.guibase (store, dialog, log, ServiceTab base) via MonitoringTab/ControlTab."""
import dataclasses
import logging
import re

import pytest

from groundstation.clients import ControlClient, MonitoringClient
from groundstation.guibase import (HINT, MAX_LOG_LINES, GREEN, RED, EventLog, OptionsStore,
                                   ProtocolOptionsDialog, _parse_flow_id, checkbutton_selected,
                                   parse_ascii_uint, parse_port, set_checkbutton, set_entry)
from groundstation.net import ConnState, Received
from groundstation.protocol import ProtocolOptions
from groundstation.tab_control import ControlTab
from groundstation.tab_monitoring import MonitoringTab, parse_interval
from gui_util import (gui_env, has_log_line, pending_after_count, settle,  # noqa: F401
                      wait_until)


# ------------------------------------------------------------ OptionsStore
def test_store_notifies_all_subscribers_and_get_returns_latest():
    store = OptionsStore()
    seen_a, seen_b = [], []
    store.subscribe(seen_a.append)
    store.subscribe(seen_b.append)
    new = ProtocolOptions(checksum_mode="payload_only")
    store.set(new)
    assert seen_a == [new] and seen_b == [new]
    assert store.get() is new
    newer = ProtocolOptions(control_flow_id=9)
    store.set(newer)
    assert store.get() is newer and seen_a == [new, newer]


def test_store_raising_subscriber_does_not_block_the_next_one(caplog):
    store = OptionsStore()
    seen = []

    def bad(opts):
        raise RuntimeError("subscriber failed on purpose")

    store.subscribe(bad)
    store.subscribe(seen.append)
    with caplog.at_level(logging.ERROR, logger="groundstation.guibase"):
        store.set(ProtocolOptions(fec_in_frame=True))
    assert len(seen) == 1 and seen[0].fec_in_frame is True
    assert any("options subscriber failed" in r.message for r in caplog.records)


def test_store_unsubscribe_works_and_is_idempotent():
    store = OptionsStore()
    seen = []
    unsubscribe = store.subscribe(seen.append)
    store.set(ProtocolOptions(control_flow_id=2))
    unsubscribe()
    unsubscribe()
    store.set(ProtocolOptions(control_flow_id=3))
    assert [o.control_flow_id for o in seen] == [2]


# ------------------------------------------------------------ dialog apply()
def test_dialog_apply_changes_each_field_individually(gui_env):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    base = store.get()

    set_checkbutton(dialog.msb_check, False)
    assert dialog.apply() is True
    assert store.get() == dataclasses.replace(base, cortex_byte0_is_msb=False)

    dialog.checksum_box.set("payload_only")
    assert dialog.apply() is True
    assert store.get().checksum_mode == "payload_only"

    set_checkbutton(dialog.fec_check, True)
    assert dialog.apply() is True
    assert store.get().fec_in_frame is True

    expected = {"control_flow_id": 11, "monitoring_flow_id": 0x12, "telecommand_flow_id": 33,
                "telemetry_flow_id": 0xFFFFFFFF}
    for field, value in expected.items():
        set_entry(dialog.flow_entries[field], hex(value) if value > 30 else str(value))
    assert dialog.apply() is True
    final = store.get()
    assert (final.control_flow_id, final.monitoring_flow_id, final.telecommand_flow_id,
            final.telemetry_flow_id) == (11, 0x12, 33, 0xFFFFFFFF)
    assert final.cortex_byte0_is_msb is False and final.checksum_mode == "payload_only"
    assert dialog.message.cget("text") == "Applied."
    assert str(dialog.message.cget("fg")) == GREEN


def test_dialog_flow_id_change_reaches_client_opts_of_a_tab(gui_env):
    store = gui_env.store()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, store=store)
    dialog = gui_env.dialog(store)
    set_entry(dialog.flow_entries["monitoring_flow_id"], "5")
    assert dialog.apply() is True
    assert tab.client.opts.monitoring_flow_id == 5
    assert tab.client.opts is store.get()


@pytest.mark.parametrize("bad", ["", "abc", "-1", "4294967296"])
def test_dialog_invalid_flow_id_changes_nothing_and_shows_a_message(gui_env, bad):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    before = store.get()
    set_entry(dialog.flow_entries["telecommand_flow_id"], bad)
    set_checkbutton(dialog.fec_check, True)  # a valid change must not leak through either
    assert dialog.apply() is False
    assert store.get() is before
    assert "Telecommand flow ID" in dialog.message.cget("text")
    assert "0xFFFFFFFF" in dialog.message.cget("text")
    assert str(dialog.message.cget("fg")) == RED


def test_dialog_has_no_modal_grab_and_shows_the_assumption_note(gui_env):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    assert dialog.grab_current() is None
    texts = []

    def walk(widget):
        try:
            texts.append(str(widget.cget("text")))
        except Exception:  # noqa: BLE001 - not every widget has text
            pass
        for child in widget.winfo_children():
            walk(child)

    walk(dialog)
    assert ("Assumptions pending confirmation against the real server: checksum coverage, "
            "byte numbering, flow IDs.") in texts
    assert str(dialog.checksum_box.cget("state")) == "readonly"
    assert tuple(dialog.checksum_box.cget("values")) == (
        "whole_packet", "excluding_postamble", "payload_only")


# ------------------------------------------------------------ one dialog per store
def test_second_click_on_protocol_options_does_not_create_a_second_dialog(gui_env, tk_root):
    store = gui_env.store()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, store=store)
    other = gui_env.make_tab(ControlTab, ControlClient, store=store)

    def dialogs():
        return [w for w in tk_root.winfo_children() if isinstance(w, ProtocolOptionsDialog)]

    tab.options_button.invoke()
    first = dialogs()
    assert len(first) == 1 and store.dialog is first[0]
    tab.options_button.invoke()
    other.options_button.invoke()  # a second tab on the same store raises the same dialog
    assert dialogs() == first
    first[0].destroy()
    assert store.dialog is None
    tab.options_button.invoke()
    assert len(dialogs()) == 1 and dialogs() != first


# ------------------------------------------------------------ connection bar
@pytest.mark.parametrize("bad_port", ["", "0", "65536", "abc"])
def test_bad_port_never_calls_configure_or_connect(gui_env, monkeypatch, bad_port):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    calls = []
    monkeypatch.setattr(tab.client, "configure", lambda *a: calls.append(("configure", a)))
    monkeypatch.setattr(tab.client, "connect", lambda: calls.append(("connect",)))
    set_entry(tab.port_entry, bad_port)
    tab.connect_button.invoke()
    assert calls == []
    assert tab.client.state == ConnState.DISCONNECTED
    assert tab.client.events.qsize() == 0
    assert tab.state_label.cget("text") == "Port must be an integer from 1 to 65535"
    assert str(tab.state_label.cget("fg")) == RED


def test_empty_host_never_connects(gui_env, monkeypatch):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    calls = []
    monkeypatch.setattr(tab.client, "connect", lambda: calls.append(1))
    set_entry(tab.host_entry, "   ")
    tab.connect_button.invoke()
    assert calls == [] and tab.state_label.cget("text") == "Host must not be empty"


def test_good_port_connects_and_disconnect_returns(gui_env):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock)
    assert tab.state_label.cget("text") == "DISCONNECTED"
    assert tab.connect_button.cget("text") == "Connect"
    tab.connect_button.invoke()
    assert wait_until(tab.winfo_toplevel(), lambda: tab.state_label.cget("text") == "CONNECTED")
    assert tab.connect_button.cget("text") == "Disconnect"
    assert tab.client.state == ConnState.CONNECTED
    tab.connect_button.invoke()
    assert wait_until(tab.winfo_toplevel(), lambda: tab.state_label.cget("text") == "DISCONNECTED")
    assert tab.connect_button.cget("text") == "Connect"


def test_connect_failure_shows_error_state(gui_env):
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, port=port,
                           client_kwargs={"connect_timeout": 1.0})
    tab.connect_button.invoke()
    assert wait_until(tab.winfo_toplevel(), lambda: tab.state_label.cget("text") == "ERROR", timeout=5)
    assert tab.connect_button.cget("text") == "Connect"
    assert has_log_line(tab, "ERROR:") and has_log_line(tab, HINT)


def test_options_change_reaches_client_opts(gui_env):
    store = gui_env.store()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, store=store)
    new = ProtocolOptions(checksum_mode="excluding_postamble")
    store.set(new)
    assert tab.client.opts is new


# ------------------------------------------------------------ log
def test_log_is_bounded_and_keeps_the_newest_line(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    tab.log.clear()
    for i in range(1500):
        tab.log.add("info", f"line {i}")
    assert tab.log.line_count() == MAX_LOG_LINES == 1000
    lines = tab.log.lines()
    assert lines[-1].endswith("line 1499")
    assert lines[0].endswith("line 500")
    assert not any(line.endswith(" line 0") or line.endswith(" line 499") for line in lines)


def test_raw_toggle_adds_one_line_per_word(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    log = tab.log
    log.clear()
    raw = bytes(range(28))  # 7 words
    log.add("sent", "x", raw=raw)
    assert log.line_count() == 1
    log.raw_check.invoke()
    assert log.raw_enabled() is True
    log.add("sent", "x", raw=raw)
    assert log.line_count() == 1 + 1 + 7
    assert log.lines()[-1].startswith("    [ 6] 0x")
    log.raw_check.invoke()
    assert log.raw_enabled() is False
    log.add("sent", "x", raw=raw)
    assert log.line_count() == 1 + 1 + 7 + 1
    log.add("info", "no raw for info", raw=None)
    log.clear()
    assert log.line_count() == 0


def test_log_rejects_unknown_kind(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    with pytest.raises(ValueError):
        tab.log.add("bogus", "x")


def test_real_sent_and_received_lines_with_raw_dump(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    tab.log.clear()
    tab.log.raw_check.invoke()
    tab.request()
    assert wait_until(tk_root, lambda: tab.received_count == 1)
    lines = tab.log.lines()
    sent = next(i for i, l in enumerate(lines) if "SENT monitoring request (28 bytes)" in l)
    assert lines[sent + 1].startswith("    [ 0] 0x499602D2")
    received = next(i for i, l in enumerate(lines) if "RECEIVED MonitoringReport(" in l)
    assert lines[received + 1].startswith("    [ 0] 0x499602D2")


# ------------------------------------------------------------ pump survives a dispatch exception
def test_pump_survives_a_dispatch_exception(gui_env, tk_root, monkeypatch):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    tab.after_cancel(tab._after_id)  # stop the timer so both responses land in ONE drain batch
    tab._after_id = None
    calls = []
    original = tab.on_received

    def flaky(event):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        original(event)

    monkeypatch.setattr(tab, "on_received", flaky)
    tab.log.clear()
    first, second = tab.client.request(), tab.client.request()
    assert wait_until(tk_root, lambda: sum(isinstance(e, Received)
                                           for e in list(tab.client.events.queue)) >= 2)
    tab._pump()  # one manual pump processes both Received events
    assert len(calls) == 2
    assert tab.received_count == 2
    assert tab.report is not None and tab.report.tag == second != first
    assert tab.error_count == 1
    assert has_log_line(tab, "internal error while handling Received: RuntimeError: boom")


# ------------------------------------------------------------ destroy
def test_destroy_cancels_after_callbacks_unsubscribes_and_stops_the_thread(gui_env, tk_root):
    errors = []
    previous = tk_root.report_callback_exception
    tk_root.report_callback_exception = lambda *exc: errors.append(exc)
    try:
        mock = gui_env.mock("monitoring")
        store = gui_env.store()
        baseline = pending_after_count(tk_root)
        tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, store=store, connect=True)
        assert pending_after_count(tk_root) == baseline + 1  # exactly the pump
        client = tab.client
        original_opts = client.opts
        tab.shutdown()
        assert client.state == ConnState.DISCONNECTED
        assert client._thread is None or not client._thread.is_alive()
        tab.destroy()
        tk_root.update_idletasks()
        assert pending_after_count(tk_root) == baseline
        settle(tk_root, 0.3)  # a leftover callback would run now and hit a dead widget
        store.set(ProtocolOptions(control_flow_id=77))
        assert client.opts is original_opts  # unsubscribed
        assert errors == []
    finally:
        tk_root.report_callback_exception = previous


def test_destroy_cancels_auto_refresh_timer_too(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    baseline = pending_after_count(tk_root)
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    tab.auto_check.invoke()
    assert pending_after_count(tk_root) == baseline + 2  # pump and auto tick
    tab.destroy()
    assert pending_after_count(tk_root) == baseline


# ------------------------------------------------------------ statistics line
def test_statistics_line_counts_exactly(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    store = gui_env.store()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, store=store, connect=True)
    assert tab.stats_text() == "Messages sent: 0 | received: 0 | errors: 0 | dropped events: 0"
    tab.request()
    tab.request()
    assert wait_until(tk_root, lambda: tab.received_count == 2)
    assert tab.stats_text() == "Messages sent: 2 | received: 2 | errors: 0 | dropped events: 0"
    store.set(dataclasses.replace(store.get(), monitoring_flow_id=5))  # server will answer negatively
    tab.request()
    assert wait_until(tk_root, lambda: tab.received_count == 3)
    assert tab.stats_text() == "Messages sent: 3 | received: 3 | errors: 1 | dropped events: 0"
    assert has_log_line(tab, "Server answered with NegativeResponse.") and has_log_line(tab, HINT)


def test_statistics_line_shows_dropped_events(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock,
                           client_kwargs={"max_queued_events": 2})
    tab.after_cancel(tab._after_id)  # keep the queue full: no draining
    tab._after_id = None
    tab.toggle_connection()
    assert wait_until(tk_root, lambda: tab.client.state == ConnState.CONNECTED)
    for _ in range(5):
        tab.client.request()
    # 2 state events fill the queue, so 5 Sent + 5 Received events are all dropped.
    assert wait_until(tk_root, lambda: tab.client.dropped_events == 10)
    tab._pump()
    assert tab.stats_text() == "Messages sent: 0 | received: 0 | errors: 0 | dropped events: 10"


# ------------------------------------------------------------ strict number parsing
def _chars(*codes):
    return "".join(chr(c) for c in codes)


ARABIC_INDIC_3070 = _chars(0x663, 0x660, 0x667, 0x660)   # looks like 3070, is not ASCII
FULL_WIDTH_80 = _chars(0xFF18, 0xFF10)                      # full-width "80"
STRICT_REJECTS = ["8_0", "+80", ARABIC_INDIC_3070, "1_0", "0x_1F", "0x1_F", "0x", " ", "1e3",
                  FULL_WIDTH_80, "", "-1", "8 0", "0b11", "80.0", "0x1F_", _chars(0x663),
                  _chars(0xB2), "8" + _chars(0x660)]


@pytest.mark.parametrize("text", STRICT_REJECTS)
def test_helper_rejects_non_strict_numbers(text):
    with pytest.raises(ValueError):
        parse_ascii_uint(text, hex_ok=True)
    with pytest.raises(ValueError):
        parse_ascii_uint(text)


@pytest.mark.parametrize("text, hex_ok, value", [
    ("80", False, 80), (" 80 ", False, 80), ("0", False, 0), ("007", False, 7),
    ("0x1F", True, 31), ("0X1f", True, 31), ("0x0", True, 0), ("0xFFFFFFFF", True, 0xFFFFFFFF),
    ("80", True, 80), ("  0x10  ", True, 16)])
def test_helper_accepts_strict_numbers(text, hex_ok, value):
    assert parse_ascii_uint(text, hex_ok=hex_ok) == value


def test_helper_hex_needs_hex_ok():
    with pytest.raises(ValueError):
        parse_ascii_uint("0x1F")
    assert parse_ascii_uint("0x1F", hex_ok=True) == 31


@pytest.mark.parametrize("text", STRICT_REJECTS + ["0x1F", "0X1f", "0", "65536", "99999"])
def test_parse_port_rejects(text):
    with pytest.raises(ValueError, match="Port must be an integer from 1 to 65535"):
        parse_port(text)


@pytest.mark.parametrize("text, value", [("80", 80), (" 80 ", 80), ("1", 1), ("65535", 65535)])
def test_parse_port_accepts(text, value):
    assert parse_port(text) == value


@pytest.mark.parametrize("text", STRICT_REJECTS + ["4294967296", "-1", "0x100000000"])
def test_flow_id_rejects(text):
    with pytest.raises(ValueError, match="must be an integer from 0 to 0xFFFFFFFF"):
        _parse_flow_id(text)


@pytest.mark.parametrize("text, value", [
    ("80", 80), (" 80 ", 80), ("0x1F", 31), ("0X1f", 31), ("0", 0), ("4294967295", 0xFFFFFFFF)])
def test_flow_id_accepts(text, value):
    assert _parse_flow_id(text) == value


@pytest.mark.parametrize("text", STRICT_REJECTS + ["0x1F", "0", "61", "-3", "1.5"])
def test_interval_rejects(text):
    with pytest.raises(ValueError, match="whole number of seconds from 1 to 60"):
        parse_interval(text)


@pytest.mark.parametrize("text, value", [("5", 5), (" 5 ", 5), ("1", 1), ("60", 60)])
def test_interval_accepts(text, value):
    assert parse_interval(text) == value


@pytest.mark.parametrize("bad", ["8_0", "+80", ARABIC_INDIC_3070, "0x_1F"])
def test_dialog_and_port_entry_use_the_strict_parser(gui_env, bad):
    store = gui_env.store()
    dialog = gui_env.dialog(store)
    before = store.get()
    set_entry(dialog.flow_entries["control_flow_id"], bad)
    assert dialog.apply() is False and store.get() is before
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    set_entry(tab.port_entry, bad)
    tab.connect_button.invoke()
    assert tab.state_label.cget("text") == "Port must be an integer from 1 to 65535"
    assert tab.client.state == ConnState.DISCONNECTED
