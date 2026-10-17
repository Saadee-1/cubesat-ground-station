"""App wiring (build_window and the close path)."""
import contextlib
import inspect
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from groundstation import app as app_module
from groundstation.app import build_window, configure_window, shutdown_window
from groundstation.cop1 import MonitoringReport
from groundstation.config import load_config
from groundstation.gui import TelemetryPanel
from groundstation.guibase import OptionsStore
from groundstation.source import SimFrameSource
from groundstation.tab_control import ControlTab
from groundstation.tab_monitoring import MonitoringTab
from groundstation.tab_telecommand import TelecommandTab
from groundstation.tab_telemetry import TelemetryTab
from groundstation.tm import TelemetryPipeline
from gui_util import wait_until

SAMPLE = Path(__file__).resolve().parent.parent / "groundstation" / "samples" / "sample_config_5.json"


@pytest.fixture
def window(tk_root):
    params = load_config(str(SAMPLE))
    source = SimFrameSource(params)
    pipeline = TelemetryPipeline(source, params)
    created = []

    def build(**kwargs):
        panel = build_window(tk_root, pipeline, **kwargs)
        notebook = [w for w in tk_root.winfo_children() if isinstance(w, ttk.Notebook)][-1]
        created.append(notebook)
        return panel, notebook, source

    yield build
    for tab in getattr(tk_root, "service_tabs", []):
        tab.shutdown()
    for notebook in created:
        notebook.destroy()
    source.stop()
    tk_root.update()


def tab_widgets(notebook):
    return [(notebook.tab(name, "text"), notebook.nametowidget(name)) for name in notebook.tabs()]


def test_default_build_window_has_the_new_tabs_and_keeps_the_telemetry_panel(window):
    panel, notebook, _ = window()
    assert isinstance(panel, TelemetryPanel)
    tabs = tab_widgets(notebook)
    assert [title for title, _ in tabs] == ["Control", "Monitoring", "Telecommand", "Telemetry"]
    assert isinstance(tabs[0][1], ControlTab) and isinstance(tabs[1][1], MonitoringTab)
    assert isinstance(tabs[2][1], TelecommandTab)  # was the "Pending protocol documents" placeholder
    assert isinstance(tabs[3][1], TelemetryTab)  # was a plain frame holding only the panel
    assert tabs[3][1].panel is panel


def test_default_clients_use_default_ports_and_localhost(window):
    _, notebook, _ = window()
    control, monitoring = [w for _, w in tab_widgets(notebook)[:2]]
    assert (control.client.host, control.client.port) == ("127.0.0.1", 3101)
    assert (monitoring.client.host, monitoring.client.port) == ("127.0.0.1", 3100)
    assert control.port_entry.get() == "3101" and monitoring.port_entry.get() == "3100"


def test_host_and_port_offset_arguments(window):
    _, notebook, _ = window(host="192.168.1.5", port_offset=100)
    control, monitoring = [w for _, w in tab_widgets(notebook)[:2]]
    assert (control.client.host, control.client.port) == ("192.168.1.5", 3201)
    assert (monitoring.client.host, monitoring.client.port) == ("192.168.1.5", 3200)


def test_both_tabs_share_one_options_store(window):
    store = OptionsStore()
    _, notebook, _ = window(store=store)
    control, monitoring = [w for _, w in tab_widgets(notebook)[:2]]
    assert control.store is store and monitoring.store is store


def test_close_path_calls_shutdown_on_every_service_tab_and_stops_the_source(window, tk_root, monkeypatch):
    from groundstation.mockserver import MockServer
    _, notebook, source = window()
    control, monitoring = [w for _, w in tab_widgets(notebook)[:2]]
    mocks = [MockServer("control"), MockServer("monitoring")]
    for mock in mocks:
        mock.start()
    try:
        for tab, mock in zip((control, monitoring), mocks):
            set_port = mock.port
            tab.port_entry.delete(0, "end")
            tab.port_entry.insert(0, str(set_port))
            tab.toggle_connection()
        assert wait_until(tk_root, lambda: control.state_label.cget("text") == "CONNECTED"
                          and monitoring.state_label.cget("text") == "CONNECTED")
        telecommand = tab_widgets(notebook)[2][1]
        telemetry = tab_widgets(notebook)[3][1]
        called = []
        for tab in (control, monitoring, telecommand, telemetry):
            original = tab.shutdown
            monkeypatch.setattr(tab, "shutdown", lambda t=tab, o=original: (called.append(t.title), o()))
        source.start()
        assert source.is_running()
        shutdown_window(tk_root, source)
        assert called == ["Control", "Monitoring", "Telecommand", "Telemetry"]
        assert not source.is_running()
        assert control.client._thread is None or not control.client._thread.is_alive()
        assert control.client.state.value == "DISCONNECTED" and monitoring.client.state.value == "DISCONNECTED"
    finally:
        for mock in mocks:
            mock.stop()


# ------------------------------------------------------------ layout
@contextlib.contextmanager
def laid_out(root, size, scale):
    """Map the shared root for real layout, fully transparent and far off screen.

    A withdrawn window is never laid out (its widgets report width 1), so the
    geometry tests need it mapped. `scale` multiplies the Tk scaling factor to
    imitate a high-DPI display (1.5 is a typical 150 percent setting).
    """
    base = float(root.tk.call("tk", "scaling"))
    root.tk.call("tk", "scaling", base * scale)
    root.geometry(f"{size}+12000+12000")
    root.attributes("-alpha", 0.0)
    root.deiconify()
    try:
        yield
    finally:
        root.withdraw()
        root.attributes("-alpha", 1.0)
        root.geometry("")
        root.tk.call("tk", "scaling", base)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def settle_layout(root, notebook, tab):
    notebook.select(tab)
    for _ in range(5):
        root.update()


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("size", ["1000x720", "900x600"])
def test_no_button_or_entry_is_clipped(window, tk_root, size, scale):
    """Every button and entry that has vertical room gets at least the width it asked for.

    Pack squeezes a too-wide row by shrinking (or unmapping) its last widgets, so the
    right edge alone cannot show clipping; the allocated width must be compared with the
    requested width. A widget squeezed out VERTICALLY (a small window) is skipped here.
    """
    with laid_out(tk_root, size, scale):
        _, notebook, _ = window()
        for tab in tk_root.service_tabs:
            settle_layout(tk_root, notebook, tab)
            limit = tk_root.winfo_rootx() + tk_root.winfo_width()
            checked = 0
            for w in descendants(tab):
                if not isinstance(w, (ttk.Button, ttk.Entry, tk.Entry)):
                    continue
                if w.winfo_height() < w.winfo_reqheight():
                    continue  # squeezed vertically, not a horizontal clipping problem
                name = f"{tab.title}: {w.winfo_class()} {str(w)} text={w.cget('text') if 'text' in w.keys() else ''!r}"
                assert w.winfo_ismapped(), name
                assert w.winfo_width() >= w.winfo_reqwidth(), (name, w.winfo_width(), w.winfo_reqwidth())
                assert w.winfo_rootx() + w.winfo_width() <= limit, name
                checked += 1
            assert checked >= 6, (tab.title, checked)  # the test must not pass by skipping everything


def test_every_tab_shows_at_least_eight_log_lines_at_the_default_size(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        for tab in tk_root.service_tabs:
            settle_layout(tk_root, notebook, tab)
            text = tab.log.text
            line_height = int(tk_root.tk.call("font", "metrics", text.cget("font"), "-linespace"))
            assert int(text.cget("height")) >= 8
            assert text.winfo_height() // line_height >= 8, (tab.title, text.winfo_height(), line_height)


def test_monitoring_trees_have_working_vertical_scrollbars(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        monitoring = tk_root.service_tabs[1]
        settle_layout(tk_root, notebook, monitoring)
        config = [0] * 40
        report = MonitoringReport(tag=1, config=tuple(config), status=tuple([0] * 20))
        monitoring._show_report(report)
        for i in range(30):  # make the short status table overflow too
            monitoring.status_tree.insert("", "end", iid=f"extra{i}", values=(f"row {i}", i))
        for _ in range(5):
            tk_root.update()
        scrollbars = [w for w in descendants(monitoring) if isinstance(w, ttk.Scrollbar)]
        assert int(monitoring.config_tree.cget("height")) >= 12
        assert int(monitoring.status_tree.column("name", "width")) >= 330
        for tree in (monitoring.config_tree, monitoring.status_tree):
            owned = [sb for sb in scrollbars if sb.master is tree.master]
            assert len(owned) == 1 and str(owned[0].cget("orient")) == "vertical"
            scrollbar = owned[0]
            assert scrollbar.get()[1] < 1.0  # the tree reports that it has more rows than it shows
            scrollbar.tk.call(scrollbar.cget("command"), "moveto", "0.5")  # and the bar drives the tree
            assert tree.yview()[0] > 0.0


def test_columns_of_both_monitoring_tables_fit_inside_their_trees(window, tk_root):
    with laid_out(tk_root, "1000x720", 1.0):
        _, notebook, _ = window()
        monitoring = tk_root.service_tabs[1]
        settle_layout(tk_root, notebook, monitoring)
        for tree in (monitoring.config_tree, monitoring.status_tree):
            total = sum(int(tree.column(c, "width")) for c in tree["columns"])
            assert total <= tree.winfo_width() + 1, (str(tree), total, tree.winfo_width())


def test_configure_window_sets_default_and_minimum_size():
    class RecordingRoot:
        def __init__(self):
            self.calls = []

        def title(self, text):
            self.calls.append(("title", text))

        def geometry(self, value):
            self.calls.append(("geometry", value))

        def minsize(self, width, height):
            self.calls.append(("minsize", width, height))

    root = RecordingRoot()
    configure_window(root)
    assert root.calls == [("title", "CubeSat Ground Station"), ("geometry", "1000x720"),
                          ("minsize", 900, 600)]
    assert "configure_window(root)" in inspect.getsource(app_module.main)
