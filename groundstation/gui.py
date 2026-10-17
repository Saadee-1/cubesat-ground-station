"""Tkinter telemetry panel: builds one row per parameter from the config."""
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .config import ParamDef, load_config
from .decode import (STATUS_CONVERSION_ERROR, STATUS_OK, STATUS_OUT_OF_RANGE,
                     STATUS_SHORT_FRAME, ParamValue)
from .errors import ConfigError
from .tm import TelemetryPipeline

STALE_AFTER_S = 3.0   # a row older than this shows STALE
POLL_MS = 100         # how often the GUI thread polls the pipeline
CLEAN_POLLS_TO_CLEAR = 100  # 100 polls x POLL_MS (100 ms) = 10 s before the error text clears
FLASH_MS = 200       # how long the "just updated" flash lasts
FLASH_COLOUR = "#cfe8ff"
STALE_COLOUR = "#808080"
STATUS_COLOURS = {
    STATUS_OK: "#2e9e44",
    STATUS_OUT_OF_RANGE: "#e0a000",
    STATUS_SHORT_FRAME: "#d03030",
    STATUS_CONVERSION_ERROR: "#d03030",
}
HEADERS = ("Name", "Value", "Unit", "Raw", "Status", "Update")


class _Row:
    """The widgets and state of one parameter row."""

    def __init__(self) -> None:
        self.labels: dict[str, tk.Label] = {}
        self.last_update: Optional[float] = None  # time.monotonic() of last update
        self.alive = True                          # False once the row is destroyed


class TelemetryPanel(ttk.Frame):
    """Scrollable table of live telemetry plus toolbar and status bar."""

    def __init__(self, master: tk.Misc, pipeline: TelemetryPipeline) -> None:
        super().__init__(master)
        self.pipeline = pipeline
        self.rows: list[_Row] = []
        self._last_error: Optional[str] = None  # most recent poll failure, if any
        self._last_error_time: Optional[float] = None  # wall-clock time of that failure
        self._clean_polls = 0  # consecutive polls without an exception
        self._status_text = tk.StringVar()
        self._build_toolbar()
        self._build_table()
        ttk.Label(self, textvariable=self._status_text, relief="sunken",
                  anchor="w").pack(side="bottom", fill="x")
        self.rebuild(pipeline.params)
        self._after_id = self.after(POLL_MS, self._poll)  # first poll
        self.bind("<Destroy>", self._on_destroy)

    # ---- construction -------------------------------------------------
    def _build_toolbar(self) -> None:
        """Buttons along the top."""
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", padx=4, pady=4)
        ttk.Button(bar, text="Load config...", command=self._on_load).pack(side="left")
        ttk.Button(bar, text="Start", command=self.pipeline.source.start).pack(side="left", padx=4)
        ttk.Button(bar, text="Stop", command=self.pipeline.source.stop).pack(side="left")

    def _build_table(self) -> None:
        """Canvas + scrollbar holding an inner frame that contains the rows."""
        holder = ttk.Frame(self)
        holder.pack(side="top", fill="both", expand=True)
        self._canvas = tk.Canvas(holder, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._inner = tk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        # Keep the scrollable area equal to the size of the inner frame...
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        # ...and make the inner frame as wide as the canvas.
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._window, width=e.width))
        # Mouse wheel scrolls only while the pointer is over the table.
        self._canvas.bind("<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event: tk.Event) -> None:
        """Scroll one notch per wheel click (Windows reports multiples of 120)."""
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---- rows ---------------------------------------------------------
    def rebuild(self, params: list[ParamDef]) -> None:
        """Destroy all rows and create one new row per parameter."""
        for row in self.rows:
            row.alive = False  # stops pending flash callbacks touching dead widgets
        for child in self._inner.winfo_children():
            child.destroy()
        self.rows = []
        for col, text in enumerate(HEADERS):
            tk.Label(self._inner, text=text, font=("TkDefaultFont", 9, "bold"),
                     anchor="w", padx=6).grid(row=0, column=col, sticky="ew")
            self._inner.grid_columnconfigure(col, weight=1)
        for r, param in enumerate(params, start=1):
            row = _Row()
            for col, key in enumerate(HEADERS):
                text = param.name if key == "Name" else param.unit if key == "Unit" else "--"
                label = tk.Label(self._inner, text=text, anchor="w", padx=6)
                label.grid(row=r, column=col, sticky="ew")
                row.labels[key] = label
            row.labels["Update"].configure(fg=STALE_COLOUR)
            self.rows.append(row)

    def _apply(self, row: _Row, pv: ParamValue) -> None:
        """Show one decoded value in its row."""
        labels = row.labels
        labels["Value"].configure(
            text="--" if pv.value is None else f"{pv.value:.{pv.decimals}f}")
        # Floats get 6 significant digits (avoids 0.30000000000000004 style noise);
        # ints are shown exactly.
        if pv.raw is None:
            raw_text = "--"
        elif isinstance(pv.raw, float):
            raw_text = f"{pv.raw:.6g}"
        else:
            raw_text = str(pv.raw)
        labels["Raw"].configure(text=raw_text)
        labels["Status"].configure(text=pv.status, fg="white",
                                   bg=STATUS_COLOURS.get(pv.status, "#d03030"))
        # Only genuine data counts as an update; for SHORT_FRAME / CONVERSION_ERROR
        # last_update is left alone so the Update column keeps ageing to STALE.
        if pv.status in (STATUS_OK, STATUS_OUT_OF_RANGE):
            row.last_update = time.monotonic()

    def _flash(self, row: _Row) -> None:
        """Briefly colour the value cells, then restore them."""
        default = self._inner.cget("bg")
        for key in ("Value", "Raw"):
            row.labels[key].configure(bg=FLASH_COLOUR)

        def restore() -> None:
            if row.alive:  # the row may have been rebuilt meanwhile
                for k in ("Value", "Raw"):
                    row.labels[k].configure(bg=default)
        self.after(FLASH_MS, restore)

    # ---- polling ------------------------------------------------------
    def _poll(self) -> None:
        """Runs on the GUI thread every POLL_MS: decode new frames and refresh."""
        try:
            for decoded in self.pipeline.poll():
                for row, pv in zip(self.rows, decoded):
                    self._apply(row, pv)
            # Flash every row that changed in the last poll interval.
            now = time.monotonic()
            for row in self.rows:
                if row.last_update is not None and now - row.last_update < POLL_MS / 1000:
                    self._flash(row)
                self._refresh_age(row, now)
            # A clean poll: after CLEAN_POLLS_TO_CLEAR in a row, forget the old error.
            self._clean_polls += 1
            if self._last_error and self._clean_polls >= CLEAN_POLLS_TO_CLEAR:
                self._last_error = None
                self._last_error_time = None
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            # Remember the problem (and when) so the status bar can show it, then carry on.
            self._record_error(exc)
        finally:
            # Always show the status bar and ALWAYS schedule the next poll,
            # even if the code above failed; otherwise the GUI would freeze.
            try:
                self._refresh_status_bar()
            except Exception as exc:  # noqa: BLE001
                self._record_error(exc)
            self._after_id = self.after(POLL_MS, self._poll)

    def _record_error(self, exc: Exception) -> None:
        """Store an error message and its time, and restart the clean-poll count."""
        self._last_error = f"ERROR: {type(exc).__name__}: {exc}"
        self._last_error_time = time.time()
        self._clean_polls = 0

    @staticmethod
    def _refresh_age(row: _Row, now: float) -> None:
        """Update the 'seconds since last update' cell."""
        label = row.labels["Update"]
        if row.last_update is None:
            label.configure(text="--", fg=STALE_COLOUR)
            return
        age = now - row.last_update
        if age > STALE_AFTER_S:
            label.configure(text="STALE", fg=STALE_COLOUR)
        else:
            label.configure(text=f"{age:.1f} s", fg="black")

    def _refresh_status_bar(self) -> None:
        """Update the text along the bottom."""
        last = self.pipeline.last_update_time
        last_text = "never" if last is None else time.strftime("%H:%M:%S", time.localtime(last))
        text = (
            f"Source: {self.pipeline.source.name}   |   Frames: {self.pipeline.frames_total}"
            f"   |   With errors: {self.pipeline.frames_with_errors}"
            f"   |   Out of limits: {self.pipeline.frames_out_of_range}"
            f"   |   Last update: {last_text}")
        if self._last_error:
            when = time.strftime("%H:%M:%S", time.localtime(self._last_error_time))
            text += f" | Last error ({when}): {self._last_error}"
        self._status_text.set(text)

    # ---- actions ------------------------------------------------------
    def _on_load(self) -> None:
        """Ask for a config file; on error keep the old configuration."""
        path = filedialog.askopenfilename(
            title="Load config", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return  # user cancelled
        try:
            params = load_config(path)
        except ConfigError as exc:
            messagebox.showerror("Invalid configuration", str(exc))
            return
        self.pipeline.set_params(params)
        self.rebuild(params)

    def _on_destroy(self, event: tk.Event) -> None:
        """Cancel the repeating poll when the panel is destroyed."""
        if event.widget is self:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
