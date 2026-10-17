"""Crash visibility, `python -m groundstation`, and the root launchers."""
import os
import runpy
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

import pytest

from groundstation import app, crashlog
from groundstation.crashlog import CrashLog
from gui_util import gui_env, settle  # noqa: F401
from test_app_gui import window  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "groundstation" / "samples" / "sample_config_5.json"


@pytest.fixture
def hooks():
    """Remember and restore every global hook a test may touch."""
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def boom(message="boom"):
    raise RuntimeError(message)


# ------------------------------------------------------------ crashlog
def test_a_raising_tk_callback_is_written_to_stderr_and_the_log_file(tk_root, tmp_path, capsys, hooks):
    log = CrashLog(str(tmp_path / "errors.log")).install(tk_root)
    try:
        tk_root.after(0, lambda: boom("callback exploded"))
        for _ in range(5):
            tk_root.update()
    finally:
        log.restore()
    text = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "unhandled exception in a Tk callback" in text and "RuntimeError: callback exploded" in text
    assert "Traceback (most recent call last)" in text and "boom" in text
    err = capsys.readouterr().err
    assert "RuntimeError: callback exploded" in err and "Traceback" in err
    assert log.count == 1


def test_a_worker_thread_exception_is_written_to_stderr_and_the_log_file(tmp_path, capsys, hooks):
    log = CrashLog(str(tmp_path / "errors.log")).install()
    try:
        thread = threading.Thread(target=lambda: boom("thread exploded"), name="demo-worker")
        thread.start()
        thread.join()
    finally:
        log.restore()
    text = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "unhandled exception in thread demo-worker" in text and "RuntimeError: thread exploded" in text
    assert "RuntimeError: thread exploded" in capsys.readouterr().err


def test_the_main_thread_hook_records_the_exception(tmp_path, capsys, hooks):
    log = CrashLog(str(tmp_path / "errors.log")).install()
    try:
        try:
            boom("main exploded")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())               # what the interpreter does for an uncaught error
    finally:
        log.restore()
    assert "unhandled exception in the main thread" in (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "main exploded" in capsys.readouterr().err


def test_errors_append_to_the_same_file(tmp_path, capsys, hooks):
    path = tmp_path / "errors.log"
    log = CrashLog(str(path)).install()
    try:
        for n in (1, 2, 3):
            try:
                boom(f"error {n}")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
    finally:
        log.restore()
    text = path.read_text(encoding="utf-8")
    assert [f"error {n}" in text for n in (1, 2, 3)] == [True] * 3 and text.count("=== ") == 3


def test_an_unwritable_log_still_reaches_stderr_and_nothing_raises(tmp_path, capsys, hooks):
    bad = tmp_path / "no_such_folder" / "errors.log"      # the folder does not exist
    log = CrashLog(str(bad)).install()
    try:
        for n in (1, 2):
            try:
                boom(f"unwritable {n}")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())           # must not raise
    finally:
        log.restore()
    err = capsys.readouterr().err
    assert "unwritable 1" in err and "unwritable 2" in err
    assert err.count("could not write") == 1              # said once, not on every error
    assert log.file_failed is True and not bad.exists()


def test_a_directory_in_place_of_the_log_file_is_also_survived(tmp_path, capsys, hooks):
    (tmp_path / "errors.log").mkdir()
    log = CrashLog(str(tmp_path / "errors.log")).install()
    try:
        try:
            boom("dir in the way")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    finally:
        log.restore()
    assert "dir in the way" in capsys.readouterr().err


def test_no_stderr_at_all_does_not_break_the_hook(tmp_path, monkeypatch, hooks):
    monkeypatch.setattr(sys, "stderr", None)              # what pythonw gives you
    log = CrashLog(str(tmp_path / "errors.log")).install()
    try:
        try:
            boom("no console")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    finally:
        log.restore()
    assert "no console" in (tmp_path / "errors.log").read_text(encoding="utf-8")


def test_a_runaway_error_loop_cannot_fill_the_disk(tmp_path, monkeypatch, capsys, hooks):
    monkeypatch.setattr(crashlog, "MAX_LOG_BYTES", 600)
    path = tmp_path / "errors.log"
    log = CrashLog(str(path)).install()
    try:
        for n in range(50):
            try:
                boom(f"loop {n}")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
    finally:
        log.restore()
    assert path.stat().st_size < 600 + 4000               # it stopped soon after the cap
    assert "loop 49" in capsys.readouterr().err           # stderr kept going


def test_keyboard_interrupt_and_thread_exit_are_not_crashes(tmp_path, capsys, hooks):
    path = tmp_path / "errors.log"
    log = CrashLog(str(path)).install()
    try:
        thread = threading.Thread(target=lambda: sys.exit(0))
        thread.start()
        thread.join()
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    finally:
        log.restore()
    assert not path.exists() or path.read_text(encoding="utf-8") == ""


def test_restore_puts_every_hook_back(tk_root, tmp_path, hooks):
    before = (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root))
    log = CrashLog(str(tmp_path / "errors.log")).install(tk_root)
    assert sys.excepthook is not before[0] and threading.excepthook is not before[1]
    assert "report_callback_exception" in vars(tk_root)
    log.restore()
    assert (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root)) == before


def test_restore_after_two_installs_still_gives_back_the_originals(tk_root, tmp_path, hooks):
    """main() installs twice (process hooks first, then with the Tk root): the originals must win."""
    before = (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root))
    log = CrashLog(str(tmp_path / "errors.log"))
    log.install()
    log.install(tk_root)
    assert sys.excepthook is not before[0]
    log.restore()
    assert (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root)) == before


def test_default_log_name_is_in_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = CrashLog()
    assert crashlog.LOG_NAME == "groundstation_errors.log"
    assert Path(log.path) == tmp_path / "groundstation_errors.log"


# ------------------------------------------------------------ main() installs them, build_window does not
def test_build_window_installs_no_crash_hooks(window, tk_root):
    before = (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root))
    window()
    assert (sys.excepthook, threading.excepthook, "report_callback_exception" in vars(tk_root)) == before


class FakeSource:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeRoot:
    """Stands in for tk.Tk so main() can run without a window or a mainloop."""

    def __init__(self):
        self.seen = {}

    def protocol(self, *args):
        pass

    def mainloop(self):
        self.seen["excepthook"] = sys.excepthook
        self.seen["threading"] = threading.excepthook
        self.seen["tk"] = self.report_callback_exception

    def report_callback_exception(self, *args):  # the default handler (a class method, as in real Tk)
        pass


def patch_main(monkeypatch, root):
    monkeypatch.setattr(app.tk, "Tk", lambda: root)
    monkeypatch.setattr(app, "SimFrameSource", FakeSource)
    monkeypatch.setattr(app, "configure_window", lambda r: None)
    monkeypatch.setattr(app, "build_window", lambda *a, **k: None)


def test_main_installs_all_three_hooks_and_leaves_them_in_place(monkeypatch, hooks):
    root = FakeRoot()
    patch_main(monkeypatch, root)
    before = (sys.excepthook, threading.excepthook)
    assert app.main(["--config", str(SAMPLE)]) == 0
    assert root.seen["excepthook"] is not before[0] and root.seen["threading"] is not before[1]
    assert getattr(root.seen["tk"], "__name__", "") == "<lambda>"       # our Tk handler, not the default method
    # They stay installed: an exception that escapes main() must still reach the log at interpreter exit.
    assert sys.excepthook is root.seen["excepthook"] and threading.excepthook is root.seen["threading"]


def test_main_writes_a_crash_in_the_main_thread_to_the_log_in_the_current_directory(
        monkeypatch, tmp_path, capsys, hooks):
    monkeypatch.chdir(tmp_path)
    root = FakeRoot()
    patch_main(monkeypatch, root)

    def mainloop():
        try:
            boom("inside the app")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

    root.mainloop = mainloop
    assert app.main(["--config", str(SAMPLE)]) == 0
    assert "inside the app" in (tmp_path / "groundstation_errors.log").read_text(encoding="utf-8")


def test_a_window_that_cannot_open_gives_a_plain_message_and_exit_code_2(monkeypatch, capsys, hooks):
    def no_display():
        raise tk.TclError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(app.tk, "Tk", no_display)
    assert app.main(["--config", str(SAMPLE)]) == 2
    err = capsys.readouterr().err
    assert "The window could not be opened" in err and "no display name" in err
    assert "TCL_LIBRARY" in err and "Traceback" not in err


def test_an_unexpected_failure_while_opening_the_window_still_reaches_the_log(monkeypatch, tmp_path, capsys, hooks):
    monkeypatch.chdir(tmp_path)

    def broken():
        raise RuntimeError("tk exploded")

    monkeypatch.setattr(app.tk, "Tk", broken)
    try:
        app.main(["--config", str(SAMPLE)])
    except RuntimeError:
        sys.excepthook(*sys.exc_info())                   # what the interpreter does with an escaping error
    else:
        pytest.fail("the RuntimeError should have escaped main()")
    assert "tk exploded" in (tmp_path / "groundstation_errors.log").read_text(encoding="utf-8")
    assert "tk exploded" in capsys.readouterr().err


def test_a_failure_while_building_the_window_still_reaches_the_log(monkeypatch, tmp_path, capsys, hooks):
    monkeypatch.chdir(tmp_path)
    root = FakeRoot()
    patch_main(monkeypatch, root)

    def broken(*args, **kwargs):
        raise RuntimeError("build exploded")

    monkeypatch.setattr(app, "build_window", broken)
    try:
        app.main(["--config", str(SAMPLE)])
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    else:
        pytest.fail("the RuntimeError should have escaped main()")
    assert "build exploded" in (tmp_path / "groundstation_errors.log").read_text(encoding="utf-8")


def test_a_bad_tcl_installation_is_reported_in_plain_words(monkeypatch, capsys, hooks):
    def bad_tcl():
        raise tk.TclError("Can't find a usable init.tcl in the following directories: C:/nope")

    monkeypatch.setattr(app.tk, "Tk", bad_tcl)
    assert app.main(["--config", str(SAMPLE)]) == 2
    err = capsys.readouterr().err
    assert "init.tcl" in err and "Traceback" not in err


def test_a_config_error_is_still_reported_before_any_window_is_opened(monkeypatch, tmp_path, capsys, hooks):
    opened = []
    monkeypatch.setattr(app.tk, "Tk", lambda: opened.append(1))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert app.main(["--config", str(bad)]) == 2 and opened == []
    assert "Config error" in capsys.readouterr().err


# ------------------------------------------------------------ python -m groundstation
def load_main_module():
    return runpy.run_module("groundstation.__main__", run_name="groundstation_main_under_test")


def test_python_problem_by_version():
    module = load_main_module()
    check = module["python_problem"]
    assert module["MIN_PYTHON"] == (3, 10)
    assert check((3, 10, 0)) is None and check((3, 11, 2)) is None and check((4, 0)) is None
    for old in ((3, 9, 18), (3, 8, 0), (3, 7, 3), (2, 7, 18), (3, 0)):
        message = check(old)
        assert message and "3.10" in message and f"found {old[0]}.{old[1]}" in message
    assert check(sys.version_info) is None


def test_too_old_python_exits_with_code_2_and_a_plain_message_before_anything_else_is_imported(
        monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    for name in [n for n in sys.modules if n.startswith("groundstation.app")]:
        monkeypatch.delitem(sys.modules, name)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("groundstation.__main__", run_name="__main__")
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "needs Python 3.10" in err and "found 3.9" in err and "Traceback" not in err
    assert "groundstation.app" not in sys.modules             # nothing heavy was imported before the exit


def test_missing_tkinter_gives_a_plain_message_and_exit_code_2(monkeypatch, capsys):
    module = load_main_module()
    monkeypatch.setitem(sys.modules, "tkinter", None)       # makes "import tkinter" raise ImportError
    assert module["main"]([]) == 2
    err = capsys.readouterr().err
    assert "no tkinter" in err and "tcl/tk and IDLE" in err and "Traceback" not in err


def test_main_runs_the_application_after_the_checks(monkeypatch):
    module = load_main_module()
    calls = []
    monkeypatch.setattr(app, "main", lambda argv=None: calls.append(argv) or 7)
    assert module["main"](["--rate", "3"]) == 7 and calls == [["--rate", "3"]]


def test_the_package_can_be_started_with_dash_m():
    done = subprocess.run([sys.executable, "-m", "groundstation", "--help"], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0 and "usage: python -m groundstation" in done.stdout


def test_dash_m_with_a_bad_option_exits_2_without_a_traceback():
    done = subprocess.run([sys.executable, "-m", "groundstation", "--rate", "0"], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 2 and "Traceback" not in done.stderr and "--rate must be greater than 0" in done.stderr


# ------------------------------------------------------------ launchers
LAUNCHERS = ["run_app.bat", "run_mock.bat", "run_tests.bat"]


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_is_ascii_crlf_and_looks_for_python_then_py(name):
    data = (ROOT / name).read_bytes()
    assert all(b < 128 for b in data), "launchers must be plain ASCII"
    assert data.count(b"\n") == data.count(b"\r\n") > 5, "every line must end with CRLF"
    text = data.decode("ascii")
    assert text.index('set "PY=python"') < text.index('set "PY=py -3"')       # python first, then py -3
    assert "%*" in text and "(3, 10)" in text and "exit /b 2" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_launchers_run_the_right_modules():
    assert "-m groundstation %*" in (ROOT / "run_app.bat").read_text()
    assert "-m groundstation.mockserver_cli %*" in (ROOT / "run_mock.bat").read_text()
    tests = (ROOT / "run_tests.bat").read_text()
    assert "-m pytest %*" in tests and "import pytest" in tests and "pip install pytest" in tests


def test_the_tcl_variables_are_reported_not_cleared():
    text = (ROOT / "run_app.bat").read_text()
    assert "if defined TCL_LIBRARY echo" in text and "if defined TK_LIBRARY echo" in text
    assert "set TCL_LIBRARY=" in text and "set \"TCL_LIBRARY=\"" not in text   # only a hint in an echo line


def run_tree(command, timeout, env=None):
    """Run a command; on a timeout kill the WHOLE process tree (a launcher that lost its arguments
    would otherwise leave the real GUI window open). Returns (exit code or None, combined output)."""
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True)
    try:
        out, _ = process.communicate(timeout=timeout)
        return process.returncode, out
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        out, _ = process.communicate()
        return None, out


@pytest.mark.skipif(os.name != "nt", reason="the launchers are Windows batch files")
@pytest.mark.parametrize("name, args, expect", [
    ("run_app.bat", ["--help"], "usage: python -m groundstation"),
    ("run_mock.bat", ["--help"], "usage:"),
    ("run_tests.bat", ["--version"], "pytest"),
])
def test_launcher_really_runs_and_passes_its_arguments_through(name, args, expect):
    code, output = run_tree(["cmd", "/c", str(ROOT / name), *args], timeout=60)
    assert code == 0 and expect in output, (code, output[:300])


@pytest.mark.skipif(os.name != "nt", reason="the launchers are Windows batch files")
def test_launcher_gives_a_plain_message_and_exit_2_when_no_python_is_found(tmp_path):
    env = dict(os.environ, PATH=str(tmp_path))               # nothing on the PATH, not even py.exe
    code, output = run_tree([os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/c",
                             str(ROOT / "run_app.bat")], timeout=60, env=env)
    assert code == 2 and "Python 3.10 or newer was not found" in output, (code, output[:300])
