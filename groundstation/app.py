"""Main window: four tabs, Telemetry hosts the live panel."""
import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional, Sequence

from .clients import ControlClient, MonitoringClient, TelecommandClient, TelemetryClient
from .config import load_config
from .crashlog import CrashLog
from .errors import ConfigError
from .gui import TelemetryPanel
from .guibase import OptionsStore
from .source import SimFrameSource
from .tab_control import ControlTab
from .tab_monitoring import MonitoringTab
from .tab_telecommand import TelecommandTab
from .tab_telemetry import TelemetryTab
from .protocol import DEFAULT_PORTS
from .tm import TelemetryPipeline

PACKAGE_DIR = Path(__file__).resolve().parent


def resolve_config_path(arg: str) -> Path:
    """Use the path as given; else try it relative to the groundstation package."""
    path = Path(arg)
    if not path.exists() and (PACKAGE_DIR / arg).exists():
        return PACKAGE_DIR / arg
    return path


def build_window(root: tk.Tk, pipeline: TelemetryPipeline, *, clients: Optional[dict] = None,
                 store: Optional[OptionsStore] = None, host: str = "127.0.0.1",
                 port_offset: int = 0) -> TelemetryPanel:
    """Create the notebook with its four tabs inside `root`.

    All four tabs are live. `clients` may hold ready-made {"control": ..., "monitoring": ...,
    "telecommand": ..., "telemetry": ...} clients; otherwise they are created for `host` on the
    default ports plus `port_offset`. If `pipeline.source` is not already a SwitchableSource it is
    wrapped (default mode: Simulated), so existing callers keep their simulated stream. The service tabs are kept in
    root.service_tabs so the window close path can call shutdown() on each.
    """
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    store = store or OptionsStore()
    clients = dict(clients or {})
    clients.setdefault("control", ControlClient(host, DEFAULT_PORTS["control"] + port_offset))
    clients.setdefault("monitoring", MonitoringClient(host, DEFAULT_PORTS["monitoring"] + port_offset))
    clients.setdefault("telecommand", TelecommandClient(host, DEFAULT_PORTS["telecommand"] + port_offset))
    clients.setdefault("telemetry", TelemetryClient(host, DEFAULT_PORTS["telemetry"] + port_offset))
    service_tabs = []
    panel: Optional[TelemetryPanel] = None
    for title in ("Control", "Monitoring", "Telecommand", "Telemetry"):
        if title == "Control":
            tab = ControlTab(notebook, clients["control"], store)
            service_tabs.append(tab)
        elif title == "Monitoring":
            tab = MonitoringTab(notebook, clients["monitoring"], store)
            service_tabs.append(tab)
        elif title == "Telecommand":
            tab = TelecommandTab(notebook, clients["telecommand"], store)
            service_tabs.append(tab)
        else:
            tab = TelemetryTab(notebook, clients["telemetry"], store, pipeline)
            service_tabs.append(tab)
            panel = tab.panel
        notebook.add(tab, text=title)
    assert panel is not None
    root.service_tabs = service_tabs
    return panel


DEFAULT_GEOMETRY = "1000x720"
MIN_WIDTH, MIN_HEIGHT = 900, 600


def configure_window(root: tk.Tk) -> None:
    """Title, default size and minimum size of the main window."""
    root.title("CubeSat Ground Station")
    root.geometry(DEFAULT_GEOMETRY)
    root.minsize(MIN_WIDTH, MIN_HEIGHT)


def shutdown_window(root: tk.Tk, source=None) -> None:
    """Close path without destroying the window: shutdown() every service tab, then stop the source."""
    for tab in getattr(root, "service_tabs", []):
        tab.shutdown()
    if source is not None:
        source.stop()


TK_FAILURE_MESSAGE = (
    "The window could not be opened: {error}\n"
    "Usual causes: no display (a remote or headless session), or a Tcl/Tk installation that Python cannot "
    "find. If the message mentions init.tcl or tk.tcl, check the TCL_LIBRARY and TK_LIBRARY environment "
    "variables (they must be unset or point at THIS Python's tcl folder), or reinstall Python with "
    "\"tcl/tk and IDLE\" ticked.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the command line, build the app and run it.

    Every unhandled exception (Tk callback, main thread or worker thread) is written to stderr and
    appended to groundstation_errors.log in the current directory while the application runs.
    """
    parser = argparse.ArgumentParser(prog="python -m groundstation",
                                     description="CubeSat ground station (simulated source)")
    parser.add_argument("--config", default="samples/sample_config_5.json",
                        help="JSON parameter definition file")
    parser.add_argument("--rate", type=float, default=2.0, help="simulated frames per second")
    parser.add_argument("--corrupt", type=float, default=0.0,
                        help="probability (0-1) that a simulated frame is corrupted")
    parser.add_argument("--host", default="127.0.0.1", help="host of the COP-1 services")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="added to the default ports of the Control, Monitoring, Telecommand and Telemetry clients")
    args = parser.parse_args(argv)
    # parser.error prints a message and raises SystemExit(2).
    if args.rate <= 0:
        parser.error("--rate must be greater than 0")
    if not 0.0 <= args.corrupt <= 1.0:
        parser.error("--corrupt must be between 0 and 1")

    try:
        params = load_config(str(resolve_config_path(args.config)))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    # The hooks stay installed until the process ends: an exception that escapes main() must still
    # reach the log when the interpreter hands it to sys.excepthook.
    crash_log = CrashLog().install()  # main thread and worker threads first, so even Tk() is covered
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display, bad Tcl installation: a plain message, not a traceback
        print(TK_FAILURE_MESSAGE.format(error=exc), file=sys.stderr)
        return 2
    crash_log.install(root)  # now the Tk callbacks too
    source = SimFrameSource(params, rate_hz=args.rate, corrupt_prob=args.corrupt)
    pipeline = TelemetryPipeline(source, params)
    configure_window(root)
    build_window(root, pipeline, host=args.host, port_offset=args.port_offset)

    def on_close() -> None:
        """Shut the service tabs down, stop the source thread, then close the window."""
        shutdown_window(root, source)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    source.start()  # begin streaming right away; Stop/Start buttons still work
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
