"""python -m groundstation.selfcheck (no GUI, standard library only)."""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from groundstation import protocol, selfcheck
from groundstation.cop1 import build_monitoring_request
from groundstation.mockserver import MockServer
from groundstation.protocol import FrameReader, Message, ProtocolOptions, build_message
from scripted_server import scripted  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
CONFIG, STATUS = [0] * 40, [0] * 20


def collect(lines):
    return lambda text: lines.append(text)


def monitoring_report(tag):
    return build_message(0, [0, 0, tag, *CONFIG, *STATUS])


def messages_received_by(server):
    reader, found = FrameReader(), []
    for event in reader.feed(bytes(server.received)):
        if isinstance(event, Message):
            found.append(event)
    return found


def closed_port():
    """A port number on 127.0.0.1 that nothing is listening on."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def replying(answer_on=None, with_negative=False):
    """A scripted server that answers the n-th request (1-based) in `answer_on` (None = every request)."""
    def script(server, conn):
        reader, count = FrameReader(), 0

        def on_data(data):
            nonlocal count
            for event in reader.feed(data):
                if isinstance(event, Message):
                    count += 1
                    if answer_on is None or count in answer_on:
                        tag = event.body[2]
                        conn.sendall(build_message(0, [-1]) if with_negative else monitoring_report(tag))

        server.read_until_closed(conn, on_data)
    return script


# ------------------------------------------------------------ default mode
def test_default_run_passes_every_check_and_is_fast():
    lines = []
    started = time.perf_counter()
    code = selfcheck.run_default(collect(lines))
    assert time.perf_counter() - started < 10.0
    text = "\n".join(lines)
    assert code == 0 and "VERDICT: PASS" in text
    for name in ("python", "tkinter", "control", "monitoring", "telecommand", "telemetry"):
        assert any(line.strip().startswith("PASS") and f" {name}:" in line for line in lines), (name, text)
    assert "FAIL" not in text and "does not prove anything about a real server" in text


class SilentMock:
    """Looks like a MockServer for one service but accepts and never answers."""

    def __init__(self, scripted):
        self.server = scripted()

    def start(self):
        pass

    @property
    def port(self):
        return self.server.port

    def stop(self):
        pass


def test_a_failing_service_gives_fail_and_exit_code_1(monkeypatch, scripted):
    real = MockServer

    def factory(service, **kwargs):
        return SilentMock(scripted) if service == "monitoring" else real(service, **kwargs)

    monkeypatch.setattr(selfcheck, "MockServer", factory)
    lines = []
    # The timeout also applies to the three healthy mock services, which must answer inside it even on a
    # loaded machine, so it is not tiny; only the silent service pays for all of it.
    assert selfcheck.run_default(collect(lines), timeout=2.0) == 1
    text = "\n".join(lines)
    assert "FAIL  monitoring: no matching reply within 2 s" in text
    assert "PASS  control" in text and "PASS  telecommand" in text and "PASS  telemetry" in text
    assert "VERDICT: FAIL (monitoring)" in text


def test_a_service_that_cannot_be_reached_is_reported_not_raised(monkeypatch):
    class Dead:
        port = 1                     # nothing listens on port 1

        def start(self): pass

        def stop(self): pass

    monkeypatch.setattr(selfcheck, "MockServer", lambda service, **kw: Dead())
    lines = []
    assert selfcheck.run_default(collect(lines), timeout=0.5) == 1
    assert "could not connect to the mock server" in "\n".join(lines)


def test_environment_checks(monkeypatch):
    assert all(c.ok for c in selfcheck.check_environment())
    monkeypatch.setattr(sys, "version_info", (3, 9, 1, "final", 0))
    old = selfcheck.check_environment()[0]
    assert old.name == "python" and not old.ok and "3.10 or newer" in old.detail
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "tkinter", None)
    broken = selfcheck.check_environment()
    assert broken[-1].name == "tkinter" and not broken[-1].ok and "cannot import tkinter" in broken[-1].detail


def test_a_broken_tcl_is_reported_with_the_environment_variables_to_check(monkeypatch):
    import tkinter

    def broken_tcl():
        raise tkinter.TclError("Can't find a usable init.tcl")

    monkeypatch.setattr(tkinter, "Tcl", broken_tcl)
    check = selfcheck.check_environment()[-1]
    assert not check.ok and "init.tcl" in check.detail and "TCL_LIBRARY" in check.detail


# ------------------------------------------------------------ --host: ONE read-only monitoring request
def test_host_mode_sends_exactly_one_monitoring_request_and_shows_the_words(scripted):
    server = scripted(replying())
    lines = []
    code = selfcheck.run_host("127.0.0.1", server.port - 3100, 3.0, collect(lines))
    text = "\n".join(lines)
    assert code == 0 and "RESULT: PASS: monitoring report" in text
    sent = messages_received_by(server)
    assert len(sent) == 1                                            # ONE message, nothing else
    assert sent[0].raw == build_monitoring_request(1, ProtocolOptions())
    assert sent[0].body[0] == 0 and sent[0].size == 28               # a monitoring request, never another kind
    assert "Sent (28 bytes, 7 words)" in text and "0x499602D2  1234567890" in text   # the exact words
    assert "Received message 1" in text and "Received message 1 (268 bytes, 67 words)" in text


def test_host_mode_silence_says_no_reply_within_s(scripted):
    server = scripted()                                              # accepts and discards
    lines = []
    code = selfcheck.run_host("127.0.0.1", server.port - 3100, 0.5, collect(lines))
    text = "\n".join(lines)
    assert code == 1 and "no reply within 0.5 s" in text and "RESULT: FAIL" in text
    assert server.wait_for_bytes(28)                                 # the server thread reads on its own schedule
    assert len(messages_received_by(server)) == 1 and "Sent (28 bytes" in text


def test_host_mode_a_negative_message_is_alive_but_not_a_pass(scripted):
    server = scripted(replying(with_negative=True))
    lines = []
    assert selfcheck.run_host("127.0.0.1", server.port - 3100, 3.0, collect(lines)) == 1
    text = "\n".join(lines)
    assert "NEGATIVE message" in text and "Monitoring flow ID" in text and "Received message 1 (20 bytes" in text


def test_host_mode_unreachable_port():
    lines = []
    assert selfcheck.run_host("127.0.0.1", closed_port() - 3100, 1.0, collect(lines)) == 1
    assert "could not connect" in "\n".join(lines) and "RESULT: FAIL" in "\n".join(lines)


def test_host_mode_never_uses_any_other_client():
    source = Path(selfcheck.__file__).read_text(encoding="utf-8")
    for name in ("ControlClient(", "TelecommandClient(", "TelemetryClient(", "set_parameters(", "send_bd(",
                 "send_ad(", "send_cltu(", "send_frame(", "send_nop(", "send_directive(", "subscribe(", ".start(0"):
        # the mock round trips legitimately use them; the real-server functions must not
        real_server_code = source[source.index("def monitoring_attempt"):source.index("# -----", source.index("def run_probe"))]
        assert name not in real_server_code, name


# ------------------------------------------------------------ --probe
def test_probe_tries_each_checksum_mode_once_and_states_its_limits(scripted):
    server = scripted(replying())
    lines = []
    code = selfcheck.run_probe("127.0.0.1", server.port - 3100, 3.0, collect(lines))
    text = "\n".join(lines)
    assert code == 0
    sent = messages_received_by(server)
    assert len(sent) == 3 and all(m.body[0] == 0 and m.size == 28 for m in sent)
    assert len({m.body[2] for m in sent}) == 3                       # three requests, three tags
    for mode in ("whole_packet", "excluding_postamble", "payload_only"):
        assert f"checksum mode {mode}: parseable reply" in text
    assert "Modes that got a parseable reply: whole_packet, excluding_postamble, payload_only" in text
    assert "only tried those checksum modes and says nothing about flow IDs" in text
    assert "identical except for the tag" in text and "no checksum field" in text


def test_probe_reports_which_modes_got_a_parseable_reply(scripted):
    server = scripted(replying(answer_on={2}))                       # only the second request is answered
    lines = []
    code = selfcheck.run_probe("127.0.0.1", server.port - 3100, 2.0, collect(lines))   # the answered one must not miss it
    text = "\n".join(lines)
    assert code == 0
    assert "checksum mode whole_packet: no parseable reply" in text
    assert "checksum mode excluding_postamble: parseable reply" in text
    assert "checksum mode payload_only: no parseable reply" in text
    assert "Modes that got a parseable reply: excluding_postamble" in text


def test_probe_with_no_reply_prints_what_to_try_next_and_the_limits(scripted):
    server = scripted()
    lines = []
    code = selfcheck.run_probe("127.0.0.1", server.port - 3100, 0.5, collect(lines))
    text = "\n".join(lines)
    assert code == 1 and "Modes that got a parseable reply: none" in text
    assert "Protocol options to try next" in text and "Monitoring flow ID" in text
    assert "only tried those checksum modes and says nothing about flow IDs" in text
    assert server.wait_for_bytes(3 * 28)                             # the server thread reads on its own schedule
    assert len(messages_received_by(server)) == 3


def test_probe_cannot_connect():
    lines = []
    assert selfcheck.run_probe("127.0.0.1", closed_port() - 3100, 1.0, collect(lines)) == 1
    assert "could not connect" in "\n".join(lines)


def test_the_probe_modes_are_the_modes_of_the_protocol_options():
    assert selfcheck.CHECKSUM_MODES == protocol._CHECKSUM_MODES


# ------------------------------------------------------------ command line
def run_main(*argv):
    return selfcheck.main(list(argv))


@pytest.mark.parametrize("argv", [
    ["--probe"], ["--host", ""], ["--host", "   "], ["--host", "h", "--timeout", "0.1"],
    ["--host", "h", "--timeout", "301"], ["--host", "h", "--timeout", "5_0"], ["--host", "h", "--timeout", "-1"],
    ["--host", "h", "--timeout", "nan"], ["--host", "h", "--timeout", "1e1"], ["--host", "h", "--timeout", "abc"],
    ["--host", "h", "--timeout", ".5"], ["--host", "h", "--port-offset", "-1"],
    ["--host", "h", "--port-offset", "1e3"], ["--host", "h", "--port-offset", "60001"],
    ["--host", "h", "--port-offset", "7_0"], ["--host", "h", "--port-offset", "٥"],
    ["--host", "h", "--timeout", "٥"],
])
def test_bad_arguments_exit_with_code_2(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        run_main(*argv)
    assert caught.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_main_routes_the_modes(monkeypatch):
    calls = []
    monkeypatch.setattr(selfcheck, "run_default", lambda: calls.append("default") or 0)
    monkeypatch.setattr(selfcheck, "run_host", lambda h, o, t: calls.append(("host", h, o, t)) or 0)
    monkeypatch.setattr(selfcheck, "run_probe", lambda h, o, t: calls.append(("probe", h, o, t)) or 0)
    assert run_main() == 0
    assert run_main("--host", "10.0.0.5", "--port-offset", "100", "--timeout", "2.5") == 0
    assert run_main("--host", "10.0.0.5", "--probe") == 0
    assert calls == ["default", ("host", "10.0.0.5", 100, 2.5), ("probe", "10.0.0.5", 0, 5.0)]


# ------------------------------------------------------------ as a program
def test_it_runs_as_a_module_in_under_ten_seconds():
    started = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "groundstation.selfcheck"], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert time.perf_counter() - started < 10.0
    assert done.returncode == 0 and "VERDICT: PASS" in done.stdout


def test_importing_it_loads_no_gui_code():
    code = ("import sys, groundstation.selfcheck; "
            "bad = sorted(m for m in sys.modules if m == 'tkinter' or m.startswith('tkinter.') "
            "or m in ('groundstation.guibase', 'groundstation.gui', 'groundstation.app') "
            "or m.startswith('groundstation.tab_')); print(bad)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stdout + done.stderr


def test_the_help_text_is_plain():
    done = subprocess.run([sys.executable, "-m", "groundstation.selfcheck", "--help"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "usage: python -m groundstation.selfcheck" in done.stdout
    assert "ONE read-only monitoring request" in done.stdout
