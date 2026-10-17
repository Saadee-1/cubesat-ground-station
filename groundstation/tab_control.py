"""Control tab: stage configuration parameters 0 to 39 and send them.

The server answers with an acknowledgement or the negative
response. Status codes are shown as raw numbers; no meaning
is invented for them.

The negative response and unrecognised replies carry no tag. This tab assumes
replies come back in the order the requests were sent, so such a reply is
attributed to the OLDEST outstanding request.
"""
import re
import tkinter as tk
from collections import OrderedDict
from tkinter import ttk
from typing import Optional

from .cop1 import (CONFIG_PARAM_NAMES, ControlAck, NegativeResponse, UnknownResponse,
                   encode_ip, encode_tm_port)
from . import guibase
from .guibase import GREEN, RED, ServiceTab, parse_ascii_uint, set_entry
from .net import ClientError, ConnState, Received
from .protocol import ProtocolError

MAX_TRACKED_TAGS = 200
PARAM_IDS = range(40)  # parameter ids 0..39
_U32_MAX = 0xFFFFFFFF
_NEGATIVE_MESSAGE = "Negative values are not accepted: parameters are unsigned 32-bit words"
_IPV4_LIKE_RE = re.compile(r"[0-9]+(\.[0-9]+)*")   # ASCII digits and dots only
_IPV4_RE = re.compile(r"[0-9]+(\.[0-9]+){3}")
_NEGATIVE_RE = re.compile(r"-[0-9]+")
_REJECTED = "rejected (-1)"
_UNRECOGNISED = "unrecognised reply"
_NO_RESPONSE = "no response (disconnected)"
_TIMEOUT = "no response (timeout)"
_NEGATIVE_NOTE = ("the negative reply has no tag, so the oldest outstanding request "
                  "was marked rejected")
_UNKNOWN_NOTE = ("the unrecognised reply has no tag, so the oldest outstanding request "
                 "was marked unrecognised")
_NO_OUTSTANDING = "negative reply with no outstanding request"


def parse_ip(text: str) -> int:
    """Dotted IPv4 (ASCII digits only) to its 32-bit word, or ValueError."""
    text = text.strip()
    if not _IPV4_RE.fullmatch(text):  # also keeps non-ASCII digits out of encode_ip
        raise ValueError(f"bad IPv4 address {text!r}")
    try:
        return encode_ip(text)
    except ProtocolError as exc:
        raise ValueError(str(exc)) from None


def parse_tmu_port(text: str) -> int:
    """'TMU:port' such as '1:3070' to its 32-bit word, or ValueError."""
    parts = text.strip().split(":")
    try:
        if len(parts) != 2:
            raise ValueError
        tmu, port = parse_ascii_uint(parts[0]), parse_ascii_uint(parts[1])
    except ValueError:
        raise ValueError("TMU:port must look like 1:3070") from None
    try:
        return encode_tm_port(tmu, port)
    except ProtocolError as exc:
        raise ValueError(f"TMU:port out of range: {exc}") from None


def parse_control_value(text: str) -> int:
    """Decimal, 0x hex, dotted IPv4 or TMU:port to an unsigned 32-bit word, or ValueError."""
    text = text.strip()
    if not text:
        raise ValueError("Value is empty")
    if ":" in text:
        return parse_tmu_port(text)
    if text.lower().startswith("0x"):
        try:
            value = parse_ascii_uint(text, hex_ok=True)
        except ValueError:
            raise ValueError(f"Not a hex number: {text!r}") from None
    elif _NEGATIVE_RE.fullmatch(text):
        raise ValueError(_NEGATIVE_MESSAGE)
    elif text.isascii() and text.isdigit():
        value = parse_ascii_uint(text)
    elif _IPV4_LIKE_RE.fullmatch(text):
        return parse_ip(text)
    else:
        raise ValueError(f"Cannot read {text!r}: use decimal, 0x hex, a.b.c.d or TMU:port")
    if not 0 <= value <= _U32_MAX:
        raise ValueError("Value out of range: must be 0 to 0xFFFFFFFF")
    return value


def _param_name(pid: int) -> str:
    return CONFIG_PARAM_NAMES.get(pid, f"param {pid}")


def _word_text(word: int) -> str:
    return f"{word} (0x{word:08X})"


class ControlTab(ServiceTab):
    """Stage values per parameter id and send only the staged ones."""

    def __init__(self, master, client, store, **kwargs) -> None:
        self.staged: "dict[int, int]" = {}
        self.sent: "dict[int, str]" = {}  # id -> "pending", "ack <status>", "rejected (-1)", ...
        self.pending_tags: "OrderedDict[int, list[int]]" = OrderedDict()  # tag -> ids, oldest first
        self.pending_due: "dict[int, tuple[float, float]]" = {}  # tag -> (due time, timeout s) at send
        self.timed_out_tags: "set[int]" = set()  # tags already shown as timed out (still outstanding)
        super().__init__(master, client, store, "Control", **kwargs)

    # ------------------------------------------------------------ widgets
    def build_body(self, parent: tk.Misc) -> None:
        columns = ("id", "name", "staged", "sent")
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=8, selectmode="browse")
        for column, text, width in (("id", "Id", 40), ("name", "Name", 280),
                                    ("staged", "Staged", 190), ("sent", "Sent", 200)):
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for pid in PARAM_IDS:
            self.tree.insert("", "end", iid=str(pid), values=(pid, _param_name(pid), "", ""))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_selection())

        # Three short rows instead of one long one, so nothing is clipped on a narrow window.
        self.selected_label = ttk.Label(parent, text="Selected: none", anchor="w")
        self.selected_label.pack(fill="x", pady=(4, 0))
        value_row = ttk.Frame(parent)
        value_row.pack(fill="x", pady=2)
        ttk.Label(value_row, text="Value").pack(side="left")
        self.value_entry = ttk.Entry(value_row, width=24)
        self.value_entry.pack(side="left", padx=4)
        self.stage_button = ttk.Button(value_row, text="Stage", command=self.stage)
        self.stage_button.pack(side="left")
        self.unstage_button = ttk.Button(value_row, text="Unstage", command=self.unstage)
        self.unstage_button.pack(side="left", padx=2)
        buttons = ttk.Frame(parent)
        buttons.pack(fill="x")
        self.send_button = ttk.Button(buttons, text="Send staged", command=self.send_staged)
        self.send_button.pack(side="left")
        self.clear_button = ttk.Button(buttons, text="Clear all staged", command=self.clear_staged)
        self.clear_button.pack(side="left", padx=4)
        self.ip_button = ttk.Button(buttons, text="IP -> word", command=self.convert_ip)
        self.ip_button.pack(side="left", padx=(16, 2))
        self.tmu_button = ttk.Button(buttons, text="TMU:port -> word", command=self.convert_tmu_port)
        self.tmu_button.pack(side="left")
        self.edit_message = tk.Label(parent, text="", fg=RED, anchor="w", justify="left")
        self.edit_message.pack(fill="x")

    # ------------------------------------------------------------ helpers
    def _say(self, text: str, error: bool = True) -> None:
        self.edit_message.configure(text=text, fg=RED if error else GREEN)

    def message_text(self) -> str:
        return self.edit_message.cget("text")

    def selected_id(self) -> Optional[int]:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def select(self, pid: int) -> None:
        """Select a parameter row (also used by tests)."""
        self.tree.selection_set(str(pid))
        self.tree.focus(str(pid))
        self.tree.see(str(pid))
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        pid = self.selected_id()
        text = "Selected: none" if pid is None else f"Selected: {pid}  {_param_name(pid)}"
        self.selected_label.configure(text=text)
        # The Value box belongs to the selected row: its staged value, or empty. It must not carry
        # the previous row's text over.
        set_entry(self.value_entry, "" if pid is None or pid not in self.staged
                  else f"0x{self.staged[pid]:08X}")

    def _refresh_row(self, pid: int) -> None:
        staged = _word_text(self.staged[pid]) if pid in self.staged else ""
        self.tree.item(str(pid), values=(pid, _param_name(pid), staged, self.sent.get(pid, "")))

    def cell(self, pid: int, column: str) -> str:
        """Text of one table cell (for tests): column is id, name, staged or sent."""
        return str(self.tree.set(str(pid), column))

    # ------------------------------------------------------------ editing
    def stage(self) -> None:
        pid = self.selected_id()
        if pid is None:
            self._say("Select a parameter first")
            return
        try:
            word = parse_control_value(self.value_entry.get())
        except ValueError as exc:
            self._say(str(exc))
            return
        self.staged[pid] = word
        self._refresh_row(pid)
        self._say(f"Staged {pid} = {_word_text(word)}", error=False)

    def unstage(self) -> None:
        pid = self.selected_id()
        if pid is None:
            self._say("Select a parameter first")
            return
        if self.staged.pop(pid, None) is None:
            self._say(f"Parameter {pid} is not staged")
            return
        self._refresh_row(pid)
        self._say(f"Unstaged {pid}", error=False)

    def clear_staged(self) -> None:
        ids = list(self.staged)
        self.staged.clear()
        for pid in ids:
            self._refresh_row(pid)
        self._say("Cleared all staged values", error=False)

    def convert_ip(self) -> None:
        """Fill the value entry with the 0x form of the IPv4 address typed there."""
        try:
            word = parse_ip(self.value_entry.get())
        except ValueError as exc:
            self._say(str(exc))
            return
        set_entry(self.value_entry, f"0x{word:08X}")
        self._say(f"IP converted: {_word_text(word)}", error=False)

    def convert_tmu_port(self) -> None:
        """Fill the value entry with the 0x form of the TMU:port typed there."""
        try:
            word = parse_tmu_port(self.value_entry.get())
        except ValueError as exc:
            self._say(str(exc))
            return
        set_entry(self.value_entry, f"0x{word:08X}")
        self._say(f"TMU:port converted: {_word_text(word)}", error=False)

    # ------------------------------------------------------------ sending
    def send_staged(self) -> None:
        """Send ONLY the staged ids; on failure keep the staged values."""
        if not self.staged:
            self._say("Nothing staged")
            return
        ids = sorted(self.staged)
        params = {pid: self.staged[pid] for pid in ids}
        try:
            tag = self.client.set_parameters(params)
        except (ClientError, ProtocolError) as exc:
            self._say(f"Send failed: {exc}")
            return
        # Bookkeeping right after send returns. Both run on the GUI thread, and the
        # pump runs there too, so the response cannot be handled before this.
        self._remember_tag(tag, ids)
        self.pending_due[tag] = self.reply_deadline()
        for pid in ids:
            self.sent[pid] = "pending"
            del self.staged[pid]
            self._refresh_row(pid)
        self._say(f"Sent {len(ids)} parameter(s), tag {tag}", error=False)

    def _remember_tag(self, tag: int, ids: "list[int]") -> None:
        self.pending_tags[tag] = ids
        self.timed_out_tags.discard(tag)  # a reused (wrapped) tag is a fresh request
        while len(self.pending_tags) > MAX_TRACKED_TAGS:
            self._forget(next(iter(self.pending_tags)))  # drop the oldest

    def _forget(self, tag: int) -> "Optional[list[int]]":
        """Stop tracking a tag (answered, dropped or the link is gone); returns its ids."""
        self.pending_due.pop(tag, None)
        self.timed_out_tags.discard(tag)
        return self.pending_tags.pop(tag, None)

    def _mark(self, ids: "list[int]", text: str) -> None:
        for pid in ids:
            self.sent[pid] = text
            self._refresh_row(pid)

    # ------------------------------------------------------------ responses
    def on_received(self, event: Received) -> None:
        parsed = event.parsed
        if isinstance(parsed, ControlAck):
            ids = self._forget(parsed.tag)  # a late reply after a timeout lands here too
            if ids is None:
                self.log_info(f"tag {parsed.tag} not sent by this tab")
                return
            self._mark(ids, f"ack {parsed.status}")
            self.log_info(f"control request tag {parsed.tag} acknowledged, status {parsed.status}, "
                          f"ids {ids}")
        elif isinstance(parsed, (NegativeResponse, UnknownResponse)):
            self._reject_oldest(rejected=isinstance(parsed, NegativeResponse))

    def _reject_oldest(self, rejected: bool) -> None:
        """A tagless failure reply belongs to the oldest outstanding request (replies are in order)."""
        if not self.pending_tags:
            self.log_info(_NO_OUTSTANDING)
            return
        tag = next(iter(self.pending_tags))  # a timed-out request is still outstanding
        ids = self._forget(tag)
        self._mark(ids, _REJECTED if rejected else _UNRECOGNISED)
        self.log_info(f"tag {tag}: " + (_NEGATIVE_NOTE if rejected else _UNKNOWN_NOTE))

    def on_state_changed(self, state: ConnState) -> None:
        """No reply will ever come for requests still outstanding when the link is gone."""
        if state in (ConnState.DISCONNECTED, ConnState.ERROR) and self.pending_tags:
            for ids in self.pending_tags.values():
                self._mark(ids, _NO_RESPONSE)
            self.pending_tags.clear()
            self.pending_due.clear()
            self.timed_out_tags.clear()

    def on_tick(self) -> None:
        """A request still unanswered after its timeout shows "no response (timeout)", once.

        This is a display state only: the tag stays outstanding, so a late reply is still
        processed and replaces the text.
        """
        current = guibase.now()
        for tag, (due, timeout) in list(self.pending_due.items()):
            if tag in self.timed_out_tags or current < due or tag not in self.pending_tags:
                continue
            self.timed_out_tags.add(tag)
            ids = self.pending_tags[tag]
            order = list(self.pending_tags)  # insertion order = send order (tag numbers can wrap)
            newer = {pid for later in order[order.index(tag) + 1:] for pid in self.pending_tags[later]}
            self._mark([pid for pid in ids if pid not in newer], _TIMEOUT)
            self.log_error(f"Control request tag {tag} got no reply within {timeout:g} s.")
