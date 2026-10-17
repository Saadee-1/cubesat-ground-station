"""Tests for groundstation.tab_monitoring (report display, auto refresh, stale marking)."""
import dataclasses
import re
import types

import pytest

from groundstation.clients import MonitoringClient
from groundstation.cop1 import MonitoringReport, encode_ip
from groundstation.guibase import GREEN, RED, checkbutton_selected, set_entry
from groundstation.protocol import to_i32
from groundstation.tab_monitoring import STALE_TEXT, MonitoringTab, parse_interval
from gui_util import gui_env, has_log_line, settle, wait_until  # noqa: F401

IP_WORD = encode_ip("192.52.109.15")


def rows_by_name(tab):
    return {row[0]: row for row in tab.config_rows()}


# ------------------------------------------------------------ report sections
def test_request_fills_every_section_with_the_report_values(gui_env, tk_root):
    shared = {0: 1023, 3: 0x00010BFE, 4: IP_WORD, 11: 7}
    mock = gui_env.mock("monitoring", shared_params=shared)
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    assert tab.header_text() == "No report yet"
    tab.request_button.invoke()
    assert wait_until(tk_root, lambda: tab.report is not None)
    rows = rows_by_name(tab)
    assert len(tab.config_rows()) == 29
    assert rows["Spacecraft identifier"] == ("Spacecraft identifier", "1023", "")
    assert rows["CORTEX_CRT port number/address"] == (
        "CORTEX_CRT port number/address", str(0x00010BFE), "TMU 1, port 3070")
    assert rows["CORTEX_CRT IP address"] == (
        "CORTEX_CRT IP address", str(IP_WORD), "192.52.109.15")
    assert rows["Check operational parameters"][1] == "7"
    assert rows["Codeblock length"][1] == "0"
    # Only the two documented ids carry a decoded value; every other cell stays empty.
    assert [r[0] for r in tab.config_rows() if r[2]] == [
        "CORTEX_CRT port number/address", "CORTEX_CRT IP address"]
    assert tab.status_rows() == [
        ("Telecommand global status", "0"), ("Number of programming perturbation", "0"),
        ("Channel auto switch occurs", "0"), ("COP1 version and compatibility options", "0")]
    assert tab.flags_text() == "no error flags" and str(tab.flags_label.cget("fg")) == GREEN
    header = tab.header_text()
    assert "COP-1 version: revision 0, issue 0, options 0" in header
    assert "TM port: TMU 1, port 3070" in header
    assert "report tag 1" in header
    assert re.search(r"received at \d\d:\d\d:\d\d$", header)
    assert tab.stale_text() == ""


def test_error_flags_and_version_come_from_the_report(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    config = [0] * 40
    config[3] = 0x00020001
    status = [0] * 20
    status[0], status[7] = 0x30, 0x01020400
    report = MonitoringReport(tag=77, config=tuple(config), status=tuple(status))
    tab.on_received(types.SimpleNamespace(parsed=report))
    assert tab.flags_text() == "Error flags: FOP, TIME"
    assert str(tab.flags_label.cget("fg")) == RED
    header = tab.header_text()
    assert "revision 1, issue 2, options 4" in header
    assert "TM port: TMU 2, port 1" in header and "report tag 77" in header
    assert tab.status_rows()[0] == ("Telecommand global status", "48")
    assert tab.status_rows()[3] == ("COP1 version and compatibility options", str(0x01020400))


# ------------------------------------------------------------ auto refresh
def test_auto_refresh_repeats_stops_when_unchecked(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    set_entry(tab.interval_spin, "1")
    tab.auto_check.invoke()
    assert checkbutton_selected(tab.auto_check) and tab.auto_active
    assert wait_until(tk_root, lambda: tab.received_count >= 3, timeout=6.0)
    tab.auto_check.invoke()  # switch off
    assert not checkbutton_selected(tab.auto_check) and not tab.auto_active
    settle(tk_root, 0.3)  # let an in-flight response arrive
    count = len(mock.received)
    assert count >= 3
    settle(tk_root, 2.5)  # 2.5 intervals
    assert len(mock.received) == count


def test_auto_refresh_does_nothing_while_disconnected_then_starts_on_connect(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock)
    set_entry(tab.interval_spin, "1")
    tab.auto_check.invoke()
    settle(tk_root, 2.5)
    assert tab.sent_count == 0 and mock.received == []
    assert tab.client.events.qsize() == 0
    assert tab.auto_active  # still armed: it only skips the ticks
    tab.toggle_connection()
    assert wait_until(tk_root, lambda: tab.received_count >= 1, timeout=4.0)


@pytest.mark.parametrize("bad", ["0", "61", "x", "", "-3", "1.5"])
def test_invalid_intervals_are_rejected(gui_env, tk_root, bad):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    set_entry(tab.interval_spin, bad)
    tab.auto_check.invoke()
    assert not checkbutton_selected(tab.auto_check)
    assert not tab.auto_active
    assert "whole number of seconds from 1 to 60" in tab.message_text()
    settle(tk_root, 0.2)
    assert mock.received == []


@pytest.mark.parametrize("text, value", [("1", 1), ("60", 60), (" 7 ", 7)])
def test_valid_intervals_parse(text, value):
    assert parse_interval(text) == value


def test_changing_the_interval_to_an_invalid_value_stops_the_timer(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    set_entry(tab.interval_spin, "1")
    tab.auto_check.invoke()
    assert wait_until(tk_root, lambda: tab.received_count >= 1)
    set_entry(tab.interval_spin, "abc")
    assert wait_until(tk_root, lambda: not tab.auto_active, timeout=3.0)
    assert not checkbutton_selected(tab.auto_check)
    assert "whole number" in tab.message_text()


# ------------------------------------------------------------ only the latest report
def test_only_the_latest_report_is_shown(gui_env, tk_root):
    shared = {0: 111}
    mock = gui_env.mock("monitoring", shared_params=shared)
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    tab.request()
    assert wait_until(tk_root, lambda: tab.report is not None and tab.report.tag == 1)
    assert rows_by_name(tab)["Spacecraft identifier"][1] == "111"
    shared[0] = 222
    tab.request()
    assert wait_until(tk_root, lambda: tab.report.tag == 2)
    assert rows_by_name(tab)["Spacecraft identifier"][1] == "222"
    assert len(tab.config_rows()) == 29 and len(tab.status_rows()) == 4  # nothing accumulates
    assert "report tag 2" in tab.header_text() and "report tag 1" not in tab.header_text()


# ------------------------------------------------------------ stale marking
def test_failed_request_marks_the_old_report_stale_then_recovers(gui_env, tk_root):
    shared = {0: 555}
    mock = gui_env.mock("monitoring", shared_params=shared)
    store = gui_env.store()
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, store=store, connect=True)
    tab.request()
    assert wait_until(tk_root, lambda: tab.report is not None)
    assert tab.stale_text() == ""
    good = store.get()
    store.set(dataclasses.replace(good, monitoring_flow_id=5))  # the server will answer negatively
    tab.request()
    assert wait_until(tk_root, lambda: tab.stale_text() == STALE_TEXT)
    assert STALE_TEXT == "stale (last request failed)"
    assert rows_by_name(tab)["Spacecraft identifier"][1] == "555"  # the old report stays visible
    assert has_log_line(tab, "Server answered with NegativeResponse.")
    store.set(good)
    tab.request()
    assert wait_until(tk_root, lambda: tab.stale_text() == "")


def test_request_while_disconnected_marks_the_old_report_stale(gui_env, tk_root):
    mock = gui_env.mock("monitoring")
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient, mock=mock, connect=True)
    tab.request()
    assert wait_until(tk_root, lambda: tab.report is not None)
    tab.client.disconnect()
    assert wait_until(tk_root, lambda: tab.state_label.cget("text") == "DISCONNECTED")
    tab.request_button.invoke()
    assert "Request failed" in tab.message_text() and "not connected" in tab.message_text()
    assert tab.stale_text() == STALE_TEXT


def test_stale_is_not_shown_before_any_report(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    tab.request_button.invoke()  # not connected
    assert "Request failed" in tab.message_text()
    assert tab.stale_text() == "" and tab.report is None


# ------------------------------------------------------------ exact unsigned texts
def test_words_above_2_to_the_31_are_shown_unsigned_with_exact_texts(gui_env):
    tab = gui_env.make_tab(MonitoringTab, MonitoringClient)
    # The protocol layer hands over SIGNED words, so build the report the same way.
    config, status = [0] * 40, [0] * 20
    config[0], config[3], config[4] = (to_i32(0xFFFFFFFF), to_i32(0x00010BFE), to_i32(0xC0346D0F))
    status[0], status[7] = to_i32(0x80000001), to_i32(0x01020300)
    assert config[0] < 0 and config[4] < 0 and status[0] < 0  # the point of this test
    report = MonitoringReport(tag=5, config=tuple(config), status=tuple(status))
    tab._show_report(report)
    rows = rows_by_name(tab)
    assert rows["Spacecraft identifier"] == ("Spacecraft identifier", "4294967295", "")
    assert rows["CORTEX_CRT port number/address"] == (
        "CORTEX_CRT port number/address", "68606", "TMU 1, port 3070")
    assert rows["CORTEX_CRT IP address"] == ("CORTEX_CRT IP address", "3224661263", "192.52.109.15")
    assert tab.status_rows() == [
        ("Telecommand global status", "2147483649"),
        ("Number of programming perturbation", "0"),
        ("Channel auto switch occurs", "0"),
        ("COP1 version and compatibility options", "16909056")]  # 16777216 + 131072 + 768
    header = tab.header_text()
    assert "revision 1, issue 2, options 3" in header
    assert "TM port: TMU 1, port 3070" in header and "report tag 5" in header
    assert tab.flags_text() == "Error flags: SOCKET"  # only bit 0x01 of 0x80000001 has a name
