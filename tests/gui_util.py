"""Shared helpers for the GUI tab tests (the session tk_root fixture lives in conftest.py).

Nothing here creates a Tk root or calls mainloop(); tests drive the GUI with
root.update() loops.
"""
import time
import tkinter as tk
from tkinter import ttk

import pytest

from groundstation.guibase import OptionsStore
from groundstation.mockserver import MockServer


def wait_until(root, predicate, timeout=5.0):
    """Run root.update() until predicate() is true; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.005)
    root.update()
    return bool(predicate())


def settle(root, seconds):
    """Keep the GUI running for a fixed time (used to prove that nothing happens)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)


def pending_after_count(root):
    """Number of after() callbacks currently scheduled in the shared Tk interpreter."""
    return len(root.tk.splitlist(root.tk.call("after", "info")))


def log_lines(tab):
    return tab.log.lines()


def has_log_line(tab, text):
    return any(text in line for line in tab.log.lines())


class GuiEnv:
    """Creates mocks, stores and tabs and tears them all down in the right order."""

    def __init__(self, root):
        self.root = root
        self.mocks, self.tabs, self.frames, self.stores = [], [], [], []

    def mock(self, service, **kwargs):
        server = MockServer(service, **kwargs)
        server.start()
        self.mocks.append(server)
        return server

    def store(self):
        store = OptionsStore()
        self.stores.append(store)
        return store

    def make_tab(self, tab_cls, client_cls, mock=None, store=None, connect=False, port=None,
                 pump_ms=20, client_kwargs=None):
        store = store or self.store()
        if store not in self.stores:
            self.stores.append(store)
        if port is None:
            port = mock.port if mock is not None else 1
        client = client_cls(port=port, **(client_kwargs or {}))
        frame = ttk.Frame(self.root)
        frame.pack()
        self.frames.append(frame)
        tab = tab_cls(frame, client, store, pump_ms=pump_ms)
        tab.pack(fill="both", expand=True)
        self.tabs.append(tab)
        self.root.update()
        if connect:
            tab.toggle_connection()
            assert wait_until(self.root, lambda: tab.state_label.cget("text") == "CONNECTED"), \
                tab.log.lines()
        return tab

    def dialog(self, store):
        from groundstation.guibase import ProtocolOptionsDialog
        return ProtocolOptionsDialog(self.root, store)

    def close(self):
        for tab in self.tabs:
            try:
                tab.shutdown()
            except tk.TclError:
                pass
        for store in self.stores:
            dialog = store.dialog
            if dialog is not None:
                try:
                    dialog.destroy()
                except tk.TclError:
                    pass
        for frame in self.frames:
            try:
                frame.destroy()
            except tk.TclError:
                pass
        for server in self.mocks:
            server.stop()
        self.root.update()


@pytest.fixture
def gui_env(tk_root):
    env = GuiEnv(tk_root)
    yield env
    env.close()
