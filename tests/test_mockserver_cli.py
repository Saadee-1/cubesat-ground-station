"""Run `python -m groundstation.mockserver_cli` as a real subprocess."""
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from groundstation.clients import ControlClient, MonitoringClient, TelemetryClient
from groundstation.cop1 import ControlAck, MonitoringReport
from groundstation.cortex import TelemetryMessage
from groundstation.net import ConnState, Received
from groundstation.protocol import to_u32

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_PORTS = {"control": 3101, "monitoring": 3100, "telecommand": 3120, "telemetry": 3070}


def find_free_offset():
    """First offset for which all four shifted default ports can be bound right now."""
    for offset in range(10000, 60000, 37):
        socks = []
        try:
            for port in BASE_PORTS.values():
                sock = socket.socket()
                sock.bind(("127.0.0.1", port + offset))
                socks.append(sock)
            return offset
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
    raise RuntimeError("no free port block found")


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def parsed_of(client, kind, sink):
    for event in client.drain():
        if isinstance(event, Received):
            sink.append(event.parsed)
    return [p for p in sink if isinstance(p, kind)]


def test_cli_starts_four_services_answers_each_and_exits_on_terminate():
    offset = find_free_offset()
    proc = subprocess.Popen(
        [sys.executable, "-m", "groundstation.mockserver_cli", "--port-offset", str(offset)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = queue.Queue()

    def read_stdout():
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))

    threading.Thread(target=read_stdout, daemon=True).start()
    clients = []
    try:
        seen = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "ALL READY" not in seen:
            try:
                seen.append(lines.get(timeout=0.1))
            except queue.Empty:
                assert proc.poll() is None, f"mock CLI exited early: {proc.stderr.read()}"
        assert seen[-1] == "ALL READY", seen
        assert seen[:-1] == [f"READY {name} 127.0.0.1:{port + offset}" for name, port in BASE_PORTS.items()]

        control = ControlClient(port=BASE_PORTS["control"] + offset)
        monitoring = MonitoringClient(port=BASE_PORTS["monitoring"] + offset)
        telemetry = TelemetryClient(port=BASE_PORTS["telemetry"] + offset)
        clients.extend([control, monitoring, telemetry])
        for client in clients:
            client.connect()
            assert wait_for(lambda c=client: c.state == ConnState.CONNECTED), client.drain()
        sinks = {id(c): [] for c in clients}
        tag = control.set_parameters({0: 1023})
        monitoring.request()
        telemetry.start(data_flow=1)
        assert wait_for(lambda: parsed_of(control, ControlAck, sinks[id(control)]))
        assert parsed_of(control, ControlAck, sinks[id(control)])[0].tag == tag
        assert wait_for(lambda: parsed_of(monitoring, MonitoringReport, sinks[id(monitoring)]))
        assert wait_for(lambda: parsed_of(telemetry, TelemetryMessage, sinks[id(telemetry)]))
    finally:
        for client in clients:
            client.disconnect()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("the mock CLI did not exit within 5 s of terminate()")
    assert proc.returncode is not None


def test_cli_reports_a_busy_port_and_exits_with_an_error():
    offset = find_free_offset()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", BASE_PORTS["monitoring"] + offset))
    blocker.listen(1)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "groundstation.mockserver_cli", "--port-offset", str(offset)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=20)
    finally:
        blocker.close()
    assert proc.returncode == 2
    assert "ALL READY" not in proc.stdout
    assert "READY control" in proc.stdout  # the first service had started before the failure
    assert "ERROR could not start the mock services" in proc.stderr


def test_cli_services_share_state_control_values_appear_in_monitoring():
    offset = find_free_offset()
    proc = subprocess.Popen(
        [sys.executable, "-m", "groundstation.mockserver_cli", "--port-offset", str(offset)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l.rstrip("\n")) for l in proc.stdout], daemon=True).start()
    clients = []
    try:
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline and not ready:
            try:
                ready = lines.get(timeout=0.1) == "ALL READY"
            except queue.Empty:
                assert proc.poll() is None, f"mock CLI exited early: {proc.stderr.read()}"
        assert ready
        control = ControlClient(port=BASE_PORTS["control"] + offset)
        monitoring = MonitoringClient(port=BASE_PORTS["monitoring"] + offset)
        clients.extend([control, monitoring])
        for client in clients:
            client.connect()
            assert wait_for(lambda c=client: c.state == ConnState.CONNECTED)
        ack_sink, report_sink = [], []
        control.set_parameters({4: 0xC0346D0F, 3: 0x00010BFE})
        assert wait_for(lambda: parsed_of(control, ControlAck, ack_sink))
        monitoring.request()
        assert wait_for(lambda: parsed_of(monitoring, MonitoringReport, report_sink))
        report = parsed_of(monitoring, MonitoringReport, report_sink)[0]
        assert to_u32(report.config[4]) == 0xC0346D0F
        assert to_u32(report.config[3]) == 0x00010BFE
        assert report.tm_port() == (1, 3070)
    finally:
        for client in clients:
            client.disconnect()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
