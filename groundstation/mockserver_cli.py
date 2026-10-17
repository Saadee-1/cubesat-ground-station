"""Run all four MOCK services on local ports (a stand-in for the real servers).

    python -m groundstation.mockserver_cli [--host 127.0.0.1] [--port-offset 0]
                                           [--config PATH] [--rate 20]

Prints one "READY <service> <host>:<port>" line per service, then "ALL READY",
then waits until Ctrl+C or SIGTERM. This is a test double, not the real CORTEX
or COP-1 software (see mockserver.py).
"""
import argparse
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from .mockserver import MockServer
from .protocol import DEFAULT_PORTS

PACKAGE_DIR = Path(__file__).resolve().parent
# Same order as the tabs: control, monitoring, telecommand, telemetry.
SERVICES = ("control", "monitoring", "telecommand", "telemetry")


def resolve_config_path(arg: str) -> Path:
    """Same rule as groundstation.app: the path as given, else relative to the package."""
    path = Path(arg)
    if not path.exists() and (PACKAGE_DIR / arg).exists():
        return PACKAGE_DIR / arg
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the four mock services locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="added to each default port (control 3101, monitoring 3100, "
                             "telecommand 3120, telemetry 3070)")
    parser.add_argument("--config", default="samples/sample_config_10.json",
                        help="parameter file used for the telemetry frames")
    parser.add_argument("--rate", type=float, default=20.0, help="telemetry messages per second")
    args = parser.parse_args(argv)
    if args.rate <= 0:
        parser.error("--rate must be greater than 0")

    stop = threading.Event()

    def request_stop(signum, frame) -> None:  # noqa: ARG001
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK exists on Windows only
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_stop)

    servers = []
    shared_params: dict = {}  # one dict for all four services, so control values show up in monitoring
    try:
        for service in SERVICES:
            server = MockServer(service, host=args.host, port=DEFAULT_PORTS[service] + args.port_offset,
                                config_path=str(resolve_config_path(args.config)),
                                frame_rate_hz=args.rate, shared_params=shared_params)
            server.start()
            servers.append(server)
            print(f"READY {service} {args.host}:{server.port}", flush=True)
        print("ALL READY", flush=True)
        while not stop.wait(0.2):
            pass
    except (OSError, ValueError) as exc:  # a busy port, or an unreadable config file
        print(f"ERROR could not start the mock services: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        for server in servers:
            server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
