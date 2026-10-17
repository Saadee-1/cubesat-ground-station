"""Tests for groundstation.tab_control (value parsing, staging, sending, tag bookkeeping)."""
import dataclasses
import struct
import time

import pytest

from groundstation.clients import ControlClient
from groundstation.cop1 import parse_control_response
from groundstation.net import ConnState, Received, StateChanged
from groundstation.protocol import parse_message
from groundstation.guibase import HINT, RED, GREEN, set_entry
from groundstation.protocol import build_message
from groundstation.tab_control import (MAX_TRACKED_TAGS, ControlTab, parse_control_value,
                                       parse_ip, parse_tmu_port)
from gui_util import gui_env, has_log_line, wait_until  # noqa: F401


# ------------------------------------------------------------ value parsing
@pytest.mark.parametrize("text, word", [
    ("10", 10), ("0x1F", 31), ("0X1f", 31), ("192.52.109.15", 0xC0346D0F), ("1:3070", 0x00010BFE),
    ("  7  ", 7), ("0", 0), ("4294967295", 0xFFFFFFFF), ("0xFFFFFFFF", 0xFFFFFFFF),
    ("0:0", 0), ("65535:65535", 0xFFFFFFFF), ("0.0.0.0", 0), ("255.255.255.255", 0xFFFFFFFF),
])
def test_parse_accepts(text, word):
    assert parse_control_value(text) == word


@pytest.mark.parametrize("text, fragment", [
    ("", "empty"),
    ("-5", "Negative values are not accepted"),
    ("-0", "Negative values are not accepted"),
    ("0x100000000", "out of range"),
    ("4294967296", "out of range"),
    ("1.2.3", "IPv4"),
    ("300.1.1.1", "IPv4"),
    ("1:70000", "out of range"),
    ("70000:1", "out of range"),
    ("x", "Cannot read"),
    ("0xZZ", "hex"),
    ("1:2:3", "TMU:port"),
    (":5", "TMU:port"),
])
def test_parse_rejects_with_a_reason(text, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_control_value(text)


def test_parse_helpers_directly():
    assert parse_ip("192.52.109.15") == 0xC0346D0F
    assert parse_tmu_port("1:3070") == 0x00010BFE
    with pytest.raises(ValueError):
        parse_ip("1.2.3")
    with pytest.raises(ValueError):
        parse_tmu_port("1-3070")


@pytest.mark.parametrize("bad, fragment", [
    ("", "empty"), ("-5", "Negative"), ("0x100000000", "out of range"), ("1.2.3", "IPv4"),
    ("300.1.1.1", "IPv4"), ("1:70000", "out of range"), ("x", "Cannot read")])
def test_ui_rejection_leaves_staged_state_unchanged_and_shows_a_message(gui_env, bad, fragment):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    tab.select(7)
    set_entry(tab.value_entry, "5")
    tab.stage_button.invoke()
    assert tab.staged == {7: 5}
    tab.select(3)
    set_entry(tab.value_entry, bad)
    tab.stage_button.invoke()
    assert tab.staged == {7: 5}
    assert tab.cell(3, "staged") == ""
    assert fragment in tab.message_text()
    assert str(tab.edit_message.cget("fg")) == RED


def test_ui_accepts_each_syntax_and_shows_the_word(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    for pid, text, word in [(0, "1023", 1023), (4, "192.52.109.15", 0xC0346D0F),
                            (3, "1:3070", 0x00010BFE), (6, "0x1F", 31)]:
        tab.select(pid)
        set_entry(tab.value_entry, text)
        tab.stage_button.invoke()
        assert tab.staged[pid] == word
        assert tab.cell(pid, "staged") == f"{word} (0x{word:08X})"
    assert tab.cell(4, "name") == "CORTEX_CRT IP address"
    assert tab.cell(39, "name") == "param 39"  # no name defined for ids above 28
    assert str(tab.edit_message.cget("fg")) == GREEN


def test_stage_without_selection_is_an_inline_error(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    set_entry(tab.value_entry, "5")
    tab.stage_button.invoke()
    assert tab.staged == {} and tab.message_text() == "Select a parameter first"


def test_conversion_helper_buttons_fill_the_entry(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    set_entry(tab.value_entry, "192.52.109.15")
    tab.ip_button.invoke()
    assert tab.value_entry.get() == "0xC0346D0F"
    set_entry(tab.value_entry, "1:3070")
    tab.tmu_button.invoke()
    assert tab.value_entry.get() == "0x00010BFE"
    set_entry(tab.value_entry, "1.2.3")
    tab.ip_button.invoke()
    assert tab.value_entry.get() == "1.2.3" and "IPv4" in tab.message_text()
    set_entry(tab.value_entry, "nope")
    tab.tmu_button.invoke()
    assert tab.value_entry.get() == "nope" and "TMU:port" in tab.message_text()
    assert tab.staged == {}  # the helpers never stage anything


# ------------------------------------------------------------ stage, unstage, clear, send
def test_stage_unstage_clear_all(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    for pid in (8, 9, 10):
        tab.select(pid)
        set_entry(tab.value_entry, str(pid * 100))
        tab.stage_button.invoke()
    tab.select(9)
    tab.unstage_button.invoke()
    assert set(tab.staged) == {8, 10} and tab.cell(9, "staged") == ""
    tab.unstage_button.invoke()  # already unstaged
    assert "not staged" in tab.message_text()
    tab.clear_button.invoke()
    assert tab.staged == {}
    assert [tab.cell(p, "staged") for p in (8, 9, 10)] == ["", "", ""]


def test_send_staged_sends_exactly_the_staged_ids_and_marks_pending_then_ack(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    for pid, text in [(3, "1023"), (4, "192.52.109.15"), (5, "1:3070"), (6, "0x1F")]:
        tab.select(pid)
        set_entry(tab.value_entry, text)
        tab.stage_button.invoke()
    tab.select(6)
    tab.unstage_button.invoke()
    tab.send_button.invoke()
    # Immediately after the click: pending, and the staged cells are cleared.
    assert [tab.cell(p, "sent") for p in (3, 4, 5)] == ["pending"] * 3
    assert tab.staged == {} and tab.cell(3, "staged") == ""
    assert tab.cell(6, "sent") == "" and tab.cell(7, "sent") == ""
    assert wait_until(tk_root, lambda: tab.cell(3, "sent") == "ack 0")
    assert [tab.cell(p, "sent") for p in (3, 4, 5)] == ["ack 0"] * 3
    # The server received exactly the staged ids with the exact words.
    assert mock.params == {3: 1023, 4: 0xC0346D0F, 5: 0x00010BFE}
    body = mock.received[0].body
    assert len(mock.received) == 1 and body[:2] == (1, 0) and body[3] == 3 and len(body) == 4 + 2 * 3
    assert has_log_line(tab, "acknowledged, status 0, ids [3, 4, 5]")
    assert tab.pending_tags == {}


def test_second_send_only_contains_newly_staged_ids(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    tab.select(1)
    set_entry(tab.value_entry, "11")
    tab.stage_button.invoke()
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.cell(1, "sent") == "ack 0")
    tab.select(2)
    set_entry(tab.value_entry, "22")
    tab.stage_button.invoke()
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.cell(2, "sent") == "ack 0")
    assert [m.body[4::2] for m in mock.received] == [(1,), (2,)]
    assert mock.params == {1: 11, 2: 22}


def test_send_with_nothing_staged_is_an_inline_message(gui_env):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    tab.send_button.invoke()
    assert tab.message_text() == "Nothing staged" and mock.received == []


# ------------------------------------------------------------ disconnected
def test_send_while_disconnected_keeps_the_staged_values(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    tab.select(2)
    set_entry(tab.value_entry, "99")
    tab.stage_button.invoke()
    tab.send_button.invoke()
    assert "Send failed" in tab.message_text() and "not connected" in tab.message_text()
    assert str(tab.edit_message.cget("fg")) == RED
    assert tab.staged == {2: 99}
    assert tab.cell(2, "staged") == "99 (0x00000063)" and tab.cell(2, "sent") == ""
    assert tab.pending_tags == {}


# ------------------------------------------------------------ tag bookkeeping
def test_unknown_ack_tag_is_logged(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    tag = tab.client.set_parameters({1: 1})  # bypasses the tab, so the tab does not know this tag
    assert wait_until(tk_root, lambda: has_log_line(tab, f"tag {tag} not sent by this tab"))
    assert tab.sent == {}


def test_the_201st_tag_evicts_the_oldest(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    tab.after_cancel(tab._after_id)  # no pump: acknowledgements must not remove tags here
    tab._after_id = None
    for i in range(MAX_TRACKED_TAGS + 1):
        tab.select(i % 40)
        set_entry(tab.value_entry, str(i))
        tab.stage_button.invoke()
        tab.send_button.invoke()
    assert len(tab.pending_tags) == MAX_TRACKED_TAGS == 200
    assert 1 not in tab.pending_tags          # the oldest tag was evicted
    assert 2 in tab.pending_tags and 201 in tab.pending_tags
    assert list(tab.pending_tags) == list(range(2, 202))


# ------------------------------------------------------------ negative response
def test_negative_response_logs_the_hint(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    # Parameter id 40 cannot be built with the protocol layer, so send raw words.
    tab.client.send(build_message(1, [1, 0, 9, 1, 40, 5]), "invalid parameter id", 9)
    assert wait_until(tk_root, lambda: has_log_line(tab, "Server answered with NegativeResponse."))
    error_line = next(l for l in tab.log.lines() if "Server answered with NegativeResponse." in l)
    assert HINT in error_line
    assert tab.error_count == 1 and mock.params == {}
    assert tab.sent == {}


def test_unknown_response_logs_the_hint_too(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    # Control request with a body the mock cannot read: it answers negatively as well.
    tab.client.send(build_message(1, [7, 7, 7]), "garbage body", 1)
    assert wait_until(tk_root, lambda: tab.error_count >= 1)
    assert has_log_line(tab, HINT)


# ------------------------------------------------------------ outstanding requests
REJECT_NOTE = "the negative reply has no tag, so the oldest outstanding request was marked rejected"


def server_event(body):
    """A server reply as the client would queue it, built with struct only."""
    words = [1234567890, 4 * (len(body) + 4), 1, *body, -1234567890]
    message = parse_message(struct.pack(f">{len(words)}I", *[w & 0xFFFFFFFF for w in words]))
    return Received(message, parse_control_response(message), time.monotonic())


def stage_and_send(tab, pid, value):
    """Stage one value and send it; returns the tag of that request."""
    tab.select(pid)
    set_entry(tab.value_entry, str(value))
    tab.stage_button.invoke()
    tab.send_button.invoke()
    return next(reversed(tab.pending_tags))


def hold_the_pump(tab, tk_root, expected_replies):
    """Stop the timer, wait for the server's replies to be queued, then throw them away.

    Leaves the requests outstanding so a test can feed the tab replies of its own choosing.
    """
    queue_events = lambda: sum(isinstance(e, Received) for e in list(tab.client.events.queue))
    assert wait_until(tk_root, lambda: queue_events() >= expected_replies)
    tab.client.drain()


def stopped_tab(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    tab.after_cancel(tab._after_id)  # no automatic pump: the test calls tab._pump() itself
    tab._after_id = None
    return tab


def test_flow_id_mismatch_ends_with_rejected_cells(gui_env, tk_root):
    store = gui_env.store()
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, store=store, connect=True)
    store.set(dataclasses.replace(store.get(), control_flow_id=9))  # the mock expects flow 1
    for pid, text in ((5, "1"), (6, "2")):
        tab.select(pid)
        set_entry(tab.value_entry, text)
        tab.stage_button.invoke()
    tab.send_button.invoke()
    assert tab.cell(5, "sent") == "pending"
    assert wait_until(tk_root, lambda: tab.cell(5, "sent") == "rejected (-1)")
    assert tab.cell(6, "sent") == "rejected (-1)"
    assert tab.pending_tags == {} and mock.params == {}
    assert has_log_line(tab, REJECT_NOTE)
    assert has_log_line(tab, HINT)  # it is still logged as an error with the hint


def test_one_negative_reply_marks_only_the_oldest_outstanding_request(gui_env, tk_root):
    tab = stopped_tab(gui_env, tk_root)
    first = stage_and_send(tab, 1, 11)
    second = stage_and_send(tab, 2, 22)
    hold_the_pump(tab, tk_root, 2)
    assert list(tab.pending_tags) == [first, second]
    tab.client.events.put(server_event([-1]))
    tab._pump()
    assert tab.cell(1, "sent") == "rejected (-1)"
    assert tab.cell(2, "sent") == "pending"
    assert list(tab.pending_tags) == [second]
    assert has_log_line(tab, REJECT_NOTE)


def test_normal_ack_after_a_rejected_request_still_works(gui_env, tk_root):
    tab = stopped_tab(gui_env, tk_root)
    stage_and_send(tab, 1, 11)
    second = stage_and_send(tab, 2, 22)
    hold_the_pump(tab, tk_root, 2)
    tab.client.events.put(server_event([-1]))
    tab.client.events.put(server_event([1, 0, second, 0]))
    tab._pump()
    assert tab.cell(1, "sent") == "rejected (-1)"
    assert tab.cell(2, "sent") == "ack 0"
    assert tab.pending_tags == {}


def test_real_server_rejects_then_acks_in_order(gui_env, tk_root):
    store = gui_env.store()
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, store=store, connect=True)
    good = store.get()
    store.set(dataclasses.replace(good, control_flow_id=9))
    stage_and_send(tab, 1, 11)
    assert wait_until(tk_root, lambda: tab.cell(1, "sent") == "rejected (-1)")
    store.set(good)
    stage_and_send(tab, 2, 22)
    assert wait_until(tk_root, lambda: tab.cell(2, "sent") == "ack 0")
    assert tab.cell(1, "sent") == "rejected (-1)"
    assert mock.params == {2: 22}


@pytest.mark.parametrize("event", [
    StateChanged(ConnState.DISCONNECTED, "closed by peer"),
    StateChanged(ConnState.ERROR, "receive failed: simulated"),
])
def test_link_loss_marks_every_outstanding_request(gui_env, tk_root, event):
    tab = stopped_tab(gui_env, tk_root)
    stage_and_send(tab, 1, 11)
    stage_and_send(tab, 2, 22)
    stage_and_send(tab, 3, 33)
    hold_the_pump(tab, tk_root, 3)
    tab.client.events.put(event)
    tab._pump()
    assert [tab.cell(p, "sent") for p in (1, 2, 3)] == ["no response (disconnected)"] * 3
    assert tab.pending_tags == {}


def test_disconnect_while_pending_marks_all_outstanding(gui_env, tk_root):
    tab = stopped_tab(gui_env, tk_root)
    stage_and_send(tab, 4, 44)
    stage_and_send(tab, 5, 55)
    hold_the_pump(tab, tk_root, 2)
    tab.client.disconnect()
    tab._pump()
    assert tab.state_label.cget("text") == "DISCONNECTED"
    assert [tab.cell(p, "sent") for p in (4, 5)] == ["no response (disconnected)"] * 2
    assert tab.pending_tags == {}


def test_already_answered_requests_are_not_touched_by_a_later_disconnect(gui_env, tk_root):
    mock = gui_env.mock("control")
    tab = gui_env.make_tab(ControlTab, ControlClient, mock=mock, connect=True)
    stage_and_send(tab, 1, 11)
    assert wait_until(tk_root, lambda: tab.cell(1, "sent") == "ack 0")
    tab.client.disconnect()
    assert wait_until(tk_root, lambda: tab.state_label.cget("text") == "DISCONNECTED")
    assert tab.cell(1, "sent") == "ack 0"


def test_unrecognised_reply_marks_the_oldest_outstanding_request(gui_env, tk_root):
    tab = stopped_tab(gui_env, tk_root)
    first = stage_and_send(tab, 1, 11)
    stage_and_send(tab, 2, 22)
    hold_the_pump(tab, tk_root, 2)
    tab.client.events.put(server_event([7, 7, 7]))  # a body the parser does not recognise
    tab._pump()
    assert tab.cell(1, "sent") == "unrecognised reply" and tab.cell(2, "sent") == "pending"
    assert first not in tab.pending_tags and len(tab.pending_tags) == 1
    assert has_log_line(tab, "the unrecognised reply has no tag")


def test_negative_reply_with_nothing_outstanding_is_logged(gui_env, tk_root):
    tab = stopped_tab(gui_env, tk_root)
    tab.client.events.put(server_event([-1]))
    tab._pump()
    assert has_log_line(tab, "negative reply with no outstanding request")
    assert tab.sent == {} and tab.pending_tags == {}


# ------------------------------------------------------------ strict number parsing
def _chars(*codes):
    return "".join(chr(c) for c in codes)


CONTROL_STRICT_REJECTS = ["8_0", "+80", _chars(0x663, 0x660, 0x667, 0x660), "1_0", "0x_1F", "0x1_F",
                          "0x", " ", "1e3", _chars(0xFF18, 0xFF10), "0x1F_", "8 0", "0b11",
                          _chars(0xB2), "1.2.3.4.5", "1:2:3",
                          _chars(0x661) + ".1.1.1", "1.1.1." + _chars(0x661), "1.1.1.1.",
                          _chars(0x661) + ":" + _chars(0x662), "1:" + _chars(0x663), "+1:2", "1_0:2"]


@pytest.mark.parametrize("text", CONTROL_STRICT_REJECTS)
def test_control_value_rejects_non_strict_text(text):
    with pytest.raises(ValueError):
        parse_control_value(text)


@pytest.mark.parametrize("text, word", [
    ("80", 80), (" 80 ", 80), ("0x1F", 31), ("0X1f", 31), ("0", 0), ("0x0", 0),
    ("1:3070", 0x00010BFE), (" 1:3070 ", 0x00010BFE)])
def test_control_value_accepts_strict_text(text, word):
    assert parse_control_value(text) == word


@pytest.mark.parametrize("text", [_chars(0x661, 0x2E, 0x662, 0x2E, 0x663, 0x2E, 0x664),
                                  "1.2.3", "1.2.3.4.5", "1_0.1.1.1", "+1.1.1.1", " ", ""])
def test_ip_helper_rejects_non_ascii_and_malformed(text):
    with pytest.raises(ValueError, match="IPv4"):
        parse_ip(text)


@pytest.mark.parametrize("text", ["1-3070", "1:", ":1", "+1:2", _chars(0x661) + ":2", "1_0:2", "1:2:3"])
def test_tmu_port_helper_rejects_non_strict_text(text):
    with pytest.raises(ValueError, match="TMU:port"):
        parse_tmu_port(text)


def test_ui_strict_rejection_keeps_staged_state(gui_env):
    tab = gui_env.make_tab(ControlTab, ControlClient)
    tab.select(2)
    set_entry(tab.value_entry, "8_0")
    tab.stage_button.invoke()
    assert tab.staged == {} and "Cannot read" in tab.message_text()
    set_entry(tab.value_entry, "0x_1F")
    tab.stage_button.invoke()
    assert tab.staged == {} and "Not a hex number" in tab.message_text()
