"""Telecommand tab: send COP-1 requests and follow their replies.

Requests: AD segment, BD segment, CLTU, Transfer Frame, directive, NOP and output flow
subscription. Replies: negative acknowledgement, accept/reject, confirm, alert, suspend,
Transfer Frame and Segment echo, CLTU echo and additional information.

Only accept/reject, confirm and the two echoes carry a tag. The negative and the
unrecognised reply carry none, so (as in tab_control.py) they are attributed to the
OLDEST request that is still pending, assuming replies come back in order. Alert,
suspend and additional information carry no tag either, so they are only logged.
"""
import dataclasses
import re
import time
import tkinter as tk
from collections import OrderedDict
from tkinter import ttk
from typing import Optional

from .cop1 import (CLTU_MAX, CODEBLOCK_LENGTHS, DIRECTIVES, FRAME_MAX, FRAME_MIN, SEGMENT_MAX,
                   SEGMENT_MAX_FEC, VCID_MAX, AcceptReject, AdditionalInfo, AlertIndication,
                   CltuEcho, ConfirmResponse, NegativeResponse, SegmentEcho, SuspendIndication,
                   UnknownResponse)
from . import guibase
from .guibase import (GREEN, RED, ServiceTab, checkbutton_selected, make_checkbutton,
                      parse_ascii_uint, set_checkbutton, set_entry)
from .net import ClientError, ConnState, Received
from .protocol import ProtocolError

MAX_ROWS = 200  # rows in the table and entries in the tag dictionary (one and the same)
OP_BD, OP_AD, OP_CLTU = "BD segment", "AD segment", "CLTU"
OP_FRAME, OP_DIRECTIVE, OP_NOP = "Transfer Frame", "Directive", "NOP"
OP_SUBSCRIBE = "Output flow subscription"
OPERATIONS = (OP_BD, OP_AD, OP_CLTU, OP_FRAME, OP_DIRECTIVE, OP_NOP, OP_SUBSCRIBE)
DATA_OPS = (OP_BD, OP_AD, OP_CLTU, OP_FRAME)
FLOW_MAX = 6            # output flows 0..6
AUTHORIZATION_MAX = 2   # authorization 0..2
_U32_MAX = 0xFFFFFFFF
_HEX_SPLIT = re.compile(r"[ \t,:]+")  # the separators between groups of hex digits
_HEX_DIGITS = re.compile(r"[0-9A-Fa-f]+")
_NEGATIVE = re.compile(r"-[0-9]+")

PENDING = "pending"
STATUS_ACCEPTED, STATUS_REJECTED = "accepted", "rejected"
STATUS_CONFIRM_POS, STATUS_CONFIRM_NEG = "confirmed (positive)", "confirmed (negative)"
STATUS_ECHO, STATUS_ALERT, STATUS_SUSPENDED = "echo received", "alert", "suspended"
STATUS_NEGATIVE, STATUS_UNRECOGNISED = "rejected (-1)", "unrecognised reply"
STATUS_NO_RESPONSE = "no response (disconnected)"
STATUS_TIMEOUT = "no response (timeout)"
# Operations that get an immediate reply, so a silent server is shown as a timeout. Every request
# builder in cop1.py has an operation code that the Accept/Reject reply carries
# back with the tag, and the mock answers each of them. Neither cop1.py nor clients.py documents a
# request that is NOT answered, so none is exempted. This is the one place to remove an operation.
EXPECTS_REPLY = frozenset(OPERATIONS)
COLUMNS = (("time", "Time", 70), ("tag", "Tag", 70), ("op", "Operation", 300),
           ("vcid", "VCID", 50), ("bytes", "Bytes", 60), ("status", "Status", 200))


# ---------------------------------------------------------------- parsing
def parse_hex_bytes(text: str) -> bytes:
    """Hex text to bytes: groups split by spaces, commas or colons, one optional leading 0x.

    Every group must have an even number of digits, so "AB CD" is fine but "A BCD" is not:
    a group with an odd count is almost always a typo and would silently shift every byte after it.
    """
    groups = [g for g in _HEX_SPLIT.split(text) if g]
    if not groups:
        raise ValueError("Data is empty")
    if groups[0][:2].lower() == "0x":
        groups[0] = groups[0][2:]  # exactly one prefix is allowed
        if not groups[0]:
            groups.pop(0)  # a lone "0x" in front of the data
            if not groups:
                raise ValueError("Data is empty after the 0x prefix")
    for group in groups:
        if not _HEX_DIGITS.fullmatch(group):
            raise ValueError("Hex data may only contain the digits 0-9 and a-f")
    for group in groups:
        if len(group) % 2:
            raise ValueError(f"Hex data needs an even number of digits (two per byte) in every group; "
                             f"{group!r} has {len(group)}")
    return bytes.fromhex("".join(groups))


def parse_ascii_bytes(text: str) -> bytes:
    """ASCII text to bytes; non-ASCII characters are rejected with their position."""
    if not text:
        raise ValueError("Data is empty")
    for index, ch in enumerate(text):
        if ord(ch) > 127:
            raise ValueError(f"ASCII data contains the non-ASCII character {ch!r} at position {index + 1}")
    return text.encode("ascii")


def _uint(text: str, what: str, limit: int, hex_ok: bool = False) -> int:
    """Strict unsigned integer 0..limit with a readable message."""
    stripped = text.strip()
    if _NEGATIVE.fullmatch(stripped):
        raise ValueError(f"{what} must not be negative")
    try:
        value = parse_ascii_uint(stripped, hex_ok=hex_ok)
    except ValueError:
        kind = "decimal or 0x hex number" if hex_ok else "whole number"
        raise ValueError(f"{what} must be a {kind}") from None
    if not 0 <= value <= limit:
        raise ValueError(f"{what} must be 0 to {limit if limit < 1000 else hex(limit)}")
    return value


def describe_reply(parsed: object) -> str:
    """Every field of a parsed reply in one line (bytes as hex), for the log."""
    if dataclasses.is_dataclass(parsed) and not isinstance(parsed, type):
        parts = []
        for f in dataclasses.fields(parsed):
            value = getattr(parsed, f.name)
            parts.append(f"{f.name}={value.hex() if isinstance(value, (bytes, bytearray)) else value}")
        return f"{type(parsed).__name__}({', '.join(parts)})"
    return repr(parsed)


class TelecommandTab(ServiceTab):
    """Build one request at a time, send it, and track each request's replies."""

    def __init__(self, master, client, store, **kwargs) -> None:
        self.requests: "OrderedDict[int, dict]" = OrderedDict()  # tag -> row state, oldest first
        self._unsubscribe_store = None
        super().__init__(master, client, store, "Telecommand", **kwargs)

    # ------------------------------------------------------------ widgets
    def build_body(self, parent: tk.Misc) -> None:
        panel = ttk.Frame(parent)
        panel.pack(fill="x")
        op_row = ttk.Frame(panel)
        op_row.pack(fill="x", pady=(2, 0))
        ttk.Label(op_row, text="Operation").pack(side="left")
        self.operation_box = ttk.Combobox(op_row, state="readonly", width=26, values=OPERATIONS)
        self.operation_box.set(OP_BD)
        self.operation_box.pack(side="left", padx=4)
        self.operation_box.bind("<<ComboboxSelected>>", lambda e: self._on_operation_changed())

        # Field groups live in a grid so they can be hidden and shown without being destroyed.
        fields = ttk.Frame(panel)
        fields.pack(fill="x")
        self.groups: "dict[str, ttk.Frame]" = {}
        for row, name in enumerate(("vcid", "codeblock", "data", "directive", "subscription", "length")):
            group = ttk.Frame(fields)
            group.grid(row=row, column=0, sticky="w", pady=1)
            self.groups[name] = group
        group = self.groups["vcid"]
        ttk.Label(group, text="VCID (0 to 63)").pack(side="left")
        self.vcid_entry = ttk.Entry(group, width=6)
        self.vcid_entry.pack(side="left", padx=4)
        set_entry(self.vcid_entry, "0")
        group = self.groups["codeblock"]
        ttk.Label(group, text="Codeblock length").pack(side="left")
        self.codeblock_box = ttk.Combobox(group, state="readonly", width=4,
                                          values=[str(c) for c in CODEBLOCK_LENGTHS])
        self.codeblock_box.set("0")
        self.codeblock_box.pack(side="left", padx=4)
        group = self.groups["data"]
        ttk.Label(group, text="Data").pack(side="left")
        self.data_entry = ttk.Entry(group, width=40)
        self.data_entry.pack(side="left", padx=4)
        self.data_entry.bind("<KeyRelease>", lambda e: self.update_length_label())
        self.format_box = ttk.Combobox(group, state="readonly", width=7, values=("Hex", "ASCII"))
        self.format_box.set("Hex")
        self.format_box.pack(side="left")
        self.format_box.bind("<<ComboboxSelected>>", lambda e: self.update_length_label())
        group = self.groups["directive"]
        ttk.Label(group, text="Directive").pack(side="left")
        self._directive_ops = {f"{op} {name}": op for op, name in DIRECTIVES.items()}
        self.directive_box = ttk.Combobox(group, state="readonly", width=30,
                                          values=list(self._directive_ops))
        self.directive_box.current(0)
        self.directive_box.pack(side="left", padx=4)
        ttk.Label(group, text="Parameter").pack(side="left")
        self.param_entry = ttk.Entry(group, width=12)
        self.param_entry.pack(side="left", padx=4)
        set_entry(self.param_entry, "0")
        group = self.groups["subscription"]
        ttk.Label(group, text="Flow (0 to 6)").pack(side="left")
        self.flow_entry = ttk.Entry(group, width=5)
        self.flow_entry.pack(side="left", padx=4)
        set_entry(self.flow_entry, "0")
        ttk.Label(group, text="Authorization (0 to 2)").pack(side="left", padx=(8, 0))
        self.auth_entry = ttk.Entry(group, width=5)
        self.auth_entry.pack(side="left", padx=4)
        set_entry(self.auth_entry, "0")
        self.length_label = tk.Label(self.groups["length"], text="", anchor="w")
        self.length_label.pack(side="left")
        self._length_colour = self.length_label.cget("fg")

        send_row = ttk.Frame(panel)
        send_row.pack(fill="x", pady=(2, 0))
        self.validate_check = make_checkbutton(send_row, "Validate limits")
        self.validate_check.pack(side="left")
        set_checkbutton(self.validate_check, True)
        self.send_button = ttk.Button(send_row, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=10)
        self.validate_note = ttk.Label(panel, text="Off sends out-of-range data to the server on purpose.",
                                       anchor="w")
        self.validate_note.pack(fill="x")
        self.message = tk.Label(panel, text="", fg=RED, anchor="w", justify="left")
        self.message.pack(fill="x")

        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings", height=5)
        for column, text, width in COLUMNS:
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, anchor="w")
        self.tree_scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.tree_scroll.set)
        self.tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self._unsubscribe_store = self.store.subscribe(lambda opts: self.update_length_label())
        self._on_operation_changed()

    # ------------------------------------------------------------ operation and fields
    _VISIBLE = {
        OP_BD: ("vcid", "data", "length"), OP_AD: ("vcid", "data", "length"),
        OP_CLTU: ("vcid", "data", "length"), OP_FRAME: ("codeblock", "data", "length"),
        OP_DIRECTIVE: ("vcid", "directive"), OP_NOP: ("vcid",), OP_SUBSCRIBE: ("vcid", "subscription"),
    }

    def set_operation(self, name: str) -> None:
        """Select an operation (also used by tests) and show its fields."""
        if name not in OPERATIONS:
            raise ValueError(f"unknown operation {name!r}")
        self.operation_box.set(name)
        self._on_operation_changed()

    def _on_operation_changed(self) -> None:
        visible = self._VISIBLE[self.operation_box.get()]
        for name, group in self.groups.items():
            if name in visible:
                group.grid()
            else:
                group.grid_remove()
        self.update_length_label()

    def visible_groups(self) -> "set[str]":
        """Names of the field groups currently managed by the grid (for tests)."""
        return {name for name, group in self.groups.items() if group.grid_info()}

    def allowed_range(self, operation: str) -> "tuple[int, int]":
        """Smallest and largest data length in bytes."""
        if operation in (OP_BD, OP_AD):
            return 1, SEGMENT_MAX_FEC if self.store.get().fec_in_frame else SEGMENT_MAX
        if operation == OP_CLTU:
            return 1, CLTU_MAX
        return FRAME_MIN, FRAME_MAX

    def _read_data(self) -> bytes:
        text = self.data_entry.get()
        return parse_ascii_bytes(text) if self.format_box.get() == "ASCII" else parse_hex_bytes(text)

    def update_length_label(self) -> None:
        """Live "N bytes (allowed a to b)" text, red when outside the allowed range."""
        operation = self.operation_box.get()
        if operation not in DATA_OPS:
            self.length_label.configure(text="")
            return
        low, high = self.allowed_range(operation)
        try:
            count = len(self._read_data()) if self.data_entry.get().strip() else 0
            ok = low <= count <= high
            text = f"{count} bytes (allowed {low} to {high})"
        except ValueError as exc:
            ok, text = False, f"{exc} (allowed {low} to {high} bytes)"
        self.length_label.configure(text=text, fg=self._length_colour if ok else RED)

    def length_text(self) -> str:
        return self.length_label.cget("text")

    # ------------------------------------------------------------ sending
    def _say(self, text: str, error: bool = True) -> None:
        self.message.configure(text=text, fg=RED if error else GREEN)

    def message_text(self) -> str:
        return self.message.cget("text")

    def _read_vcid(self, validate: bool) -> int:
        vcid = _uint(self.vcid_entry.get(), "VCID", _U32_MAX)
        if validate and vcid > VCID_MAX:
            raise ValueError(f"VCID must be 0 to {VCID_MAX} (switch off Validate limits to send it anyway)")
        return vcid

    def _read_data_checked(self, operation: str, validate: bool) -> bytes:
        data = self._read_data()
        low, high = self.allowed_range(operation)
        if validate and not low <= len(data) <= high:
            raise ValueError(f"Data length {len(data)} is outside the allowed {low} to {high} bytes "
                             f"(switch off Validate limits to send it anyway)")
        return data

    def send(self) -> None:
        """Build the request for the chosen operation, send it and add a table row."""
        operation = self.operation_box.get()
        validate = checkbutton_selected(self.validate_check)
        try:
            tag, vcid_text, size_text, description = self._send_operation(operation, validate)
        except (ValueError, ClientError, ProtocolError) as exc:
            self._say(f"Not sent: {exc}")
            return
        self._add_row(tag, description, vcid_text, size_text, operation)
        self._say(f"Sent {description}, tag {tag}", error=False)

    def _send_operation(self, operation: str, validate: bool):
        """Returns (tag, vcid text, bytes text, description). Raises on any problem."""
        client = self.client
        if operation in (OP_BD, OP_AD, OP_CLTU):
            vcid = self._read_vcid(validate)
            data = self._read_data_checked(operation, validate)
            send = {OP_BD: client.send_bd, OP_AD: client.send_ad, OP_CLTU: client.send_cltu}[operation]
            return send(vcid, data, validate), str(vcid), str(len(data)), operation
        if operation == OP_FRAME:
            codeblock = _uint(self.codeblock_box.get(), "Codeblock length", 8)
            data = self._read_data_checked(operation, validate)
            tag = client.send_frame(codeblock, data, validate)
            return tag, "-", str(len(data)), f"{operation} (codeblock {codeblock})"
        vcid = self._read_vcid(True)  # these builders always check the range
        if operation == OP_NOP:
            return client.send_nop(vcid), str(vcid), "-", operation
        if operation == OP_DIRECTIVE:
            op = self._directive_ops[self.directive_box.get()]
            param = _uint(self.param_entry.get(), "Directive parameter", _U32_MAX, hex_ok=True)
            tag = client.send_directive(op, vcid, param)
            return tag, str(vcid), "-", f"Directive {op} ({DIRECTIVES[op]})"
        flow = _uint(self.flow_entry.get(), "Flow", FLOW_MAX)
        authorization = _uint(self.auth_entry.get(), "Authorization", AUTHORIZATION_MAX)
        tag = client.subscribe(vcid, flow, authorization)
        return tag, str(vcid), "-", f"{operation} (flow {flow}, authorization {authorization})"

    # ------------------------------------------------------------ table
    def _add_row(self, tag: int, description: str, vcid_text: str, size_text: str,
                 operation: str = OP_BD) -> None:
        iid = f"t{tag}"
        if tag in self.requests:  # a wrapped tag: drop the stale row first
            self._drop(tag)
        self.tree.insert("", "end", iid=iid, values=(
            time.strftime("%H:%M:%S"), tag, description, vcid_text, size_text, PENDING))
        self.requests[tag] = {"iid": iid, "status": PENDING, "replies": 0, "timed_out": False,
                              "due": self.reply_deadline() if operation in EXPECTS_REPLY else None}
        while len(self.requests) > MAX_ROWS:
            self._drop(next(iter(self.requests)))  # the oldest
        self.tree.see(iid)

    def _drop(self, tag: int) -> None:
        row = self.requests.pop(tag)
        if self.tree.exists(row["iid"]):
            self.tree.delete(row["iid"])

    def _show_status(self, tag: int) -> None:
        row = self.requests[tag]
        text = row["status"] if row["replies"] <= 1 else f"{row['status']} ({row['replies']})"
        self.tree.set(row["iid"], "status", text)

    def _reply(self, tag: int, status: str) -> None:
        """Record one more reply for a request and show the latest status with the count."""
        row = self.requests[tag]
        row["status"] = status
        row["replies"] += 1
        self._show_status(tag)

    def rows(self) -> "list[tuple]":
        """All table rows as tuples of strings, oldest first (for tests)."""
        return [tuple(str(v) for v in self.tree.item(i, "values")) for i in self.tree.get_children()]

    def cell(self, tag: int, column: str) -> str:
        return str(self.tree.set(self.requests[tag]["iid"], column))

    def status_of(self, tag: int) -> str:
        return self.cell(tag, "status")

    # ------------------------------------------------------------ replies
    def _oldest_pending(self) -> Optional[int]:
        """Oldest request still waiting for its reply; a timed-out one is still waiting."""
        for tag, row in self.requests.items():
            if row["status"] in (PENDING, STATUS_TIMEOUT):
                return tag
        return None

    def log_received(self, event: Received) -> None:
        """Nothing here: on_received writes the single entry (short line and field dump in one)."""

    def on_received(self, event: Received) -> None:
        parsed = event.parsed
        message = getattr(event, "message", None)  # tests pass minimal stand-ins without one
        size = f" ({message.size} bytes)" if message is not None else ""
        self.log.add("received", f"RECEIVED {describe_reply(parsed)}{size}", getattr(message, "raw", None))
        if isinstance(parsed, AcceptReject):
            self._reply_for_tag(parsed.tag, STATUS_ACCEPTED if parsed.accepted else STATUS_REJECTED)
        elif isinstance(parsed, ConfirmResponse):
            self._reply_for_tag(parsed.tag, STATUS_CONFIRM_POS if parsed.positive else STATUS_CONFIRM_NEG)
        elif isinstance(parsed, (SegmentEcho, CltuEcho)):
            self._reply_for_tag(parsed.tag, STATUS_ECHO)
        elif isinstance(parsed, (AlertIndication, SuspendIndication, AdditionalInfo)):
            tag = getattr(parsed, "tag", None)  # none of the real ones carry a tag
            if tag is not None:
                status = STATUS_ALERT if isinstance(parsed, AlertIndication) else (
                    STATUS_SUSPENDED if isinstance(parsed, SuspendIndication) else None)
                if status is not None:
                    self._reply_for_tag(tag, status)
        elif isinstance(parsed, (NegativeResponse, UnknownResponse)):
            self._reply_without_tag(isinstance(parsed, NegativeResponse))

    def _reply_for_tag(self, tag: int, status: str) -> None:
        if tag not in self.requests:
            self.log_info(f"tag {tag} not sent by this tab")
            return
        self._reply(tag, status)

    def _reply_without_tag(self, negative: bool) -> None:
        """Negative and unrecognised replies have no tag: use the oldest pending request."""
        tag = self._oldest_pending()
        if tag is None:
            self.log_info("negative reply with no pending request" if negative
                          else "unrecognised reply with no pending request")
            return
        self._reply(tag, STATUS_NEGATIVE if negative else STATUS_UNRECOGNISED)
        self.log_info(f"the {'negative' if negative else 'unrecognised'} reply has no tag, so the oldest "
                      f"pending request (tag {tag}) was marked {self.requests[tag]['status']}")

    def on_state_changed(self, state: ConnState) -> None:
        """No reply will ever come for a request that is still pending when the link is gone."""
        if state in (ConnState.DISCONNECTED, ConnState.ERROR):
            for tag, row in self.requests.items():
                if row["status"] in (PENDING, STATUS_TIMEOUT):
                    row["status"] = STATUS_NO_RESPONSE
                    self._show_status(tag)

    def on_tick(self) -> None:
        """A request still pending after its timeout shows "no response (timeout)", once.

        A display state only: the row stays outstanding, so a late reply is still processed and
        replaces the text (the reply count then shows 1 for it).
        """
        current = guibase.now()
        for tag, row in list(self.requests.items()):
            if row["status"] != PENDING or row["timed_out"] or row["due"] is None or current < row["due"][0]:
                continue
            row["timed_out"] = True
            row["status"] = STATUS_TIMEOUT
            self._show_status(tag)
            self.log_error(f"Telecommand request tag {tag} got no reply within {row['due'][1]:g} s.")

    def on_destroy(self) -> None:
        if self._unsubscribe_store is not None:
            self._unsubscribe_store()
            self._unsubscribe_store = None
