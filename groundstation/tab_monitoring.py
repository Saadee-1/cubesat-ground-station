"""Monitoring tab: request and show the COP-1 monitoring message.

The report is the monitoring message with the configuration parameters and the status
table. The negative message is a short failure reply.
"""
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

from .cop1 import (CONFIG_PARAM_NAMES, STATUS_PARAM_NAMES, MonitoringReport, decode_ip,
                   decode_tm_port, NegativeResponse, UnknownResponse)
from . import guibase
from .guibase import (GREEN, RED, ServiceTab, checkbutton_selected, make_checkbutton,
                      parse_ascii_uint, set_checkbutton, set_entry)
from .net import ClientError, ConnState, Received
from .protocol import to_u32

STALE_TEXT = "stale (last request failed)"
TIMEOUT_TEXT = "stale (no report within {seconds:g} s)"
MIN_INTERVAL_S, MAX_INTERVAL_S, DEFAULT_INTERVAL_S = 1, 60, 2
# The names below come from cop1.CONFIG_PARAM_NAMES; the ids are looked up, not guessed.
_IP_NAME = "CORTEX_CRT IP address"
_PORT_NAME = "CORTEX_CRT port number/address"


def parse_interval(text: str) -> int:
    """Auto refresh interval in whole seconds, 1 to 60, or ValueError."""
    try:
        value = parse_ascii_uint(text)
    except ValueError:
        raise ValueError(f"Interval must be a whole number of seconds from "
                         f"{MIN_INTERVAL_S} to {MAX_INTERVAL_S}") from None
    if not MIN_INTERVAL_S <= value <= MAX_INTERVAL_S:
        raise ValueError(f"Interval must be a whole number of seconds from "
                         f"{MIN_INTERVAL_S} to {MAX_INTERVAL_S}")
    return value


class MonitoringTab(ServiceTab):
    """Shows only the most recent monitoring report."""

    def __init__(self, master, client, store, **kwargs) -> None:
        self.report: Optional[MonitoringReport] = None
        self.auto_active = False
        self._auto_id: Optional[str] = None
        self._waiting: "list[tuple[float, float]]" = []  # (due time, timeout s) of each unanswered request
        self._ip_ids = [pid for pid, name in CONFIG_PARAM_NAMES.items() if name == _IP_NAME]
        self._port_ids = [pid for pid, name in CONFIG_PARAM_NAMES.items() if name == _PORT_NAME]
        super().__init__(master, client, store, "Monitoring", **kwargs)

    # ------------------------------------------------------------ widgets
    def build_body(self, parent: tk.Misc) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=2)
        self.request_button = ttk.Button(bar, text="Request", command=self.request)
        self.request_button.pack(side="left")
        self.auto_check = make_checkbutton(bar, "Auto refresh every", command=self._on_auto_toggle)
        self.auto_check.pack(side="left", padx=(14, 2))
        self.interval_spin = ttk.Spinbox(bar, from_=MIN_INTERVAL_S, to=MAX_INTERVAL_S, width=4)
        self.interval_spin.pack(side="left")
        set_entry(self.interval_spin, str(DEFAULT_INTERVAL_S))
        ttk.Label(bar, text="s").pack(side="left", padx=2)
        # Messages get their own row so a long text never pushes a control off the window.
        self.message = tk.Label(parent, text="", fg=RED, anchor="w")
        self.message.pack(fill="x")

        self.header_label = ttk.Label(parent, text="No report yet", anchor="w")
        self.header_label.pack(fill="x")
        self.stale_label = tk.Label(parent, text="", fg=RED, anchor="w")
        self.stale_label.pack(fill="x")

        tables = ttk.Frame(parent)
        tables.pack(fill="both", expand=True)
        # Left: configuration (12 rows visible, scrollbar for the rest of the 29).
        left = ttk.Frame(tables)
        left.pack(side="left", fill="both", expand=True)
        self.config_tree = ttk.Treeview(left, columns=("name", "value", "decoded"),
                                        show="headings", height=12)
        for column, text, width in (("name", "Config parameter", 240), ("value", "Value", 100),
                                    ("decoded", "Decoded", 150)):
            self.config_tree.heading(column, text=text)
            self.config_tree.column(column, width=width, anchor="w", stretch=True)
        self.config_scroll = ttk.Scrollbar(left, orient="vertical", command=self.config_tree.yview)
        self.config_tree.configure(yscrollcommand=self.config_scroll.set)
        self.config_scroll.pack(side="right", fill="y")
        self.config_tree.pack(side="left", fill="both", expand=True)
        # Right: status table (wide name column) and the error flags.
        right = ttk.Frame(tables)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        status_frame = ttk.Frame(right)
        status_frame.pack(fill="both", expand=True)
        self.status_tree = ttk.Treeview(status_frame, columns=("name", "raw"), show="headings", height=4)
        for column, text, width in (("name", "Status parameter", 330), ("raw", "Raw value", 100)):
            self.status_tree.heading(column, text=text)
            self.status_tree.column(column, width=width, anchor="w", stretch=True)
        self.status_scroll = ttk.Scrollbar(status_frame, orient="vertical", command=self.status_tree.yview)
        self.status_tree.configure(yscrollcommand=self.status_scroll.set)
        self.status_scroll.pack(side="right", fill="y")
        self.status_tree.pack(side="left", fill="both", expand=True)
        self.flags_label = tk.Label(right, text="", anchor="w")
        self.flags_label.pack(fill="x")

    # ------------------------------------------------------------ requests
    def _say(self, text: str, error: bool = True) -> None:
        self.message.configure(text=text, fg=RED if error else GREEN)

    def message_text(self) -> str:
        return self.message.cget("text")

    def request(self) -> None:
        """Send one monitoring request; show a problem inline."""
        try:
            self.client.request()
        except ClientError as exc:
            self._say(f"Request failed: {exc}")
            self._mark_stale()
        else:
            self._waiting.append(self.reply_deadline())
            self._say("")

    def _read_interval(self) -> Optional[int]:
        try:
            return parse_interval(self.interval_spin.get())
        except ValueError as exc:
            self._say(str(exc))
            return None

    def _on_auto_toggle(self) -> None:
        """Command of the Auto refresh checkbutton (runs after the tick state changed)."""
        if not checkbutton_selected(self.auto_check):
            self._stop_auto()
            return
        if self._read_interval() is None:  # invalid: do not start
            set_checkbutton(self.auto_check, False)
            return
        self._say("")
        self.auto_active = True
        self._auto_tick()

    def _auto_tick(self) -> None:
        """One refresh tick: request when connected, then schedule the next tick."""
        self._auto_id = None
        if not self.auto_active or self._destroyed:
            return
        try:
            if self.client.state == ConnState.CONNECTED:
                self.request()
        finally:
            if self.auto_active and not self._destroyed:
                interval = self._read_interval()
                if interval is None:
                    self._stop_auto(uncheck=True)
                else:
                    self._auto_id = self.after(interval * 1000, self._auto_tick)

    def _stop_auto(self, uncheck: bool = False) -> None:
        self.auto_active = False
        if self._auto_id is not None:
            try:
                self.after_cancel(self._auto_id)
            except tk.TclError:
                pass
            self._auto_id = None
        if uncheck:
            set_checkbutton(self.auto_check, False)

    def shutdown(self) -> None:
        self._stop_auto()
        super().shutdown()

    def on_destroy(self) -> None:
        self._stop_auto()

    # ------------------------------------------------------------ responses
    def _mark_stale(self) -> None:
        if self.report is not None:
            self.stale_label.configure(text=STALE_TEXT)

    def on_received(self, event: Received) -> None:
        parsed = event.parsed
        if isinstance(parsed, MonitoringReport):
            self._waiting.clear()  # a late report after a timeout lands here too and clears the label
            self._show_report(parsed)
        elif isinstance(parsed, (NegativeResponse, UnknownResponse)):
            self._waiting.clear()  # the server did answer, with a failure
            self._mark_stale()

    def on_state_changed(self, state: ConnState) -> None:
        if state in (ConnState.DISCONNECTED, ConnState.ERROR):
            self._waiting.clear()  # no report can arrive on a dead link; the timeout would be a false alarm

    def on_tick(self) -> None:
        """No report within the timeout: say so in the stale label (one log line per request).

        A display state only: the next report clears it, and auto refresh keeps running.
        """
        current = guibase.now()
        while self._waiting and current >= self._waiting[0][0]:
            _, timeout = self._waiting.pop(0)
            self.stale_label.configure(text=TIMEOUT_TEXT.format(seconds=timeout))
            self.log_error(f"Monitoring request got no report within {timeout:g} s.")

    def _show_report(self, report: MonitoringReport) -> None:
        """Replace everything shown with this (the most recent) report."""
        self.report = report
        self.stale_label.configure(text="")
        revision, issue, options = report.cop1_version()
        tmu, port = report.tm_port()
        self.header_label.configure(text=(
            f"COP-1 version: revision {revision}, issue {issue}, options {options} | "
            f"TM port: TMU {tmu}, port {port} | report tag {report.tag} | "
            f"received at {time.strftime('%H:%M:%S')}"))
        self.config_tree.delete(*self.config_tree.get_children())
        for pid, name in CONFIG_PARAM_NAMES.items():
            value = to_u32(report.config[pid])  # words are shown unsigned
            decoded = ""
            if pid in self._ip_ids:
                decoded = decode_ip(value)
            elif pid in self._port_ids:
                t, p = decode_tm_port(value)
                decoded = f"TMU {t}, port {p}"
            self.config_tree.insert("", "end", iid=f"c{pid}", values=(name, value, decoded))
        self.status_tree.delete(*self.status_tree.get_children())
        for pid, name in STATUS_PARAM_NAMES.items():
            self.status_tree.insert("", "end", iid=f"s{pid}", values=(name, to_u32(report.status[pid])))
        flags = report.status_error_flags()
        if flags:
            self.flags_label.configure(text="Error flags: " + ", ".join(flags), fg=RED)
        else:
            self.flags_label.configure(text="no error flags", fg=GREEN)

    # ------------------------------------------------------------ test helpers
    def header_text(self) -> str:
        return self.header_label.cget("text")

    def stale_text(self) -> str:
        return self.stale_label.cget("text")

    def flags_text(self) -> str:
        return self.flags_label.cget("text")

    def config_rows(self) -> "list[tuple]":
        return [tuple(self.config_tree.item(i, "values")) for i in self.config_tree.get_children()]

    def status_rows(self) -> "list[tuple]":
        return [tuple(self.status_tree.item(i, "values")) for i in self.status_tree.get_children()]
