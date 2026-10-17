"""Shared GUI pieces for the service tabs: options store, dialog, event log, tab base.

Rules followed here:
- Tk is only touched from the GUI thread. Network events arrive through
  client.drain() called from an after() pump.
- No Tk Variable objects (StringVar and friends). Checkbuttons use a plain
  Tcl variable NAME (a str), Entries are read with get/insert/delete.
- Every repeating after() callback reschedules in a finally block and is
  cancelled when its widget is destroyed.
"""
import dataclasses
import itertools
import logging
import re
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .cop1 import NegativeResponse, UnknownResponse
from .net import (ClientError, ConnState, ParseFailure, Received, Sent,
                  StateChanged, StreamProblem, describe_words)
from .protocol import ProtocolOptions

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 1000
HINT = "Check: flow ID (Protocol options), message size, checksum mode."
ASSUMPTION_NOTE = ("Assumptions pending confirmation against the real server: "
                   "checksum coverage, byte numbering, flow IDs.")
GREEN, RED, AMBER, GREY, BLUE = "#2e7d32", "#c62828", "#b26a00", "#666666", "#1565c0"
_STATE_COLOURS = {ConnState.DISCONNECTED: GREY, ConnState.CONNECTING: AMBER,
                  ConnState.CONNECTED: GREEN, ConnState.ERROR: RED}
_FLOW_FIELDS = (("control_flow_id", "Control flow ID"),
                ("monitoring_flow_id", "Monitoring flow ID"),
                ("telecommand_flow_id", "Telecommand flow ID"),
                ("telemetry_flow_id", "Telemetry flow ID"))
DEFAULT_REPLY_TIMEOUT_S, MIN_REPLY_TIMEOUT_S, MAX_REPLY_TIMEOUT_S = 5.0, 0.5, 300.0
_REPLY_TIMEOUT_MESSAGE = (f"Reply timeout must be a number of seconds from "
                          f"{MIN_REPLY_TIMEOUT_S:g} to {MAX_REPLY_TIMEOUT_S:g}")
_checkbutton_names = itertools.count(1)


def now() -> float:
    """The clock the reply timeouts use (seconds, monotonic). Tests replace this function."""
    return time.monotonic()


# ---------------------------------------------------------------- helpers
def make_checkbutton(parent: tk.Misc, text: str, command: Optional[Callable] = None) -> ttk.Checkbutton:
    """A ttk.Checkbutton bound to a plain Tcl variable name (no Python Variable object)."""
    name = f"gs_check_{next(_checkbutton_names)}"
    button = ttk.Checkbutton(parent, text=text, variable=name, command=command)
    button.setvar(name, 0)  # start unchecked
    button._gs_var = name  # remember the name for set/unset
    button.bind("<Destroy>", lambda e, b=button: _unset_check_var(e, b), add="+")
    return button


def _unset_check_var(event: tk.Event, button: ttk.Checkbutton) -> None:
    """Remove the Tcl variable when the checkbutton is destroyed."""
    if event.widget is button:
        try:
            button.tk.unsetvar(button._gs_var)
        except tk.TclError:
            pass


def checkbutton_selected(button: ttk.Checkbutton) -> bool:
    """True when the checkbutton is ticked."""
    return bool(button.instate(["selected"]))


def set_checkbutton(button: ttk.Checkbutton, on: bool) -> None:
    """Tick or untick without invoking the command."""
    button.setvar(button._gs_var, 1 if on else 0)


_ASCII_DECIMAL = re.compile(r"[0-9]+")
_ASCII_HEX = re.compile(r"0[xX][0-9A-Fa-f]+")


def parse_ascii_uint(text: str, *, hex_ok: bool = False) -> int:
    """Strict unsigned integer: ASCII digits only, optional 0x hex, or ValueError.

    Unlike int(), this rejects underscores ("8_0"), a leading plus, non-ASCII
    digits (Arabic-Indic, full-width) and exponent forms ("1e3").
    Surrounding whitespace is ignored.
    """
    text = text.strip()
    if _ASCII_DECIMAL.fullmatch(text):
        return int(text, 10)
    if hex_ok and _ASCII_HEX.fullmatch(text):
        return int(text, 16)
    raise ValueError(f"not an unsigned integer: {text!r}")


_ASCII_SECONDS = re.compile(r"[0-9]+(\.[0-9]+)?")


def parse_reply_timeout(text: str) -> float:
    """Reply timeout in seconds, 0.5 to 300, or ValueError.

    Strict like parse_ascii_uint: ASCII digits with an optional ".digits" part. Underscores,
    a leading plus or minus, exponents, "nan", "inf", ".5", "5." and non-ASCII digits are rejected.
    """
    text = text.strip()
    if not _ASCII_SECONDS.fullmatch(text):
        raise ValueError(_REPLY_TIMEOUT_MESSAGE)
    value = float(text)
    if not MIN_REPLY_TIMEOUT_S <= value <= MAX_REPLY_TIMEOUT_S:
        raise ValueError(_REPLY_TIMEOUT_MESSAGE)
    return value


def parse_port(text: str) -> int:
    """Port text to int, 1 to 65535, or ValueError with a user-readable reason."""
    try:
        port = parse_ascii_uint(text)
    except ValueError:
        raise ValueError("Port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port must be an integer from 1 to 65535")
    return port


def set_entry(entry: tk.Misc, text: str) -> None:
    """Replace an Entry/Spinbox text (no Tk Variable involved)."""
    entry.delete(0, "end")
    entry.insert(0, text)


def summarise(obj: object) -> str:
    """One short line describing a parsed response (no long tuples)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        parts = []
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if value is None or isinstance(value, (int, float, str, bool)):
                parts.append(f"{f.name}={value}")
            elif isinstance(value, (bytes, bytearray)):
                parts.append(f"{f.name}={len(value)} bytes")
            elif isinstance(value, (tuple, list)):
                parts.append(f"{f.name}=[{len(value)} items]")
        return f"{type(obj).__name__}({', '.join(parts)})"
    return repr(obj)[:200]


# ---------------------------------------------------------------- options store
class OptionsStore:
    """Holds the current ProtocolOptions and notifies subscribers (no Tk)."""

    def __init__(self, opts: Optional[ProtocolOptions] = None) -> None:
        self._opts = opts or ProtocolOptions()
        self._subscribers: "list[Callable[[ProtocolOptions], None]]" = []
        self.dialog = None  # the open ProtocolOptionsDialog, if any (at most one per store)
        self._reply_timeout = DEFAULT_REPLY_TIMEOUT_S

    @property
    def reply_timeout(self) -> float:
        """Seconds a request may stay unanswered before the tabs show "no response (timeout)"."""
        return self._reply_timeout

    def set_reply_timeout(self, seconds: float) -> None:
        """Change the reply timeout (applies to requests sent afterwards); ValueError when out of range."""
        if not MIN_REPLY_TIMEOUT_S <= seconds <= MAX_REPLY_TIMEOUT_S:  # also false for NaN
            raise ValueError(_REPLY_TIMEOUT_MESSAGE)
        self._reply_timeout = float(seconds)

    def get(self) -> ProtocolOptions:
        return self._opts

    def set(self, opts: ProtocolOptions) -> None:
        """Replace the options and call every subscriber on the calling thread."""
        self._opts = opts
        for callback in list(self._subscribers):
            try:
                callback(opts)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop the others
                logger.exception("options subscriber failed")

    def subscribe(self, callback: "Callable[[ProtocolOptions], None]") -> Callable[[], None]:
        """Register a callback; returns a function that unsubscribes it (safe to call twice)."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
        return unsubscribe


# ---------------------------------------------------------------- dialog
def _parse_flow_id(text: str) -> int:
    """Decimal or 0x hex text to an int in 0..0xFFFFFFFF, else ValueError."""
    try:
        value = parse_ascii_uint(text, hex_ok=True)
    except ValueError:
        raise ValueError("must be an integer from 0 to 0xFFFFFFFF") from None
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("must be an integer from 0 to 0xFFFFFFFF")
    return value


class ProtocolOptionsDialog(tk.Toplevel):
    """Edits the shared ProtocolOptions. Not modal: no grab, no wait_window."""

    def __init__(self, master: tk.Misc, store: OptionsStore) -> None:
        super().__init__(master)
        self.store = store
        self.title("Protocol options")
        store.dialog = self
        opts = store.get()
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        self.msb_check = make_checkbutton(
            body, "Byte numbering: CORTEX byte 0 of the frame check word is the most significant byte")
        self.msb_check.pack(anchor="w")
        set_checkbutton(self.msb_check, opts.cortex_byte0_is_msb)
        row = ttk.Frame(body)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Checksum mode").pack(side="left")
        self.checksum_box = ttk.Combobox(
            row, state="readonly", width=22,
            values=("whole_packet", "excluding_postamble", "payload_only"))
        self.checksum_box.set(opts.checksum_mode)
        self.checksum_box.pack(side="left", padx=6)
        self.fec_check = make_checkbutton(body, "FEC in frame (shorter segment limit)")
        self.fec_check.pack(anchor="w")
        set_checkbutton(self.fec_check, opts.fec_in_frame)
        self.flow_entries: "dict[str, ttk.Entry]" = {}
        for field, label in _FLOW_FIELDS:
            row = ttk.Frame(body)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=22).pack(side="left")
            entry = ttk.Entry(row, width=14)
            entry.pack(side="left")
            set_entry(entry, str(getattr(opts, field)))
            self.flow_entries[field] = entry
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 1))
        ttk.Label(row, text="Reply timeout (s)", width=22).pack(side="left")
        self.timeout_entry = ttk.Entry(row, width=14)
        self.timeout_entry.pack(side="left")
        set_entry(self.timeout_entry, f"{store.reply_timeout:g}")
        ttk.Label(body, text=ASSUMPTION_NOTE, wraplength=420, foreground=GREY).pack(anchor="w", pady=6)
        self.message = tk.Label(body, text="", fg=RED, anchor="w", justify="left", wraplength=420)
        self.message.pack(fill="x")
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(6, 0))
        self.apply_button = ttk.Button(buttons, text="Apply", command=self.apply)
        self.apply_button.pack(side="left")
        self.close_button = ttk.Button(buttons, text="Close", command=self.destroy)
        self.close_button.pack(side="left", padx=6)
        self.bind("<Destroy>", self._on_destroy, add="+")

    def apply(self) -> bool:
        """Validate the widgets; on success update the store, else show the problem."""
        values = {}
        for field, label in _FLOW_FIELDS:
            try:
                values[field] = _parse_flow_id(self.flow_entries[field].get())
            except ValueError as exc:
                self._show(f"{label} {exc}", error=True)
                return False
        try:
            timeout = parse_reply_timeout(self.timeout_entry.get())
        except ValueError as exc:
            self._show(str(exc), error=True)
            return False
        try:
            new = dataclasses.replace(
                self.store.get(), cortex_byte0_is_msb=checkbutton_selected(self.msb_check),
                checksum_mode=self.checksum_box.get(), fec_in_frame=checkbutton_selected(self.fec_check),
                **values)
        except ValueError as exc:
            self._show(str(exc), error=True)
            return False
        self.store.set_reply_timeout(timeout)
        self.store.set(new)
        self._show("Applied.", error=False)
        return True

    def _show(self, text: str, error: bool) -> None:
        self.message.configure(text=text, fg=RED if error else GREEN)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self and self.store.dialog is self:
            self.store.dialog = None


# ---------------------------------------------------------------- event log
class EventLog(ttk.Frame):
    """Bounded read-only log with colour tags and an optional raw-word dump."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.raw_check = make_checkbutton(top, "Show raw words")
        self.raw_check.pack(side="left")
        self.clear_button = ttk.Button(top, text="Clear", command=self.clear)
        self.clear_button.pack(side="right")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, height=10, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("info", foreground=GREY)
        self.text.tag_configure("sent", foreground=BLUE)
        self.text.tag_configure("received", foreground=GREEN)
        self.text.tag_configure("error", foreground=RED)

    def raw_enabled(self) -> bool:
        return checkbutton_selected(self.raw_check)

    def add(self, kind: str, text: str, raw: Optional[bytes] = None) -> None:
        """Append a line (and, if enabled, one line per raw word); keep at most MAX_LOG_LINES."""
        if kind not in ("info", "sent", "received", "error"):
            raise ValueError(f"unknown log kind {kind!r}")
        lines = [f"{time.strftime('%H:%M:%S')} {text}"]
        if raw is not None and self.raw_enabled():
            lines += ["    " + line for line in describe_words(raw)]
        at_bottom = self.text.yview()[1] >= 0.999  # do not yank the view if the user scrolled up
        self.text.configure(state="normal")
        self.text.insert("end", "\n".join(lines) + "\n", kind)
        count = self.line_count()
        if count > MAX_LOG_LINES:
            self.text.delete("1.0", f"{count - MAX_LOG_LINES + 1}.0")  # drop the oldest lines
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def line_count(self) -> int:
        return int(self.text.index("end-1c").split(".")[0]) - 1

    def lines(self) -> "list[str]":
        return self.text.get("1.0", "end-1c").splitlines()

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# ---------------------------------------------------------------- tab base
class ServiceTab(ttk.Frame):
    """Connection bar, event pump, log and statistics shared by every service tab."""

    def __init__(self, master: tk.Misc, client, store: OptionsStore, title: str, *,
                 pump_ms: int = 50) -> None:
        super().__init__(master)
        self.client = client
        self.store = store
        self.title = title
        self.pump_ms = pump_ms
        self.sent_count = self.received_count = self.error_count = 0
        self._destroyed = False
        self._after_id: Optional[str] = None
        self._last_tick_error = ""

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Label(bar, text="Host").pack(side="left")
        self.host_entry = ttk.Entry(bar, width=18)
        self.host_entry.pack(side="left", padx=4)
        set_entry(self.host_entry, client.host)
        ttk.Label(bar, text="Port").pack(side="left")
        self.port_entry = ttk.Entry(bar, width=7)
        self.port_entry.pack(side="left", padx=4)
        set_entry(self.port_entry, str(client.port))
        self.connect_button = ttk.Button(bar, text="Connect", width=11, command=self.toggle_connection)
        self.connect_button.pack(side="left", padx=4)
        self.options_button = ttk.Button(bar, text="Protocol options...", command=self.open_options_dialog)
        self.options_button.pack(side="right")
        # The state text (and validation messages) get their own row so no button is pushed off screen.
        self.state_label = tk.Label(self, text=client.state.value, fg=_STATE_COLOURS[client.state],
                                    anchor="w")
        self.state_label.pack(fill="x", padx=8)

        self.body_frame = ttk.Frame(self)
        self.body_frame.pack(fill="both", expand=True, padx=6)
        self.build_body(self.body_frame)
        self.stats_label = ttk.Label(self, text="", anchor="w")
        self.stats_label.pack(fill="x", padx=6)
        self.log = EventLog(self)
        self.log.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self._push_options(store.get())
        self._unsubscribe = store.subscribe(self._push_options)
        self._update_stats()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._after_id = self.after(pump_ms, self._pump)

    # ------------------------------------------------------------ hooks
    def build_body(self, parent: tk.Misc) -> None:
        """Subclasses place their widgets in `parent` (below the bar, above the log)."""

    def on_received(self, event: Received) -> None:
        """Subclasses handle a received message here (default: nothing)."""

    def on_state_changed(self, state: ConnState) -> None:
        """Subclasses react to a connection state change here (default: nothing)."""

    def on_destroy(self) -> None:
        """Subclasses cancel their own timers here."""

    def on_tick(self) -> None:
        """Called on the GUI thread after every pump (about every 50 ms): check reply deadlines here."""

    def reply_deadline(self) -> "tuple[float, float]":
        """(due time, timeout in seconds) for a request sent now, using the current store setting."""
        timeout = self.store.reply_timeout
        return now() + timeout, timeout

    def log_received(self, event: Received) -> None:
        """Write the one "RECEIVED" log entry for a message (the raw words follow when enabled)."""
        self.log.add("received", f"RECEIVED {summarise(event.parsed)} ({event.message.size} bytes)",
                     event.message.raw)

    def is_error_response(self, parsed: object) -> bool:
        """True for responses that should be logged as errors."""
        return isinstance(parsed, (NegativeResponse, UnknownResponse))

    # ------------------------------------------------------------ options
    def _push_options(self, opts: ProtocolOptions) -> None:
        self.client.opts = opts

    def open_options_dialog(self) -> tk.Toplevel:
        """Open the shared dialog, or raise the one that is already open."""
        dialog = self.store.dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_set()
            return dialog
        return ProtocolOptionsDialog(self.winfo_toplevel(), self.store)

    # ------------------------------------------------------------ connection
    def show_state_message(self, text: str, error: bool = True) -> None:
        self.state_label.configure(text=text, fg=RED if error else GREY)

    def toggle_connection(self) -> None:
        """Connect when idle; otherwise disconnect (or cancel a connect in progress)."""
        if self.client.state in (ConnState.DISCONNECTED, ConnState.ERROR):
            host = self.host_entry.get().strip()
            if not host:
                self.show_state_message("Host must not be empty")
                return
            try:
                port = parse_port(self.port_entry.get())
            except ValueError as exc:
                self.show_state_message(str(exc))
                return
            try:
                self.client.configure(host, port)
                self.client.connect()
            except ClientError as exc:
                self.show_state_message(str(exc))
        else:
            self.client.disconnect()

    def shutdown(self) -> None:
        """Disconnect the client (the app calls this on window close)."""
        self.client.disconnect()

    # ------------------------------------------------------------ pump
    def _pump(self) -> None:
        """GUI-thread timer: drain the client's queue and dispatch each event."""
        self._after_id = None
        try:
            for event in self.client.drain(max_events=500):
                try:
                    self._dispatch(event)
                except Exception as exc:  # noqa: BLE001 - one bad event must not kill the pump
                    self.log_error(f"internal error while handling {type(event).__name__}: "
                                   f"{type(exc).__name__}: {exc}")
            try:
                self.on_tick()
            except Exception as exc:  # noqa: BLE001 - a failing deadline check must not kill the pump
                text = f"internal error in the timeout check: {type(exc).__name__}: {exc}"
                if text != self._last_tick_error:  # once per distinct problem, not every 50 ms
                    self._last_tick_error = text
                    self.log_error(text)
            self._update_stats()
        finally:
            if not self._destroyed:
                self._after_id = self.after(self.pump_ms, self._pump)

    def _dispatch(self, event: object) -> None:
        if isinstance(event, StateChanged):
            self._on_state(event)
        elif isinstance(event, Sent):
            self.sent_count += 1
            description = event.description or "message"
            self.log.add("sent", f"SENT {description} ({len(event.raw)} bytes)", event.raw)
        elif isinstance(event, Received):
            self.received_count += 1
            self.log_received(event)
            if self.is_error_response(event.parsed):
                self.log_error(f"Server answered with {type(event.parsed).__name__}.")
            self.on_received(event)
        elif isinstance(event, StreamProblem):
            self.log_error(f"Stream problem: {event.error.reason}, "
                           f"{event.error.discarded} byte(s) discarded.")
        elif isinstance(event, ParseFailure):
            self.log_error(f"Parse failure: {event.error}")

    def _on_state(self, event: StateChanged) -> None:
        state = event.state
        self.state_label.configure(text=state.value, fg=_STATE_COLOURS[state])
        self.connect_button.configure(text={ConnState.CONNECTED: "Disconnect",
                                            ConnState.CONNECTING: "Cancel"}.get(state, "Connect"))
        if state == ConnState.ERROR:
            self.log_error(f"{state.value}: {event.detail}")
        else:
            self.log.add("info", f"{state.value}: {event.detail}")
        self.on_state_changed(state)

    def log_error(self, text: str) -> None:
        """Count and log an error line, with the troubleshooting hint appended."""
        self.error_count += 1
        self.log.add("error", f"{text} {HINT}")

    def log_info(self, text: str) -> None:
        self.log.add("info", text)

    def _update_stats(self) -> None:
        text = (f"Messages sent: {self.sent_count} | received: {self.received_count} | "
                f"errors: {self.error_count} | dropped events: {self.client.dropped_events}")
        if self.stats_label.cget("text") != text:
            self.stats_label.configure(text=text)

    def stats_text(self) -> str:
        return self.stats_label.cget("text")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._destroyed = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self._unsubscribe()
        self.on_destroy()
