"""Telemetry tab: request CORTEX telemetry and feed the existing panel.

The embedded TelemetryPanel and TelemetryPipeline are unchanged. In Network mode this tab
forwards the frame of every telemetry message into the network frame
source, whose queue the pipeline already decodes. Negative acknowledgements,
stop acknowledgements and dummy messages are handled here too.

The time tags are shown as raw 32-bit words only: the epoch is not confirmed.
"""
import tkinter as tk
from tkinter import ttk
from typing import Optional

from .cop1 import NegativeResponse, UnknownResponse
from .cortex import (CHANNELS, DATA_FLOW_STOP, DATA_FLOW_OFFLINE_DUMMY, DATA_FLOW_OFFLINE_PERMANENT,
                     DATA_FLOW_OFFLINE_SINGLE, DATA_FLOW_REALTIME_DUMMY, DATA_FLOW_REALTIME_PERMANENT,
                     DATA_FLOW_REALTIME_SINGLE, DummyTelemetry, TelemetryMessage, TelemetryNegativeAck,
                     TelemetryStopAck, UnknownTmMessage, interpret_frame_check)
from .gui import TelemetryPanel
from . import guibase
from .guibase import (RED, GREEN, HINT, ServiceTab, checkbutton_selected, make_checkbutton,
                      parse_ascii_uint, set_entry)
from .net import ConnState, Received, Sent
from .protocol import ProtocolError, parse_message
from .netsource import MODE_NETWORK, MODES, make_switchable

SEQUENCE_MODULUS = 1 << 32
HALF_MODULUS = 1 << 31
BUFFERED_MAX = 1024           # buffered blocks 0..1024
U64_MAX = 2 ** 64 - 1         # frame mask and expected value
PANEL_TABLE_HEIGHT = 170      # pixels at 96 dpi: the table's requested height (scaled with Tk's scaling; it grows with the window)
LOG_LINES = 8                 # the log's requested height in text lines (it grows with the window; 8 is the
                              # least the shared test_app_gui log test accepts)
DATA_FLOWS = {
    DATA_FLOW_REALTIME_PERMANENT: "realtime permanent", DATA_FLOW_REALTIME_SINGLE: "realtime single",
    DATA_FLOW_REALTIME_DUMMY: "realtime dummy", DATA_FLOW_OFFLINE_PERMANENT: "offline permanent",
    DATA_FLOW_OFFLINE_SINGLE: "offline single", DATA_FLOW_OFFLINE_DUMMY: "offline dummy",
}  # the values a start request may use; 0x80 is the stop value, sent by the panel's Stop button
STATE_IDLE, STATE_REQUESTED, STATE_STREAMING = "idle", "requested", "streaming"
STATE_STOPPED, STATE_REFUSED, STATE_LINK_LOST = "stopped", "request refused", "link lost"


class SequenceTracker:
    """Counts gaps, missed messages and out-of-order sequence numbers (32-bit, wrapping)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the last sequence number AND zero the counters (a new stream)."""
        self.last: Optional[int] = None
        self.gaps = 0
        self.missed = 0
        self.out_of_order = 0

    def restart(self) -> None:
        """Forget only the last sequence number: the next message is "first", not a gap."""
        self.last = None

    def observe(self, sequence: int) -> str:
        """Returns "first", "ok", "gap" or "out_of_order" for this sequence number."""
        if self.last is None:
            self.last = sequence
            return "first"
        diff = (sequence - self.last) % SEQUENCE_MODULUS
        if diff == 1:
            self.last = sequence
            return "ok"
        if diff == 0 or diff > HALF_MODULUS:  # repeated, or backwards (a restarted stream)
            self.out_of_order += 1
            if diff != 0:
                self.last = sequence  # carry on from the new number instead of flagging everything after it
            return "out_of_order"
        self.gaps += 1
        self.missed += diff - 1
        self.last = sequence
        return "gap"


def parse_uint(text: str, what: str, limit: int) -> int:
    """Strict unsigned number (decimal or 0x hex) in 0..limit, or ValueError with a readable text."""
    stripped = text.strip()
    if stripped.startswith("-") and stripped[1:].isascii() and stripped[1:].isdigit():
        raise ValueError(f"{what} must not be negative")
    try:
        value = parse_ascii_uint(stripped, hex_ok=True)
    except ValueError:
        raise ValueError(f"{what} must be a decimal or 0x hex number") from None
    if value > limit:
        raise ValueError(f"{what} must be 0 to {limit if limit < 100000 else hex(limit)}")
    return value


class TelemetryTab(ServiceTab):
    """Request telemetry, forward frames to the panel and show stream statistics."""

    def __init__(self, master, client, store, pipeline, **kwargs) -> None:
        self.pipeline = pipeline
        self.switch = make_switchable(pipeline, client)  # before the panel binds Start and Stop
        self.network = self.switch.network
        self.tracker = SequenceTracker()
        self.stream_state = STATE_IDLE
        self.last_frame: Optional[bytes] = None
        self._seen_starts = 0
        self._seen_error = ""
        self._after_stop = False  # True from the Stop request in the event stream until the next Start
        self._request_due: Optional[tuple] = None   # (sent at, due time, timeout s) while waiting for a first frame
        self._no_frame_seconds: Optional[int] = None  # shown in the status block while no frame has come
        self._reset_counters()
        super().__init__(master, client, store, "Telemetry", **kwargs)
        self.log.text.configure(height=LOG_LINES)

    # ------------------------------------------------------------ counters
    def _reset_counters(self) -> None:
        self.counters = {
            "received": 0, "forwarded": 0, "dummy": 0, "gaps": 0, "missed": 0, "out_of_order": 0,
            "ignored_after_stop": 0, "check_ok": 0, "check_error": 0, "check_not_significant": 0, "length_inconsistent": 0,
            "last_sequence": None, "last_sync_status": None, "last_decoder_status": None,
            "last_bit_slip": None, "last_tm_delay": None, "last_time_tag_1": None, "last_time_tag_2": None,
        }

    def _reset_tracking(self) -> None:
        """New stream (Start or a source switch): sequence tracking and all counters start again."""
        self.tracker.reset()
        self._reset_counters()
        self._after_stop = False  # a new stream: the Start request itself also clears this in the event stream
        self._refresh_status()

    # ------------------------------------------------------------ widgets
    def build_body(self, parent: tk.Misc) -> None:
        request = ttk.Frame(parent)
        request.pack(fill="x")
        row = ttk.Frame(request)
        row.pack(fill="x")
        ttk.Label(row, text="Source").pack(side="left")
        self.source_box = ttk.Combobox(row, state="readonly", width=10, values=MODES)
        self.source_box.set(self.switch.mode)
        self.source_box.pack(side="left", padx=(4, 10))
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self._on_source_selected())
        ttk.Label(row, text="Channel").pack(side="left")
        self.channel_box = ttk.Combobox(row, state="readonly", width=3, values=list(CHANNELS))
        self.channel_box.set(CHANNELS[0])
        self.channel_box.pack(side="left", padx=(4, 10))
        self.channel_box.bind("<<ComboboxSelected>>", lambda e: self.apply_settings())
        ttk.Label(row, text="Buffered blocks").pack(side="left")
        self.blocks_entry = ttk.Entry(row, width=6)
        self.blocks_entry.pack(side="left", padx=4)
        set_entry(self.blocks_entry, "0")
        row = ttk.Frame(request)
        row.pack(fill="x")
        ttk.Label(row, text="Data flow").pack(side="left")
        self._flow_labels = {f"{value} {name}": value for value, name in DATA_FLOWS.items()}
        self.flow_box = ttk.Combobox(row, state="readonly", width=24, values=list(self._flow_labels))
        self.flow_box.current(0)
        self.flow_box.pack(side="left", padx=4)
        self.flow_box.bind("<<ComboboxSelected>>", lambda e: self.apply_settings())
        row = ttk.Frame(request)
        row.pack(fill="x")
        ttk.Label(row, text="Frame mask").pack(side="left")
        self.mask_entry = ttk.Entry(row, width=18)
        self.mask_entry.pack(side="left", padx=(4, 10))
        set_entry(self.mask_entry, "0")
        ttk.Label(row, text="Expected value").pack(side="left")
        self.expected_entry = ttk.Entry(row, width=18)
        self.expected_entry.pack(side="left", padx=4)
        set_entry(self.expected_entry, "0")
        for entry in (self.blocks_entry, self.mask_entry, self.expected_entry):
            entry.bind("<KeyRelease>", lambda e: self.apply_settings())
        row = ttk.Frame(request)
        row.pack(fill="x")
        self.forward_check = make_checkbutton(row, "Forward only frames whose check is ok")
        self.forward_check.pack(side="left")
        self.message = tk.Label(request, text="", fg=RED, anchor="w", justify="left")
        self.message.pack(fill="x")

        self.status_label = tk.Label(parent, text="", anchor="w", justify="left")
        self.status_label.pack(fill="x", pady=(1, 0))
        self.status_label.bind("<Configure>", lambda e: self.status_label.configure(
            wraplength=max(100, e.width - 6)))

        self.panel = TelemetryPanel(parent, self.pipeline)
        # A small default; it still grows. The log is sized in text lines, so the table must scale with
        # the font size too: Tk's scaling relative to 96 dpi (winfo_fpixels("1i") is dots per inch).
        dpi_factor = self.winfo_fpixels("1i") / 96.0
        self.panel._canvas.configure(height=round(PANEL_TABLE_HEIGHT * dpi_factor))
        self.panel.pack(fill="both", expand=True)
        self.network.prepare = self.apply_settings
        self._refresh_status()

    # ------------------------------------------------------------ request settings
    def _say(self, text: str, error: bool = True) -> None:
        self.message.configure(text=text, fg=RED if error else GREEN)

    def message_text(self) -> str:
        return self.message.cget("text")

    def apply_settings(self) -> bool:
        """Validate the request fields; push them into the network source only if all are valid."""
        try:
            channel = CHANNELS.index(self.channel_box.get())
            blocks = parse_uint(self.blocks_entry.get(), "Buffered blocks", BUFFERED_MAX)
            flow = self._flow_labels[self.flow_box.get()]
            mask = parse_uint(self.mask_entry.get(), "Frame mask", U64_MAX)
            expected = parse_uint(self.expected_entry.get(), "Expected value", U64_MAX)
        except ValueError as exc:
            self._say(str(exc))
            self.network.last_error = str(exc)
            self._seen_error = self.network.last_error  # already shown, do not repeat it
            return False
        net = self.network
        net.channel, net.buffered_blocks, net.data_flow = channel, blocks, flow
        net.frame_mask, net.expected_value = mask, expected
        self._say("")
        return True

    def _on_source_selected(self) -> None:
        self.switch.select(self.source_box.get())
        self._set_state(STATE_IDLE)
        self._reset_tracking()
        self._say("")

    def set_source_mode(self, mode: str) -> None:
        """Choose Simulated or Network (also used by tests)."""
        self.source_box.set(mode)
        self._on_source_selected()

    # ------------------------------------------------------------ stream state
    def _set_state(self, state: str) -> None:
        self.stream_state = state
        if state != STATE_REQUESTED:
            self._request_due = None  # nothing is awaited any more
            self._no_frame_seconds = None
        self._refresh_status()

    def _refresh_status(self) -> None:
        c = self.counters

        def word(value):
            return "-" if value is None else f"0x{value:08X}"

        def num(value):
            return "-" if value is None else str(value)

        waiting = ""
        if self._no_frame_seconds is not None and self.stream_state == STATE_REQUESTED:
            waiting = f" (no frame yet, {self._no_frame_seconds} s since the request)"
        text = (
            f"Source: {self.switch.mode} | stream: {self.stream_state}{waiting} | received {c['received']} | "
            f"forwarded {c['forwarded']} | dummy {c['dummy']} | last sequence {num(c['last_sequence'])}\n"
            f"Gaps {c['gaps']} (missed {c['missed']}) | out of order {c['out_of_order']} | "
            f"frame check ok {c['check_ok']} / error {c['check_error']} / "
            f"not significant {c['check_not_significant']} | length mismatch {c['length_inconsistent']} | "
            f"ignored after stop {c['ignored_after_stop']}\n"
            f"Last sync status {num(c['last_sync_status'])} | decoder status {num(c['last_decoder_status'])} | "
            f"bit slip {num(c['last_bit_slip'])} | TM delay {num(c['last_tm_delay'])} | "
            f"time tags (raw words) {word(c['last_time_tag_1'])} {word(c['last_time_tag_2'])}")
        if self.status_label.cget("text") != text:
            self.status_label.configure(text=text)

    def status_text(self) -> str:
        return self.status_label.cget("text")

    # ------------------------------------------------------------ events
    def is_error_response(self, parsed: object) -> bool:
        return isinstance(parsed, (NegativeResponse, UnknownResponse, UnknownTmMessage))

    def on_received(self, event: Received) -> None:
        parsed = event.parsed
        if isinstance(parsed, TelemetryMessage):
            self._on_message(parsed)
        elif isinstance(parsed, DummyTelemetry):
            self.counters["dummy"] += 1  # counted, never forwarded
            self._refresh_status()
        elif isinstance(parsed, TelemetryNegativeAck):
            self.log_error(f"Telemetry request refused, code {parsed.code}: {parsed.meaning}.")
            self.network.mark_stopped()
            self._say(f"Request refused: {parsed.meaning} (code {parsed.code})")
            self._set_state(STATE_REFUSED)
        elif isinstance(parsed, TelemetryStopAck):
            self.network.mark_stopped()
            self._set_state(STATE_STOPPED)

    def _dispatch(self, event: object) -> None:
        if isinstance(event, Sent):
            self._follow_requests(event)
        super()._dispatch(event)

    def _follow_requests(self, event: Sent) -> None:
        """Mark where in the event stream a Stop (or a new Start) request was sent.

        The Sent event of a request is queued in order with the frames that arrive, so every frame
        behind a Stop request in the queue really did arrive after the Stop was sent.
        """
        try:
            body = parse_message(event.raw).body
        except ProtocolError:
            return
        if len(body) > 2 and body[2] == DATA_FLOW_STOP:
            self._after_stop = True
        elif len(body) > 2 and body[2] in DATA_FLOWS:
            self._after_stop = False  # a new Start: frames count again, as before

    def _on_message(self, message: TelemetryMessage) -> None:
        c = self.counters
        if self._after_stop:
            self._ignore_after_stop(message)
            return
        c["received"] += 1
        self.tracker.observe(message.sequence)
        c["gaps"], c["missed"], c["out_of_order"] = (
            self.tracker.gaps, self.tracker.missed, self.tracker.out_of_order)
        c["last_sequence"] = message.sequence
        c["last_sync_status"], c["last_decoder_status"] = message.sync_status, message.decoder_status
        c["last_bit_slip"], c["last_tm_delay"] = message.bit_slip, message.tm_delay
        c["last_time_tag_1"], c["last_time_tag_2"] = message.time_tag_1, message.time_tag_2
        if not message.length_consistent:
            c["length_inconsistent"] += 1
        check = interpret_frame_check(message, self.store.get())  # the CURRENT shared options
        c["check_" + check.status] += 1
        if self.stream_state == STATE_REQUESTED and self.network.streaming:
            self._set_state(STATE_STREAMING)  # the first frame: the "no frame yet" note goes away
        if self.switch.mode == MODE_NETWORK and (
                not checkbutton_selected(self.forward_check) or check.status == "ok"):
            self.network.push_frame(message.frame)
            self.last_frame = message.frame
            c["forwarded"] += 1
        self._refresh_status()

    def _ignore_after_stop(self, message: TelemetryMessage) -> None:
        """A frame that arrives after Stop was sent never reaches the table; it is only counted.

        A few frames already in flight are normal. A count that keeps growing is the operator's
        evidence that the server ignored the Stop request.
        """
        c = self.counters
        c["ignored_after_stop"] += 1
        if c["ignored_after_stop"] == 1:
            self.log_info(f"A frame (sequence {message.sequence}) arrived after Stop was sent and was ignored. "
                          f"A few are normal; if the 'ignored after stop' count keeps growing the server is "
                          f"not honouring Stop.")
        self._refresh_status()

    def on_state_changed(self, state: ConnState) -> None:
        if state == ConnState.CONNECTED:
            self.tracker.restart()
        elif state in (ConnState.DISCONNECTED, ConnState.ERROR):
            self.network.mark_stopped()  # nothing is sent: the link is gone
            self.tracker.restart()  # the counters stay visible after the link is lost
            if self.stream_state in (STATE_REQUESTED, STATE_STREAMING):
                self._set_state(STATE_LINK_LOST)

    # ------------------------------------------------------------ pump and shutdown
    def _pump(self) -> None:
        """The base pump, plus a look at what the panel's Start and Stop buttons did."""
        try:
            self._poll_source()
        finally:
            super()._pump()

    def _poll_source(self) -> None:
        net = self.network
        if net.starts != self._seen_starts:  # the panel's Start button sent a request
            self._seen_starts = net.starts
            self._reset_tracking()
            self._set_state(STATE_REQUESTED)
            due, timeout = self.reply_deadline()
            self._request_due = (guibase.now(), due, timeout)
            self._say("")
        if net.last_error != self._seen_error:
            self._seen_error = net.last_error
            if net.last_error and self.switch.mode == MODE_NETWORK:
                self._say(net.last_error)
        if not net.streaming and self.stream_state in (STATE_REQUESTED, STATE_STREAMING):
            self._set_state(STATE_STOPPED)  # the panel's Stop button

    def on_tick(self) -> None:
        """Requested, but no frame and no refusal within the timeout: say so in the status block.

        Informational only (not an error, the counters are untouched). It is shown only until the
        first frame; there is no silence alarm while streaming, because the real frame rate is unknown.
        """
        if self._request_due is None:  # set only while the state is "requested" (see _set_state)
            return
        sent_at, due, timeout = self._request_due
        current = guibase.now()
        if current < due:
            return
        first = self._no_frame_seconds is None
        seconds = int(current - sent_at)
        if first or seconds != self._no_frame_seconds:
            self._no_frame_seconds = seconds
            self._refresh_status()
        if first:
            self.log_info(f"Telemetry request got no frame within {timeout:g} s. {HINT}")

    def shutdown(self) -> None:
        """Stop a running stream politely, then disconnect."""
        if self.network.streaming and self.client.state == ConnState.CONNECTED:
            self.network.stop()
        super().shutdown()

    def on_destroy(self) -> None:
        self.network.prepare = None
