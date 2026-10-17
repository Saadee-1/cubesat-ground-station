"""Crash visibility: every unhandled exception is written to stderr AND to groundstation_errors.log.

Three hooks cover every place an exception can escape: Tk callbacks (root.report_callback_exception),
the main thread (sys.excepthook) and worker threads (threading.excepthook). Only app.main() installs
them. A failing log file never hides an error: the traceback still goes to stderr.
No Tk is imported here, so this module is safe to import anywhere.
"""
import os
import sys
import threading
import time
import traceback
from typing import Optional

LOG_NAME = "groundstation_errors.log"
MAX_LOG_BYTES = 2_000_000  # stop appending past this; a runaway error loop must not fill the disk


class CrashLog:
    """Writes tracebacks to stderr and to a log file in the current directory (resolved at install)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path or LOG_NAME)
        self.file_failed = False  # set by the first write failure; later errors go to stderr only
        self.count = 0
        self._lock = threading.Lock()
        self._previous: "list[tuple[object, str, object, bool]]" = []  # owner, attribute, old value, was own

    # ------------------------------------------------------------ recording
    def record(self, source: str, exc_type, exc, tb) -> None:
        """Write one traceback. Never raises: this runs inside exception hooks."""
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            header = f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} unhandled exception in {source} ==="
            block = f"{header}\n{text}" if text.endswith("\n") else f"{header}\n{text}\n"
            with self._lock:
                self.count += 1
                self._write_stderr(block)
                self._append_file(block)
        except Exception:  # noqa: BLE001 - a hook that raises would hide the original error
            pass

    @staticmethod
    def _write_stderr(block: str) -> None:
        stream = sys.stderr
        if stream is None:  # pythonw has no console
            return
        try:
            stream.write(block)
            stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def _append_file(self, block: str) -> None:
        if self.file_failed:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_LOG_BYTES:
                return
            with open(self.path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(block)
        except OSError as exc:
            self.file_failed = True
            self._write_stderr(f"(groundstation could not write {self.path}: {exc}; "
                               f"errors go to this window only)\n")

    # ------------------------------------------------------------ installing
    def install(self, root=None) -> "CrashLog":
        """Replace sys.excepthook and threading.excepthook, and (given a root) the Tk callback handler.

        May be called twice (first without a root, then with one). restore() puts the hooks back in
        reverse order, so the ORIGINAL ones win however often install() was called.
        """
        self._previous += [(sys, "excepthook", sys.excepthook, True),
                           (threading, "excepthook", threading.excepthook, True)]

        def on_main(exc_type, exc, tb) -> None:
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc, tb)
                return
            self.record("the main thread", exc_type, exc, tb)

        def on_thread(args) -> None:
            if args.exc_type is SystemExit:
                return
            name = args.thread.name if args.thread is not None else "unknown thread"
            self.record(f"thread {name}", args.exc_type, args.exc_value, args.exc_traceback)

        sys.excepthook = on_main
        threading.excepthook = on_thread
        if root is not None and not any(owner is root for owner, _, _, _ in self._previous):
            self._previous.append((root, "report_callback_exception", root.report_callback_exception,
                                   "report_callback_exception" in vars(root)))
            root.report_callback_exception = lambda exc_type, exc, tb: self.record(
                "a Tk callback", exc_type, exc, tb)
        return self

    def restore(self) -> None:
        """Put the previous hooks back (the tests and the end of main() use this)."""
        for owner, name, value, was_own in reversed(self._previous):
            try:
                if was_own:
                    setattr(owner, name, value)
                else:
                    delattr(owner, name)  # the Tk default is the class method: just remove our override
            except Exception:  # noqa: BLE001 - e.g. the Tk root is already destroyed
                pass
        self._previous = []
