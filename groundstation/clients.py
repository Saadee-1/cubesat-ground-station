"""Service clients: one TcpClient subclass per COP-1 / CORTEX service.

Each method builds a request with the protocol-layer builders and sends it.
Builders raise ProtocolError for invalid arguments; the clients do not catch
it, so the GUI can show the message.
"""
import threading
from typing import Callable, Optional

from . import cop1, cortex
from .net import TcpClient
from .protocol import DEFAULT_PORTS, Message, ProtocolOptions

_U32_MASK = 0xFFFFFFFF


class ServiceClient(TcpClient):
    """TcpClient plus protocol options and a tag counter."""

    SERVICE = ""  # key into protocol.DEFAULT_PORTS, set by each subclass
    DEFAULT_PARSE: Callable[[Message], object] = staticmethod(lambda msg: msg)

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None,
                 opts: Optional[ProtocolOptions] = None,
                 parse: Optional[Callable[[Message], object]] = None, **tcp_kwargs) -> None:
        super().__init__(self.SERVICE, host,
                         DEFAULT_PORTS[self.SERVICE] if port is None else port,
                         parse or self.DEFAULT_PARSE, **tcp_kwargs)
        self._opts = opts or ProtocolOptions()
        self._opts_lock = threading.Lock()
        self._tag_lock = threading.Lock()
        self._next_tag = 1

    @property
    def opts(self) -> ProtocolOptions:
        """The current (frozen) options; replace the whole object to change them."""
        with self._opts_lock:
            return self._opts

    @opts.setter
    def opts(self, value: ProtocolOptions) -> None:
        with self._opts_lock:
            self._opts = value

    def next_tag(self) -> int:
        """Unsigned 32-bit counter starting at 1, wrapping to 0 after 0xFFFFFFFF."""
        with self._tag_lock:
            tag = self._next_tag
            self._next_tag = (self._next_tag + 1) & _U32_MASK
            return tag


class ControlClient(ServiceClient):
    SERVICE = "control"
    DEFAULT_PARSE = staticmethod(cop1.parse_control_response)

    def set_parameters(self, params: "dict[int, int]") -> int:
        """Send a control request; returns its tag."""
        tag = self.next_tag()
        raw = cop1.build_control_request(tag, params, self.opts)
        self.send(raw, f"control request {len(params)} parameter(s)", tag)
        return tag


class MonitoringClient(ServiceClient):
    SERVICE = "monitoring"
    DEFAULT_PARSE = staticmethod(cop1.parse_monitoring_response)

    def request(self) -> int:
        """Send a monitoring request; returns its tag."""
        tag = self.next_tag()
        self.send(cop1.build_monitoring_request(tag, self.opts), "monitoring request", tag)
        return tag


class TelecommandClient(ServiceClient):
    SERVICE = "telecommand"
    DEFAULT_PARSE = staticmethod(cop1.parse_tc_response)

    def send_bd(self, vcid: int, data: bytes, validate: bool = True) -> int:
        tag = self.next_tag()
        raw = cop1.build_bd_segment(vcid, tag, data, self.opts, validate)
        self.send(raw, f"BD segment vcid={vcid} {len(data)} bytes", tag)
        return tag

    def send_ad(self, vcid: int, data: bytes, validate: bool = True) -> int:
        tag = self.next_tag()
        raw = cop1.build_ad_segment(vcid, tag, data, self.opts, validate)
        self.send(raw, f"AD segment vcid={vcid} {len(data)} bytes", tag)
        return tag

    def send_cltu(self, vcid: int, data: bytes, validate: bool = True) -> int:
        tag = self.next_tag()
        raw = cop1.build_cltu(vcid, tag, data, self.opts, validate)
        self.send(raw, f"CLTU vcid={vcid} {len(data)} bytes", tag)
        return tag

    def send_frame(self, codeblock_length: int, data: bytes, validate: bool = True) -> int:
        tag = self.next_tag()
        raw = cop1.build_transfer_frame(codeblock_length, tag, data, self.opts, validate)
        self.send(raw, f"Transfer Frame codeblock={codeblock_length} {len(data)} bytes", tag)
        return tag

    def send_nop(self, vcid: int) -> int:
        tag = self.next_tag()
        self.send(cop1.build_nop(vcid, tag, self.opts), f"NOP vcid={vcid}", tag)
        return tag

    def send_directive(self, op: int, vcid: int, param: int) -> int:
        tag = self.next_tag()
        raw = cop1.build_directive(op, vcid, tag, param, self.opts)
        name = cop1.DIRECTIVES.get(op, str(op))
        self.send(raw, f"directive {op} ({name}) vcid={vcid} param={param}", tag)
        return tag

    def subscribe(self, vcid: int, flow: int, authorization: int) -> int:
        tag = self.next_tag()
        raw = cop1.build_output_flow_subscription(vcid, tag, flow, authorization, self.opts)
        self.send(raw, f"subscribe vcid={vcid} flow={flow} authorization={authorization}", tag)
        return tag


class TelemetryClient(ServiceClient):
    SERVICE = "telemetry"
    DEFAULT_PARSE = staticmethod(cortex.parse_tm_message)

    def start(self, channel: int = 0, buffered_blocks: int = 0, data_flow: int = 0,
              frame_mask: int = 0, expected_value: int = 0) -> None:
        """Send a telemetry request; it has no tag."""
        raw = cortex.build_tm_request(channel, buffered_blocks, data_flow, self.opts,
                                      frame_mask, expected_value)
        self.send(raw, f"TM start channel={cortex.CHANNELS[channel]} data_flow={data_flow}", None)

    def stop(self, channel: int = 0) -> None:
        """Send a stop request (a telemetry request with data flow 0x80)."""
        raw = cortex.build_stop_request(channel, self.opts)
        self.send(raw, f"TM stop channel={cortex.CHANNELS[channel]}", None)
