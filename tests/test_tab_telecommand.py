"""Tests for groundstation.tab_telecommand (parsing, fields, limits, sending, replies, layout)."""
import dataclasses
import struct
import types

import pytest
from tkinter import ttk
import tkinter as tk

from groundstation.clients import TelecommandClient
from groundstation.cop1 import (CLTU_MAX, DIRECTIVES, FRAME_MAX, FRAME_MIN, SEGMENT_MAX, SEGMENT_MAX_FEC,
                                AcceptReject, AdditionalInfo, AlertIndication, CltuEcho, ConfirmResponse,
                                NegativeResponse, SegmentEcho, SuspendIndication, UnknownResponse)
from groundstation.guibase import RED, checkbutton_selected, set_checkbutton, set_entry
from groundstation.net import ConnState, StateChanged
from groundstation.protocol import ProtocolOptions, to_u32
from groundstation.tab_telecommand import (MAX_ROWS, OP_AD, OP_BD, OP_CLTU, OP_DIRECTIVE, OP_FRAME, OP_NOP,
                                           OP_SUBSCRIBE, OPERATIONS, TelecommandTab, parse_ascii_bytes,
                                           parse_hex_bytes)
from gui_util import gui_env, has_log_line, pending_after_count, settle, wait_until  # noqa: F401
from test_app_gui import descendants, laid_out, settle_layout, window  # noqa: F401


def _chars(*codes):
    return "".join(chr(c) for c in codes)


def make_tab(gui_env, mock=None, connect=False, store=None, **kwargs):
    return gui_env.make_tab(TelecommandTab, TelecommandClient, mock=mock, connect=connect,
                            store=store, **kwargs)


def connected(gui_env, **mock_kwargs):
    mock = gui_env.mock("telecommand", **mock_kwargs)
    return make_tab(gui_env, mock=mock, connect=True), mock


def set_data(tab, text, fmt="Hex"):
    set_entry(tab.data_entry, text)
    tab.format_box.set(fmt)
    tab.update_length_label()


def reply(parsed):
    """A stand-in for a Received event: on_received only reads .parsed."""
    return types.SimpleNamespace(parsed=parsed)


def add_rows(tab, *tags):
    for tag in tags:
        tab._add_row(tag, "BD segment", "1", "5")


def data_of(message):
    """The data bytes of a BD/AD/CLTU/Frame request, rebuilt from the signed body words."""
    length = message.body[3]
    words = message.body[4:-1]
    return struct.pack(f">{len(words)}i", *words)[:length]


# ------------------------------------------------------------ hex and ASCII parsing
@pytest.mark.parametrize("text, expected", [
    ("0102 03", bytes([1, 2, 3])), ("0x0102", bytes([1, 2])), ("01:02,03", bytes([1, 2, 3])),
    ("ab", bytes([0xAB])), ("AB cd", bytes([0xAB, 0xCD])), ("0X0A", bytes([10])),
    (" 00 ", bytes([0])), ("0x 01", bytes([1])), ("de:ad,be ef", bytes.fromhex("deadbeef")),
    ("0b01", bytes([0x0B, 0x01])), ("1e", bytes([0x1E]))])  # all hex digits, so valid
def test_hex_accepts(text, expected):
    assert parse_hex_bytes(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "0x", "0", "012", "zz", "0x_1", "1_0", "+1", "0x0x01", "0x1", "1,2,3", "0x01_",
    "-1", "01 0", "0x0g", "0b0g", _chars(0xFF11, 0xFF12), _chars(0x661, 0x662),
    "01" + _chars(0xFF11, 0xFF12), "0x" + _chars(0x661, 0x662), "0102\n", "0x\n"])
def test_hex_rejects(text):
    with pytest.raises(ValueError):
        parse_hex_bytes(text)


def test_hex_error_messages_are_specific():
    with pytest.raises(ValueError, match="empty"):
        parse_hex_bytes("")
    with pytest.raises(ValueError, match="even number"):
        parse_hex_bytes("012")
    with pytest.raises(ValueError, match="digits 0-9 and a-f"):
        parse_hex_bytes("0x_1F")


def test_ascii_parser():
    assert parse_ascii_bytes("Hello 123") == b"Hello 123"
    with pytest.raises(ValueError, match="non-ASCII character 'é' at position 2"):
        parse_ascii_bytes("a" + _chars(0xE9))
    with pytest.raises(ValueError, match="empty"):
        parse_ascii_bytes("")


@pytest.mark.parametrize("field, text", [("vcid_entry", "8_0"), ("vcid_entry", "+3"),
                                         ("vcid_entry", _chars(0x663)), ("vcid_entry", "-1"),
                                         ("vcid_entry", ""), ("vcid_entry", "0x3")])
def test_vcid_entry_is_strict(gui_env, field, text):
    tab, mock = connected(gui_env)
    set_data(tab, "01")
    set_entry(getattr(tab, field), text)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Not sent:")
    assert tab.rows() == [] and mock.received == []


@pytest.mark.parametrize("entry, text", [("param_entry", "0x_1F"), ("param_entry", "8_0"),
                                         ("param_entry", "-5"), ("param_entry", "4294967296"),
                                         ("param_entry", _chars(0x663))])
def test_directive_parameter_is_strict(gui_env, entry, text):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_DIRECTIVE)
    set_entry(getattr(tab, entry), text)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Not sent:") and mock.received == []


@pytest.mark.parametrize("flow, auth", [("7", "0"), ("0", "3"), ("1_0", "0"), ("0", "+1"), ("", "0"),
                                        (_chars(0x663), "0"), ("0", "-1")])
def test_subscription_fields_are_strict(gui_env, flow, auth):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_SUBSCRIBE)
    set_entry(tab.flow_entry, flow)
    set_entry(tab.auth_entry, auth)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Not sent:") and mock.received == [] and tab.rows() == []


# ------------------------------------------------------------ operation switching shows the right fields
EXPECTED_GROUPS = {
    OP_BD: {"vcid", "data", "length"}, OP_AD: {"vcid", "data", "length"},
    OP_CLTU: {"vcid", "data", "length"}, OP_FRAME: {"codeblock", "data", "length"},
    OP_DIRECTIVE: {"vcid", "directive"}, OP_NOP: {"vcid"}, OP_SUBSCRIBE: {"vcid", "subscription"}}


def test_operation_switching_shows_exactly_the_right_fields(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[2]
        settle_layout(tk_root, notebook, tab)
        assert tuple(tab.operation_box.cget("values")) == OPERATIONS
        for operation in OPERATIONS:
            tab.set_operation(operation)
            for _ in range(3):
                tk_root.update()
            assert tab.visible_groups() == EXPECTED_GROUPS[operation], operation
            for name, group in tab.groups.items():
                assert bool(group.winfo_ismapped()) == (name in EXPECTED_GROUPS[operation]), (operation, name)
            widgets = {"vcid": tab.vcid_entry, "codeblock": tab.codeblock_box, "data": tab.data_entry,
                       "format": tab.format_box, "directive": tab.directive_box, "param": tab.param_entry,
                       "flow": tab.flow_entry, "auth": tab.auth_entry}
            shown = {name for name, w in widgets.items() if w.winfo_ismapped()}
            expected = set()
            for group in EXPECTED_GROUPS[operation]:
                expected |= {"vcid": {"vcid"}, "codeblock": {"codeblock"}, "data": {"data", "format"},
                             "directive": {"directive", "param"}, "subscription": {"flow", "auth"},
                             "length": set()}[group]
            assert shown == expected, operation
        # Fields are hidden, not destroyed: the values survive a round trip through other operations.
        tab.set_operation(OP_BD)
        set_entry(tab.vcid_entry, "12")
        tab.set_operation(OP_NOP)
        tab.set_operation(OP_BD)
        assert tab.vcid_entry.get() == "12"


def test_codeblock_choices_and_directive_list_come_from_cop1(gui_env):
    tab = make_tab(gui_env)
    assert tuple(tab.codeblock_box.cget("values")) == ("0", "5", "6", "7", "8")
    listed = {int(v.split(" ", 1)[0]): v.split(" ", 1)[1] for v in tab.directive_box.cget("values")}
    assert listed == DIRECTIVES
    assert tuple(tab.format_box.cget("values")) == ("Hex", "ASCII")
    assert str(tab.operation_box.cget("state")) == "readonly"


# ------------------------------------------------------------ live limit label
def test_limit_label_follows_fec_and_turns_red_outside(gui_env):
    store = gui_env.store()
    tab = make_tab(gui_env, store=store)
    set_data(tab, "01 02 03 04 05")
    assert tab.length_text() == f"5 bytes (allowed 1 to {SEGMENT_MAX})" == "5 bytes (allowed 1 to 1019)"
    assert str(tab.length_label.cget("fg")) != RED
    set_data(tab, "00" * 1019)
    assert tab.length_text() == "1019 bytes (allowed 1 to 1019)" and str(tab.length_label.cget("fg")) != RED
    set_data(tab, "00" * 1020)
    assert tab.length_text() == "1020 bytes (allowed 1 to 1019)" and str(tab.length_label.cget("fg")) == RED
    store.set(dataclasses.replace(store.get(), fec_in_frame=True))  # the label follows the shared store
    assert tab.length_text() == f"1020 bytes (allowed 1 to {SEGMENT_MAX_FEC})" == "1020 bytes (allowed 1 to 1017)"
    set_data(tab, "00" * 1017)
    assert tab.length_text() == "1017 bytes (allowed 1 to 1017)" and str(tab.length_label.cget("fg")) != RED
    set_data(tab, "00" * 1018)
    assert str(tab.length_label.cget("fg")) == RED
    store.set(dataclasses.replace(store.get(), fec_in_frame=False))
    assert tab.length_text() == "1018 bytes (allowed 1 to 1019)" and str(tab.length_label.cget("fg")) != RED


def test_limit_label_ranges_for_every_data_operation(gui_env):
    tab = make_tab(gui_env)
    expected = {OP_BD: (1, SEGMENT_MAX), OP_AD: (1, SEGMENT_MAX), OP_CLTU: (1, CLTU_MAX),
                OP_FRAME: (FRAME_MIN, FRAME_MAX)}
    assert (SEGMENT_MAX, CLTU_MAX, FRAME_MIN, FRAME_MAX) == (1019, 1284, 6, 1024)
    for operation, (low, high) in expected.items():
        tab.set_operation(operation)
        set_data(tab, "00" * low)
        assert tab.length_text() == f"{low} bytes (allowed {low} to {high})"
        assert str(tab.length_label.cget("fg")) != RED
        set_data(tab, "00" * high)
        assert str(tab.length_label.cget("fg")) != RED
        set_data(tab, "00" * (high + 1))
        assert str(tab.length_label.cget("fg")) == RED
        set_data(tab, "00" * (low - 1)) if low > 1 else set_data(tab, "")
        assert str(tab.length_label.cget("fg")) == RED
    tab.set_operation(OP_NOP)
    assert tab.length_text() == ""  # no data field, no label


def test_limit_label_with_ascii_and_invalid_data(gui_env):
    tab = make_tab(gui_env)
    set_data(tab, "Hello", "ASCII")
    assert tab.length_text() == "5 bytes (allowed 1 to 1019)"
    set_data(tab, "H" + _chars(0xE9), "ASCII")
    assert "non-ASCII character" in tab.length_text() and str(tab.length_label.cget("fg")) == RED
    set_data(tab, "zz", "Hex")
    assert "digits 0-9 and a-f" in tab.length_text() and str(tab.length_label.cget("fg")) == RED
    set_data(tab, "", "Hex")
    assert tab.length_text() == "0 bytes (allowed 1 to 1019)" and str(tab.length_label.cget("fg")) == RED


# ------------------------------------------------------------ every operation reaches the mock exactly
def send_and_wait(tab, tk_root, mock, count=1):
    tab.send_button.invoke()
    assert tab.message_text().startswith("Sent "), tab.message_text()
    assert wait_until(tk_root, lambda: len(mock.received) >= count)
    return mock.received[count - 1]


def test_bd_segment_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    set_entry(tab.vcid_entry, "5")
    set_data(tab, "de ad be ef 01")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body[:4] == (30, 5, tag, 5) and data_of(message) == bytes.fromhex("deadbeef01")
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "accepted")
    assert tab.rows()[0][1:] == (str(tag), "BD segment", "5", "5", "accepted")


def test_ad_segment_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_AD)
    set_entry(tab.vcid_entry, "63")
    set_data(tab, "0102030405060708")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body[:4] == (20, 63, tag, 8) and data_of(message) == bytes(range(1, 9))
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "confirmed (positive) (2)")  # accept + confirm


def test_cltu_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_CLTU)
    set_entry(tab.vcid_entry, "7")
    set_data(tab, "aa")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body[:4] == (40, 7, tag, 1) and data_of(message) == b"\xaa"
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "accepted")


def test_transfer_frame_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_FRAME)
    tab.codeblock_box.set("7")
    set_data(tab, "010203040506")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body[:4] == (50, 7, tag, 6) and data_of(message) == bytes(range(1, 7))
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "accepted")
    assert tab.rows()[0][2:5] == ("Transfer Frame (codeblock 7)", "-", "6")


@pytest.mark.parametrize("codeblock", ["0", "5", "6", "7", "8"])
def test_every_codeblock_length_is_sent(gui_env, tk_root, codeblock):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_FRAME)
    tab.codeblock_box.set(codeblock)
    set_data(tab, "00" * 6)
    message = send_and_wait(tab, tk_root, mock)
    assert message.body[1] == int(codeblock)


def test_directive_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_DIRECTIVE)
    tab.directive_box.set("8 Set V(S)")
    set_entry(tab.vcid_entry, "3")
    set_entry(tab.param_entry, "0x1234")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body == (8, 3, tag, 0x1234)
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "confirmed (positive) (2)")
    assert tab.rows()[0][2] == "Directive 8 (Set V(S))"


def test_every_directive_op_is_sent(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_DIRECTIVE)
    for op, name in DIRECTIVES.items():
        tab.directive_box.set(f"{op} {name}")
        tab.send_button.invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == len(DIRECTIVES))
    assert [m.body[0] for m in mock.received] == list(DIRECTIVES)


def test_nop_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_NOP)
    set_entry(tab.vcid_entry, "9")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body == (13, 9, tag, 0)
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "confirmed (positive) (2)")


def test_subscription_exact_fields(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_SUBSCRIBE)
    set_entry(tab.vcid_entry, "4")
    set_entry(tab.flow_entry, "5")
    set_entry(tab.auth_entry, "1")
    message = send_and_wait(tab, tk_root, mock)
    tag = next(iter(tab.requests))
    assert message.body == (14, 4, tag, 5, 1)  # flow before authorization
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "accepted")
    assert tab.rows()[0][2] == "Output flow subscription (flow 5, authorization 1)"


def test_ascii_format_sends_ascii_bytes(gui_env, tk_root):
    tab, mock = connected(gui_env)
    set_data(tab, "Hi!", "ASCII")
    message = send_and_wait(tab, tk_root, mock)
    assert data_of(message) == b"Hi!"


def test_tags_increase_and_rows_are_in_send_order(gui_env, tk_root):
    tab, mock = connected(gui_env)
    for n in range(3):
        set_data(tab, f"{n:02x}")
        tab.send_button.invoke()
    assert wait_until(tk_root, lambda: all(tab.status_of(t) == "accepted" for t in tab.requests))
    assert list(tab.requests) == [1, 2, 3]
    assert [r[1] for r in tab.rows()] == ["1", "2", "3"]


# ------------------------------------------------------------ Validate OFF versus ON
def test_validate_on_refuses_oversized_data_locally(gui_env, tk_root):
    tab, mock = connected(gui_env)
    assert checkbutton_selected(tab.validate_check)  # default ON
    set_data(tab, "00" * 1020)
    tab.send_button.invoke()
    assert "Data length 1020 is outside the allowed 1 to 1019 bytes" in tab.message_text()
    assert tab.rows() == [] and tab.requests == {}
    settle(tk_root, 0.3)
    assert mock.received == [] and tab.sent_count == 0


def test_validate_off_really_sends_the_oversized_segment(gui_env, tk_root):
    tab, mock = connected(gui_env)
    set_checkbutton(tab.validate_check, False)
    assert not checkbutton_selected(tab.validate_check)
    set_data(tab, "00" * 1020)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Sent ")
    assert wait_until(tk_root, lambda: len(mock.received) == 1)
    assert mock.received[0].body[3] == 1020 and len(data_of(mock.received[0])) == 1020
    tag = next(iter(tab.requests))
    assert wait_until(tk_root, lambda: tab.status_of(tag) == "rejected (-1)")  # the server answers -1
    assert has_log_line(tab, "the negative reply has no tag")


def test_validate_off_also_skips_the_vcid_range_but_still_needs_digits(gui_env, tk_root):
    tab, mock = connected(gui_env)
    set_data(tab, "01")
    set_entry(tab.vcid_entry, "64")
    tab.send_button.invoke()
    assert "VCID must be 0 to 63" in tab.message_text() and mock.received == []
    set_checkbutton(tab.validate_check, False)
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == 1) and mock.received[0].body[1] == 64
    set_entry(tab.vcid_entry, "6_4")
    tab.send_button.invoke()
    assert tab.message_text().startswith("Not sent:") and len(mock.received) == 1


def test_validate_note_is_shown(gui_env):
    tab = make_tab(gui_env)
    assert tab.validate_note.cget("text") == "Off sends out-of-range data to the server on purpose."


def test_frame_below_minimum_is_refused_locally_and_sent_when_off(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.set_operation(OP_FRAME)
    set_data(tab, "0102")
    tab.send_button.invoke()
    assert "outside the allowed 6 to 1024 bytes" in tab.message_text() and mock.received == []
    set_checkbutton(tab.validate_check, False)
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == 1)
    assert wait_until(tk_root, lambda: tab.status_of(1) == "rejected (-1)")


# ------------------------------------------------------------ replies update exactly the matching row
INFO = AdditionalInfo(vcid=1, last_clcw=2, state=3, last_event=4, v_s=5, nn_r=6, n_r=7, timer_initial=8,
                      timeout_type=9, transmission_limit=10, transmission_count=11, suspended=0,
                      sliding_window=12)


@pytest.mark.parametrize("make_reply, status", [
    (lambda t: AcceptReject(op=30, vcid=1, tag=t, accepted=True), "accepted"),
    (lambda t: AcceptReject(op=30, vcid=1, tag=t, accepted=False), "rejected"),
    (lambda t: ConfirmResponse(op=20, vcid=1, tag=t, positive=True), "confirmed (positive)"),
    (lambda t: ConfirmResponse(op=20, vcid=1, tag=t, positive=False), "confirmed (negative)"),
    (lambda t: SegmentEcho(op=30, field=1, tag=t, length=2, data=b"\x01\x02"), "echo received"),
    (lambda t: CltuEcho(vcid=1, tag=t, frame_sequence=3, error_location=0, length=1, data=b"\xaa"),
     "echo received"),
])
def test_tagged_reply_updates_exactly_the_matching_row(gui_env, make_reply, status):
    tab = make_tab(gui_env)
    add_rows(tab, 11, 12, 13)
    tab.on_received(reply(make_reply(12)))
    assert [tab.status_of(t) for t in (11, 12, 13)] == ["pending", status, "pending"]


@pytest.mark.parametrize("make_reply", [
    lambda t: AcceptReject(op=30, vcid=1, tag=t, accepted=True),
    lambda t: ConfirmResponse(op=20, vcid=1, tag=t, positive=True),
    lambda t: SegmentEcho(op=30, field=1, tag=t, length=0, data=b""),
    lambda t: CltuEcho(vcid=1, tag=t, frame_sequence=0, error_location=0, length=0, data=b""),
])
def test_reply_with_an_unknown_tag_is_logged_and_changes_nothing(gui_env, make_reply):
    tab = make_tab(gui_env)
    add_rows(tab, 11, 12)
    before = tab.rows()
    tab.on_received(reply(make_reply(999)))
    assert tab.rows() == before
    assert has_log_line(tab, "tag 999 not sent by this tab")


def test_echo_replies_are_logged_in_full(gui_env):
    tab = make_tab(gui_env)
    add_rows(tab, 5)
    tab.on_received(reply(SegmentEcho(op=30, field=1, tag=5, length=2, data=b"\x01\xfe")))
    tab.on_received(reply(CltuEcho(vcid=1, tag=5, frame_sequence=3, error_location=2, length=1, data=b"\xaa")))
    assert has_log_line(tab, "SegmentEcho(op=30, field=1, tag=5, length=2, data=01fe)")
    assert has_log_line(tab, "CltuEcho(vcid=1, tag=5, frame_sequence=3, error_location=2, length=1, data=aa)")


def test_untagged_replies_are_logged_only(gui_env):
    tab = make_tab(gui_env)
    add_rows(tab, 5, 6)
    before = tab.rows()
    for parsed in (INFO, AlertIndication(vcid=1, code=5, name="CLCW"), SuspendIndication(vcid=2)):
        tab.on_received(reply(parsed))
    assert tab.rows() == before
    assert has_log_line(tab, "AdditionalInfo(vcid=1, last_clcw=2, state=3, last_event=4, v_s=5, nn_r=6, "
                             "n_r=7, timer_initial=8, timeout_type=9, transmission_limit=10, "
                             "transmission_count=11, suspended=0, sliding_window=12)")
    assert has_log_line(tab, "AlertIndication(vcid=1, code=5, name=CLCW)")
    assert has_log_line(tab, "SuspendIndication(vcid=2)")


def test_alert_or_suspend_that_carries_a_tag_marks_its_row(gui_env):
    @dataclasses.dataclass(frozen=True)
    class TaggedAlert(AlertIndication):
        tag: int = 0

    @dataclasses.dataclass(frozen=True)
    class TaggedSuspend(SuspendIndication):
        tag: int = 0

    tab = make_tab(gui_env)
    add_rows(tab, 5, 6)
    tab.on_received(reply(TaggedAlert(vcid=1, code=5, name="CLCW", tag=5)))
    tab.on_received(reply(TaggedSuspend(vcid=1, tag=6)))
    assert [tab.status_of(5), tab.status_of(6)] == ["alert", "suspended"]


# ------------------------------------------------------------ several replies for one tag
def test_multiple_replies_show_latest_status_with_the_count(gui_env):
    tab = make_tab(gui_env)
    add_rows(tab, 1, 2)
    tab.on_received(reply(AcceptReject(op=20, vcid=1, tag=1, accepted=True)))
    assert tab.status_of(1) == "accepted"  # one reply: no count
    tab.on_received(reply(ConfirmResponse(op=20, vcid=1, tag=1, positive=True)))
    assert tab.status_of(1) == "confirmed (positive) (2)"
    tab.on_received(reply(ConfirmResponse(op=20, vcid=1, tag=1, positive=False)))
    assert tab.status_of(1) == "confirmed (negative) (3)"
    assert tab.status_of(2) == "pending"


# ------------------------------------------------------------ tagless failure replies use the oldest pending
def test_negative_reply_hits_only_the_oldest_pending_request(gui_env):
    tab = make_tab(gui_env)
    add_rows(tab, 1, 2, 3)
    tab.on_received(reply(AcceptReject(op=30, vcid=1, tag=1, accepted=True)))  # no longer pending
    tab.on_received(reply(NegativeResponse()))
    assert [tab.status_of(t) for t in (1, 2, 3)] == ["accepted", "rejected (-1)", "pending"]
    assert has_log_line(tab, "the negative reply has no tag, so the oldest pending request (tag 2) was marked")
    tab.on_received(reply(NegativeResponse()))
    assert [tab.status_of(t) for t in (1, 2, 3)] == ["accepted", "rejected (-1)", "rejected (-1)"]


def test_unrecognised_reply_hits_only_the_oldest_pending_request(gui_env):
    tab = make_tab(gui_env)
    add_rows(tab, 1, 2)
    tab.on_received(reply(UnknownResponse(raw=b"\x00", reason="x")))
    assert [tab.status_of(t) for t in (1, 2)] == ["unrecognised reply", "pending"]
    assert has_log_line(tab, "the unrecognised reply has no tag, so the oldest pending request (tag 1)")


def test_failure_replies_with_nothing_pending_only_log(gui_env):
    tab = make_tab(gui_env)
    tab.on_received(reply(NegativeResponse()))
    tab.on_received(reply(UnknownResponse(raw=b"", reason="x")))
    assert tab.rows() == []
    assert has_log_line(tab, "negative reply with no pending request")
    assert has_log_line(tab, "unrecognised reply with no pending request")
    add_rows(tab, 1)
    tab.on_received(reply(AcceptReject(op=30, vcid=1, tag=1, accepted=True)))
    tab.on_received(reply(NegativeResponse()))  # row 1 is not pending any more
    assert tab.status_of(1) == "accepted"


def test_real_server_negative_reply_marks_the_request(gui_env, tk_root):
    store = gui_env.store()
    mock = gui_env.mock("telecommand")
    tab = make_tab(gui_env, mock=mock, connect=True, store=store)
    store.set(dataclasses.replace(store.get(), telecommand_flow_id=9))  # the mock expects 0
    set_data(tab, "01")
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.status_of(1) == "rejected (-1)")


# ------------------------------------------------------------ link loss
@pytest.mark.parametrize("event", [StateChanged(ConnState.DISCONNECTED, "closed by peer"),
                                   StateChanged(ConnState.ERROR, "receive failed: simulated")])
def test_link_loss_marks_every_pending_request(gui_env, event):
    tab = make_tab(gui_env)
    add_rows(tab, 1, 2, 3)
    tab.on_received(reply(AcceptReject(op=30, vcid=1, tag=3, accepted=True)))
    tab.client.events.put(event)
    tab._pump()
    assert [tab.status_of(t) for t in (1, 2, 3)] == ["no response (disconnected)"] * 2 + ["accepted"]


def test_real_disconnect_marks_pending_requests(gui_env, tk_root):
    from groundstation.net import Received
    tab, mock = connected(gui_env)
    tab.after_cancel(tab._after_id)  # hold the pump so the replies stay unread
    tab._after_id = None
    for text in ("01", "02"):
        set_data(tab, text)
        tab.send_button.invoke()
    queued = lambda: sum(isinstance(e, Received) for e in list(tab.client.events.queue))
    assert wait_until(tk_root, lambda: queued() >= 2)
    tab.client.drain()  # throw the replies away: both requests are still pending
    assert [tab.status_of(t) for t in tab.requests] == ["pending", "pending"]
    tab.client.disconnect()
    tab._pump()
    assert [tab.status_of(t) for t in tab.requests] == ["no response (disconnected)"] * 2
    assert tab.state_label.cget("text") == "DISCONNECTED"


# ------------------------------------------------------------ bounds
def test_table_and_tag_dictionary_are_bounded(gui_env):
    tab = make_tab(gui_env)
    for tag in range(1, MAX_ROWS + 6):
        tab._add_row(tag, "BD segment", "1", "5")
    assert MAX_ROWS == 200
    assert len(tab.requests) == 200 and len(tab.tree.get_children()) == 200
    assert 1 not in tab.requests and 5 not in tab.requests and 6 in tab.requests and 205 in tab.requests
    assert [r[1] for r in tab.rows()][0] == "6" and [r[1] for r in tab.rows()][-1] == "205"
    tab.on_received(reply(AcceptReject(op=30, vcid=1, tag=1, accepted=True)))  # an evicted tag is unknown
    assert has_log_line(tab, "tag 1 not sent by this tab")


def test_bound_also_holds_for_real_sends(gui_env, tk_root):
    tab, mock = connected(gui_env)
    tab.after_cancel(tab._after_id)
    tab._after_id = None
    set_data(tab, "01")
    for _ in range(MAX_ROWS + 1):
        tab.send_button.invoke()
    assert len(tab.requests) == 200 and len(tab.tree.get_children()) == 200
    assert 1 not in tab.requests and 2 in tab.requests and 201 in tab.requests


# ------------------------------------------------------------ disconnected
def test_send_while_disconnected_shows_an_error_and_adds_no_row(gui_env):
    tab = make_tab(gui_env)
    set_data(tab, "01 02")
    tab.send_button.invoke()
    assert "Not sent:" in tab.message_text() and "not connected" in tab.message_text()
    assert str(tab.message.cget("fg")) == RED
    assert tab.rows() == [] and tab.requests == {}


# ------------------------------------------------------------ layout
def in_hidden_group(tab, widget):
    """True when the widget sits in a field group that the grid currently hides."""
    parent = widget.master
    while parent is not None and parent is not tab:
        if parent in tab.groups.values() and not parent.grid_info():
            return True
        parent = parent.master
    return False


@pytest.mark.parametrize("scale", [1.0, 2.0])
@pytest.mark.parametrize("size", ["1000x720", "900x600"])
def test_nothing_is_clipped_for_every_operation(window, tk_root, size, scale):
    with laid_out(tk_root, size, scale):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[2]
        settle_layout(tk_root, notebook, tab)
        limit = tk_root.winfo_rootx() + tk_root.winfo_width()
        for operation in OPERATIONS:
            tab.set_operation(operation)
            for _ in range(5):
                tk_root.update()
            checked = 0
            for w in descendants(tab):
                if not isinstance(w, (ttk.Button, ttk.Entry, tk.Entry, ttk.Checkbutton)):
                    continue
                if in_hidden_group(tab, w):
                    continue  # a field group of another operation (hidden, not destroyed)
                if w.winfo_height() < w.winfo_reqheight():
                    continue  # vertical squeeze in a small window, not horizontal clipping
                name = f"{operation}: {w.winfo_class()} {str(w)}"
                assert w.winfo_ismapped(), name
                assert w.winfo_width() >= w.winfo_reqwidth(), (name, w.winfo_width(), w.winfo_reqwidth())
                assert w.winfo_rootx() + w.winfo_width() <= limit, name
                checked += 1
            assert checked >= 6, (operation, checked)
            assert tab.send_button.winfo_ismapped() and tab.send_button.winfo_width() >= tab.send_button.winfo_reqwidth()


def test_log_shows_at_least_eight_lines_for_every_operation(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[2]
        settle_layout(tk_root, notebook, tab)
        for operation in OPERATIONS:
            tab.set_operation(operation)
            for _ in range(5):
                tk_root.update()
            text = tab.log.text
            line_height = int(tk_root.tk.call("font", "metrics", text.cget("font"), "-linespace"))
            assert text.winfo_height() // line_height >= 8, (operation, text.winfo_height(), line_height)


def test_requests_table_has_a_working_vertical_scrollbar(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        tab = tk_root.service_tabs[2]
        settle_layout(tk_root, notebook, tab)
        for tag in range(1, 40):
            tab._add_row(tag, "BD segment", "1", "5")
        for _ in range(3):
            tk_root.update()
        assert str(tab.tree_scroll.cget("orient")) == "vertical" and tab.tree_scroll.master is tab.tree.master
        first, last = tab.tree_scroll.get()
        assert last - first < 1.0  # the tree shows only part of its rows
        tab.tree_scroll.tk.call(tab.tree_scroll.cget("command"), "moveto", "0.5")
        assert tab.tree.yview()[0] > 0.0
        assert [c for c in tab.tree["columns"]] == ["time", "tag", "op", "vcid", "bytes", "status"]


# ------------------------------------------------------------ destroy
def test_destroy_cancels_callbacks_unsubscribes_and_leaves_no_thread(gui_env, tk_root):
    errors = []
    previous = tk_root.report_callback_exception
    tk_root.report_callback_exception = lambda *exc: errors.append(exc)
    try:
        store = gui_env.store()
        mock = gui_env.mock("telecommand")
        subscribers_before = len(store._subscribers)
        baseline = pending_after_count(tk_root)
        tab = make_tab(gui_env, mock=mock, connect=True, store=store)
        assert len(store._subscribers) == subscribers_before + 2  # base class and the length label
        assert pending_after_count(tk_root) == baseline + 1
        client = tab.client
        tab.shutdown()
        assert client.state == ConnState.DISCONNECTED
        assert client._thread is None or not client._thread.is_alive()
        tab.destroy()
        tk_root.update_idletasks()
        assert pending_after_count(tk_root) == baseline
        assert len(store._subscribers) == subscribers_before
        settle(tk_root, 0.3)
        store.set(dataclasses.replace(store.get(), fec_in_frame=True))  # must not touch the dead tab
        assert errors == []
    finally:
        tk_root.report_callback_exception = previous


# ------------------------------------------------------------ shared options
def test_changing_the_telecommand_flow_id_changes_the_next_request(gui_env, tk_root):
    store = gui_env.store()
    mock = gui_env.mock("telecommand", opts=ProtocolOptions(telecommand_flow_id=0x55))
    tab = make_tab(gui_env, mock=mock, connect=True, store=store)
    set_data(tab, "01")
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.status_of(1) == "rejected (-1)")  # default flow 0 is wrong here
    assert mock.received[0].flow_id == 0
    store.set(dataclasses.replace(store.get(), telecommand_flow_id=0x55))
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: len(mock.received) == 2)
    assert mock.received[1].flow_id == 0x55
    assert wait_until(tk_root, lambda: tab.status_of(2) == "accepted")


def test_checksum_mode_from_the_store_is_used(gui_env, tk_root):
    store = gui_env.store()
    mock = gui_env.mock("telecommand", opts=ProtocolOptions(checksum_mode="payload_only"))
    tab = make_tab(gui_env, mock=mock, connect=True, store=store)
    set_data(tab, "01")
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.status_of(1) == "rejected (-1)")  # default whole_packet mismatches
    store.set(dataclasses.replace(store.get(), checksum_mode="payload_only"))
    tab.send_button.invoke()
    assert wait_until(tk_root, lambda: tab.status_of(2) == "accepted")


def test_fec_in_frame_changes_what_validation_accepts(gui_env, tk_root):
    store = gui_env.store()
    tab, mock = None, gui_env.mock("telecommand")
    tab = make_tab(gui_env, mock=mock, connect=True, store=store)
    store.set(dataclasses.replace(store.get(), fec_in_frame=True))
    set_data(tab, "00" * 1018)
    tab.send_button.invoke()
    assert "outside the allowed 1 to 1017 bytes" in tab.message_text() and mock.received == []
    set_data(tab, "00" * 1017)
    tab.send_button.invoke()
    assert tab.message_text().startswith("Sent ")


# ------------------------------------------------------------ app wiring
def test_default_window_has_a_telecommand_tab_on_the_telecommand_client(window, tk_root):
    _, notebook, _ = window()
    tabs = [notebook.nametowidget(n) for n in notebook.tabs()]
    assert isinstance(tabs[2], TelecommandTab) and tabs[2] is tk_root.service_tabs[2]
    assert isinstance(tabs[2].client, TelecommandClient)
    assert (tabs[2].client.host, tabs[2].client.port) == ("127.0.0.1", 3120)
    assert tabs[2].port_entry.get() == "3120"


def test_host_and_port_offset_apply_to_the_telecommand_tab(window, tk_root):
    _, notebook, _ = window(host="10.1.2.3", port_offset=100)
    tab = tk_root.service_tabs[2]
    assert (tab.client.host, tab.client.port) == ("10.1.2.3", 3220)


def test_a_ready_made_telecommand_client_can_be_passed_in(window, tk_root):
    client = TelecommandClient("192.0.2.9", 4000)
    window(clients={"telecommand": client})
    assert tk_root.service_tabs[2].client is client


def test_close_path_calls_shutdown_of_the_telecommand_tab(window, tk_root, gui_env, monkeypatch):
    from groundstation.app import shutdown_window
    _, notebook, _ = window()
    tab = tk_root.service_tabs[2]
    mock = gui_env.mock("telecommand")
    set_entry(tab.port_entry, str(mock.port))
    tab.toggle_connection()
    assert wait_until(tk_root, lambda: tab.state_label.cget("text") == "CONNECTED")
    calls = []
    original = tab.shutdown
    monkeypatch.setattr(tab, "shutdown", lambda: (calls.append("telecommand"), original()))
    shutdown_window(tk_root)
    assert calls == ["telecommand"] and tab.client.state == ConnState.DISCONNECTED
