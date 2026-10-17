import gc

import pytest


@pytest.fixture(scope="session")
def tk_root():
    """One shared hidden Tk root for the whole pytest process.

    Creating a 2nd or later tk.Tk() in one process can fail intermittently on
    Windows, so every GUI test uses this single root. A failure here is NOT
    skipped: it fails loudly.
    """
    tk = pytest.importorskip("tkinter")  # only skips if tkinter itself is not installed
    root = tk.Tk()
    root.withdraw()  # keep the window invisible during tests
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass  # teardown only: nothing useful to do if Tk is already gone


@pytest.fixture(autouse=True)
def _collect_garbage_on_main_thread():
    """Free leftover Tk variables on the main thread right after each test.

    A destroyed TelemetryPanel stays in a reference cycle until the cycle
    collector runs. If that happens on another thread (for example a mock server
    thread) tkinter's Variable.__del__ fails with "main thread is not in main loop".
    """
    yield
    gc.collect()
