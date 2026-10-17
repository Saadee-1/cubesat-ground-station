"""Diagnostic tool, no GUI, standard library only:  python -m groundstation.selfcheck

Default (no --host): a 10 second "does this install work on this machine" check. It starts the four
mock servers in this process, does one round trip per service with the real clients, and prints
PASS or FAIL per check and a verdict. Exit code 0 when everything passed, else 1.

--host H [--port-offset N] [--timeout S]: talks to a REAL server and sends ONLY the monitoring request
(read only). It prints the exact words sent and received, or "no reply within S s". It never sends a
control, telecommand or telemetry request.

--probe (needs --host): the same read-only monitoring request once per checksum mode.
The monitoring request has no checksum field, so those requests are identical
except for the tag; see the note the tool prints. It says nothing about flow IDs.
"""
import argparse
import dataclasses
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .clients import ControlClient, MonitoringClient, TelecommandClient, TelemetryClient
from .cop1 import AcceptReject, ControlAck, MonitoringReport, NegativeResponse, UnknownResponse
from .cortex import TelemetryMessage, TelemetryStopAck
from .mockserver import MockServer
from .net import ClientError, ConnState, ParseFailure, Received, Sent, StateChanged, StreamProblem, describe_words
from .protocol import DEFAULT_PORTS, ProtocolOptions

MIN_PYTHON = (3, 10)
DEFAULT_TIMEOUT_S = 5.0
CHECKSUM_MODES = ("whole_packet", "excluding_postamble", "payload_only")  # the choices of the Protocol options
PROBE_NOTE = ("This probe only tried those checksum modes and says nothing about flow IDs. "
              "The monitoring request has no checksum field, so the requests above are "
              "identical except for the tag: a reply to one is a reply to all, and silence for all means "
              "the monitoring service did not answer, not that a checksum mode is wrong. "
              "The checksum only appears in telecommand data requests, which this tool never sends.")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _poll(predicate: Callable[[], bool], timeout: float, step: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _connect(client, timeout: float) -> Optional[str]:
    """Connect and wait; returns None when connected, else the reason."""
    try:
        client.connect()
    except ClientError as exc:
        return str(exc)
    if _poll(lambda: client.state in (ConnState.CONNECTED, ConnState.ERROR, ConnState.DISCONNECTED), timeout):
        if client.state == ConnState.CONNECTED:
            return None
        return f"state {client.state.value}"
    return f"no connection within {timeout:g} s"


def _wait_event(client, wanted: Callable[[object], bool], timeout: float, seen: Optional[list] = None):
    """Drain the client's events until `wanted(event)` is true; returns that event or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in client.drain():
            if seen is not None:
                seen.append(event)
            if wanted(event):
                return event
        time.sleep(0.005)
    return None


# ---------------------------------------------------------------- environment
def check_environment() -> "list[Check]":
    """Python version, tkinter and the Tcl interpreter. No window is opened."""
    checks = []
    version = sys.version_info
    ok = tuple(version[:2]) >= MIN_PYTHON
    checks.append(Check("python", ok, f"{version[0]}.{version[1]}.{version[2]} "
                                      f"({MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required; that is the version this was tested with)"))
    try:
        import tkinter
    except ImportError as exc:
        checks.append(Check("tkinter", False, f"cannot import tkinter: {exc}"))
        return checks
    try:
        tcl = tkinter.Tcl()
        checks.append(Check("tkinter", True, f"imports; Tcl {tcl.eval('info patchlevel')} runs (no window was opened)"))
    except Exception as exc:  # noqa: BLE001 - any Tcl failure is exactly what this check is for
        checks.append(Check("tkinter", False, f"imports but the Tcl interpreter failed: {exc}. "
                                              f"Check TCL_LIBRARY and TK_LIBRARY (they must be unset or point "
                                              f"at this Python's tcl folder)."))
    return checks


# ---------------------------------------------------------------- mock round trips
def _round_trip(service: str, timeout: float) -> Check:
    """One request and its reply between a real client and an in-process mock server."""
    kwargs = {"frame_rate_hz": 50.0} if service == "telemetry" else {}
    mock = MockServer(service, **kwargs)
    mock.start()
    client_class = {"control": ControlClient, "monitoring": MonitoringClient,
                    "telecommand": TelecommandClient, "telemetry": TelemetryClient}[service]
    client = client_class(port=mock.port)
    started = time.perf_counter()
    try:
        problem = _connect(client, timeout)
        if problem:
            return Check(service, False, f"could not connect to the mock server: {problem}")
        if service == "control":
            tag = client.set_parameters({1: 1})
            event = _wait_event(client, lambda e: isinstance(e, Received) and isinstance(e.parsed, ControlAck)
                                and e.parsed.tag == tag, timeout)
            what = "control request answered with a ControlAck"
        elif service == "monitoring":
            tag = client.request()
            event = _wait_event(client, lambda e: isinstance(e, Received) and isinstance(e.parsed, MonitoringReport)
                                and e.parsed.tag == tag, timeout)
            what = "monitoring request answered with a MonitoringReport"
        elif service == "telecommand":
            tag = client.send_nop(0)
            event = _wait_event(client, lambda e: isinstance(e, Received) and isinstance(e.parsed, AcceptReject)
                                and e.parsed.tag == tag, timeout)
            what = "NOP answered with an Accept"
        else:
            client.start(0, 0, 0)
            event = _wait_event(client, lambda e: isinstance(e, Received) and isinstance(e.parsed, TelemetryMessage),
                                timeout)
            if event is not None:
                client.stop(0)
                event = _wait_event(client, lambda e: isinstance(e, Received) and isinstance(e.parsed, TelemetryStopAck),
                                    timeout)
            what = "telemetry started, a frame arrived, and Stop was acknowledged"
        elapsed = (time.perf_counter() - started) * 1000
        if event is None:
            return Check(service, False, f"no matching reply within {timeout:g} s")
        return Check(service, True, f"{what} ({elapsed:.0f} ms)")
    except Exception as exc:  # noqa: BLE001 - a failing check must be reported, not crash the tool
        return Check(service, False, f"{type(exc).__name__}: {exc}")
    finally:
        client.disconnect()
        mock.stop()


def run_default(out: Callable[[str], None] = print, timeout: float = 3.0) -> int:
    started = time.perf_counter()
    out("groundstation self check: this installation against built-in mock servers (no real server is contacted)")
    checks = check_environment()
    for service in ("control", "monitoring", "telecommand", "telemetry"):
        checks.append(_round_trip(service, timeout))
    for check in checks:
        out(f"  {'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    failed = [c.name for c in checks if not c.ok]
    elapsed = time.perf_counter() - started
    if failed:
        out(f"VERDICT: FAIL ({', '.join(failed)}) in {elapsed:.1f} s")
        return 1
    out(f"VERDICT: PASS (all {len(checks)} checks) in {elapsed:.1f} s. "
        f"This proves the program runs here; it does not prove anything about a real server.")
    return 0


# ---------------------------------------------------------------- a real server: monitoring only
@dataclass
class Attempt:
    connected: bool
    problem: str                       # why there is no result, "" when a message came back
    sent: Optional[bytes]
    received: "list[Received]"
    other: "list[str]"                 # stream problems and parse failures seen on the way


def monitoring_attempt(host: str, port: int, timeout: float, opts: ProtocolOptions) -> Attempt:
    """Connect, send ONE monitoring request, wait for a reply. Never sends anything else."""
    client = MonitoringClient(host, port, opts)
    try:
        problem = _connect(client, timeout)
        if problem:
            return Attempt(False, f"could not connect to {host}:{port}: {problem}", None, [], [])
        return _request_on(client, timeout)
    finally:
        client.disconnect()


def _request_on(client: MonitoringClient, timeout: float) -> Attempt:
    seen: list = []
    try:
        client.request()
    except ClientError as exc:
        return Attempt(True, f"could not send the request: {exc}", None, [], [])
    _wait_event(client, lambda e: isinstance(e, (Received, ParseFailure)), timeout, seen)
    sent = next((e.raw for e in seen if isinstance(e, Sent)), None)
    if sent is None:  # the Sent event is queued before send() returns, so it is in the first drain
        sent = next((e.raw for e in client.drain() if isinstance(e, Sent)), None)
    received = [e for e in seen if isinstance(e, Received)]
    other = [f"stream problem: {e.error.reason}, {e.error.discarded} byte(s) discarded"
             for e in seen if isinstance(e, StreamProblem)]
    other += [f"parse failure: {e.error}" for e in seen if isinstance(e, ParseFailure)]
    problem = "" if received else f"no reply within {timeout:g} s"
    return Attempt(True, problem, sent, received, other)


def _show_words(out: Callable[[str], None], title: str, raw: bytes) -> None:
    out(f"  {title} ({len(raw)} bytes, {len(raw) // 4} words):")
    for line in describe_words(raw):
        out(f"    {line}")


def _verdict_of(attempt: Attempt) -> "tuple[bool, str]":
    """(counts as a parseable reply, one line saying what came back)."""
    if not attempt.received:
        return False, attempt.problem
    parsed = attempt.received[0].parsed
    if isinstance(parsed, MonitoringReport):
        return True, f"monitoring report (tag {parsed.tag})"
    if isinstance(parsed, NegativeResponse):
        return False, ("the server answered with the NEGATIVE message (-1): it is alive but rejected the request; "
                       "check the Monitoring flow ID in Protocol options")
    if isinstance(parsed, UnknownResponse):
        return False, f"a reply that is not a monitoring report ({parsed.reason}); check the Monitoring flow ID"
    return False, f"unexpected reply type {type(parsed).__name__}"


def run_host(host: str, port_offset: int, timeout: float, out: Callable[[str], None] = print) -> int:
    port = DEFAULT_PORTS["monitoring"] + port_offset
    opts = ProtocolOptions()
    out(f"Target: {host}:{port} (monitoring service), monitoring flow ID {opts.monitoring_flow_id}, "
        f"timeout {timeout:g} s. Read only: ONE monitoring request, nothing else.")
    attempt = monitoring_attempt(host, port, timeout, opts)
    if attempt.sent is not None:
        _show_words(out, "Sent", attempt.sent)
    for number, received in enumerate(attempt.received, 1):
        _show_words(out, f"Received message {number}", received.message.raw)
    for line in attempt.other:
        out(f"  {line}")
    ok, text = _verdict_of(attempt)
    if not attempt.received:
        out(f"  {attempt.problem}")
    out(f"RESULT: {'PASS' if ok else 'FAIL'}: {text}")
    return 0 if ok else 1


def run_probe(host: str, port_offset: int, timeout: float, out: Callable[[str], None] = print) -> int:
    port = DEFAULT_PORTS["monitoring"] + port_offset
    base = ProtocolOptions()
    out(f"PROBE (read only) of {host}:{port}: one monitoring request per checksum mode, using the configured "
        f"flow IDs (monitoring flow ID {base.monitoring_flow_id}), timeout {timeout:g} s each.")
    client = MonitoringClient(host, port, base)
    answered = []
    try:
        problem = _connect(client, timeout)
        if problem:
            out(f"  could not connect to {host}:{port}: {problem}")
            out("RESULT: FAIL: no connection, nothing was probed")
            return 1
        for mode in CHECKSUM_MODES:
            client.opts = dataclasses.replace(base, checksum_mode=mode)
            attempt = _request_on(client, timeout)
            ok, text = _verdict_of(attempt)
            out(f"  checksum mode {mode}: {'parseable reply' if ok else 'no parseable reply'} - {text}")
            if ok:
                answered.append(mode)
    finally:
        client.disconnect()
    out(f"Modes that got a parseable reply: {', '.join(answered) if answered else 'none'}")
    out(f"NOTE: {PROBE_NOTE}")
    if not answered:
        out("Protocol options to try next: Monitoring flow ID (now "
            f"{base.monitoring_flow_id}; use the value the server's documentation or its operator gives), then check "
            "host, port and --port-offset. Checksum mode, byte numbering and FEC do not change this request.")
        out("RESULT: FAIL: no mode got a parseable reply")
        return 1
    out("RESULT: PASS: the monitoring service answers (this says nothing about the checksum mode)")
    return 0


# ---------------------------------------------------------------- command line
_ASCII_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


def _timeout_arg(text: str) -> float:
    if not _ASCII_NUMBER.fullmatch(text.strip()):
        raise argparse.ArgumentTypeError("must be a number of seconds such as 5 or 2.5")
    value = float(text)
    if not 0.5 <= value <= 300:
        raise argparse.ArgumentTypeError("must be from 0.5 to 300 seconds")
    return value


def _offset_arg(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text.strip()):
        raise argparse.ArgumentTypeError("must be a whole number from 0 to 60000")
    value = int(text)
    if not 0 <= value <= 60000:
        raise argparse.ArgumentTypeError("must be a whole number from 0 to 60000")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m groundstation.selfcheck",
        description="Without --host: check this installation against built-in mock servers. "
                    "With --host: send ONE read-only monitoring request to a real server.")
    parser.add_argument("--host", help="a real server; only the monitoring request is ever sent")
    parser.add_argument("--port-offset", type=_offset_arg, default=0,
                        help="added to the default monitoring port 3100 (default 0)")
    parser.add_argument("--timeout", type=_timeout_arg, default=DEFAULT_TIMEOUT_S,
                        help="seconds to wait for a reply, 0.5 to 300 (default 5)")
    parser.add_argument("--probe", action="store_true",
                        help="with --host: one read-only monitoring request per checksum mode")
    args = parser.parse_args(argv)
    if args.probe and not args.host:
        parser.error("--probe needs --host")
    if args.host is not None and not args.host.strip():
        parser.error("--host must not be empty")
    if args.host is None:
        return run_default()
    if args.probe:
        return run_probe(args.host.strip(), args.port_offset, args.timeout)
    return run_host(args.host.strip(), args.port_offset, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
