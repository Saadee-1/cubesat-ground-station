"""Tests for groundstation.cop1 (builders, helpers, response parsers)."""
import struct

import pytest

from groundstation import cop1
from groundstation.cop1 import (
    AcceptReject, AdditionalInfo, AlertIndication, CltuEcho, ConfirmResponse,
    ControlAck, MonitoringReport, NegativeResponse, SegmentEcho,
    SuspendIndication, UnknownResponse)
from groundstation.protocol import (ProtocolError, ProtocolOptions,
                                    parse_message)

START, POST = 1234567890, -1234567890


def pack(*words):
    """Independent big-endian packer (does not use the code under test)."""
    return struct.pack(f">{len(words)}I", *[w & 0xFFFFFFFF for w in words])


def server_message(flow, body):
    """Build a server reply with struct only, then parse it with parse_message."""
    words = [START, 4 * (len(body) + 4), flow, *body, POST]
    return parse_message(pack(*words))


def words_of(raw):
    return struct.unpack(f">{len(raw) // 4}I", raw)


OPTS = ProtocolOptions()
BD_DATA = bytes.fromhex("deadbeef01")
BD_VECTORS = {
    "whole_packet": "499602d20000002c000000000000001e000000030000000900000005deadbeef01000000205240b6b669fd2e",
    "excluding_postamble": "499602d20000002c000000000000001e000000030000000900000005deadbeef01000000d6bc3de4b669fd2e",
    "payload_only": "499602d20000002c000000000000001e000000030000000900000005deadbeef01000000205240e2b669fd2e",
}


# ------------------------------------------------------------ vectors
def test_monitoring_request_vector():
    assert cop1.build_monitoring_request(7, OPTS) == bytes.fromhex(
        "499602d20000001c00000000000000000000000000000007b669fd2e")


def test_control_request_vector():
    raw = cop1.build_control_request(5, {0: 1023, 2: 1}, OPTS)
    assert raw == bytes.fromhex(
        "499602d200000030000000010000000100000000000000050000000200000000"
        "000003ff0000000200000001b669fd2e")
    assert len(raw) == 48


@pytest.mark.parametrize("mode", list(BD_VECTORS))
def test_bd_segment_vectors(mode):
    opts = ProtocolOptions(checksum_mode=mode)
    raw = cop1.build_bd_segment(3, 9, BD_DATA, opts)
    assert raw == bytes.fromhex(BD_VECTORS[mode])
    assert len(raw) == 44


def test_flow_ids_come_from_options():
    opts = ProtocolOptions(control_flow_id=11, monitoring_flow_id=12,
                           telecommand_flow_id=13)
    assert words_of(cop1.build_control_request(1, {}, opts))[2] == 11
    assert words_of(cop1.build_monitoring_request(1, opts))[2] == 12
    assert words_of(cop1.build_bd_segment(0, 1, b"x", opts))[2] == 13
    assert words_of(cop1.build_nop(0, 1, opts))[2] == 13


# ------------------------------------------------------------ sizes
def test_exact_sizes():
    assert len(cop1.build_nop(1, 2, OPTS)) == 32
    assert len(cop1.build_output_flow_subscription(1, 2, 3, 1, OPTS)) == 36
    assert len(cop1.build_directive(2, 1, 2, 0, OPTS)) == 32
    assert len(cop1.build_transfer_frame(0, 1, b"\x01" * 6, OPTS)) == 44
    assert len(cop1.build_cltu(1, 1, b"\x01", OPTS)) == 40
    assert len(cop1.build_ad_segment(1, 1, b"\x01", OPTS)) == 40
    # n data bytes -> ceil(n/4) data words
    assert len(cop1.build_cltu(1, 1, b"\x01" * 1284, OPTS)) == 4 * (3 + 4 + 321 + 1 + 1)


def test_nop_directive_and_subscription_bodies():
    assert words_of(cop1.build_nop(4, 9, OPTS))[3:-1] == (13, 4, 9, 0)
    assert words_of(cop1.build_directive(8, 5, 6, 77, OPTS))[3:-1] == (8, 5, 6, 77)
    assert words_of(cop1.build_output_flow_subscription(4, 9, 6, 2, OPTS))[3:-1] == (14, 4, 9, 6, 2)


def test_data_request_layouts_and_checksum_sums_to_zero():
    for raw, op, field4 in [
        (cop1.build_ad_segment(2, 8, b"abc", OPTS), 20, 2),
        (cop1.build_bd_segment(2, 8, b"abc", OPTS), 30, 2),
        (cop1.build_cltu(2, 8, b"abc", OPTS), 40, 2),
        (cop1.build_transfer_frame(7, 8, b"abcdef", OPTS), 50, 7),
    ]:
        words = words_of(raw)
        assert words[3:7] == (op, field4, 8, 3 if op != 50 else 6)
        assert sum(words) % 2 ** 32 == 0  # whole_packet checksum


def test_padding_bytes_are_zero():
    words = words_of(cop1.build_bd_segment(0, 1, b"\xff", OPTS))
    assert words[7] == 0xFF000000


# ------------------------------------------------------------ limits
@pytest.mark.parametrize("builder", [cop1.build_bd_segment, cop1.build_ad_segment])
def test_segment_limits(builder):
    builder(0, 0, b"x" * 1, OPTS)
    builder(63, 2 ** 32 - 1, b"x" * 1019, OPTS)
    for bad in (0, 1020):
        with pytest.raises(ProtocolError):
            builder(0, 0, b"x" * bad, OPTS)
    fec = ProtocolOptions(fec_in_frame=True)
    builder(0, 0, b"x" * 1017, fec)
    with pytest.raises(ProtocolError):
        builder(0, 0, b"x" * 1018, fec)


def test_cltu_limits():
    cop1.build_cltu(0, 0, b"x", OPTS)
    cop1.build_cltu(0, 0, b"x" * 1284, OPTS)
    for bad in (0, 1285):
        with pytest.raises(ProtocolError):
            cop1.build_cltu(0, 0, b"x" * bad, OPTS)


def test_transfer_frame_limits():
    cop1.build_transfer_frame(0, 0, b"x" * 6, OPTS)
    cop1.build_transfer_frame(8, 0, b"x" * 1024, OPTS)
    for bad in (5, 1025):
        with pytest.raises(ProtocolError):
            cop1.build_transfer_frame(0, 0, b"x" * bad, OPTS)
    for cb in (0, 5, 6, 7, 8):
        cop1.build_transfer_frame(cb, 0, b"x" * 6, OPTS)
    for cb in (1, 4, 9, -1):
        with pytest.raises(ProtocolError):
            cop1.build_transfer_frame(cb, 0, b"x" * 6, OPTS)


@pytest.mark.parametrize("call", [
    lambda v: cop1.build_bd_segment(v, 0, b"x", OPTS),
    lambda v: cop1.build_ad_segment(v, 0, b"x", OPTS),
    lambda v: cop1.build_cltu(v, 0, b"x", OPTS),
    lambda v: cop1.build_nop(v, 0, OPTS),
    lambda v: cop1.build_directive(2, v, 0, 0, OPTS),
    lambda v: cop1.build_output_flow_subscription(v, 0, 0, 0, OPTS),
])
def test_vcid_range(call):
    call(0)
    call(63)
    for bad in (-1, 64):
        with pytest.raises(ProtocolError):
            call(bad)


def test_tag_range():
    cop1.build_nop(0, 0, OPTS)
    cop1.build_nop(0, 2 ** 32 - 1, OPTS)
    for bad in (-1, 2 ** 32):
        with pytest.raises(ProtocolError):
            cop1.build_nop(0, bad, OPTS)
        with pytest.raises(ProtocolError):
            cop1.build_monitoring_request(bad, OPTS)


def test_validate_false_bypasses_length_limits():
    assert len(cop1.build_bd_segment(0, 0, b"", OPTS, validate=False)) == 4 * (3 + 4 + 1 + 1)
    assert cop1.build_ad_segment(0, 0, b"x" * 2000, OPTS, validate=False)
    assert cop1.build_cltu(0, 0, b"x" * 2000, OPTS, validate=False)
    assert cop1.build_transfer_frame(3, 0, b"x", OPTS, validate=False)
    assert cop1.build_bd_segment(0, 0, b"x" * 1018, ProtocolOptions(fec_in_frame=True),
                                 validate=False)


def test_control_param_ids():
    cop1.build_control_request(0, {0: 1, 39: 2}, OPTS)
    with pytest.raises(ProtocolError):
        cop1.build_control_request(0, {40: 1}, OPTS)
    with pytest.raises(ProtocolError):
        cop1.build_control_request(0, {-1: 1}, OPTS)


def test_directives_table():
    assert set(cop1.DIRECTIVES) == set(range(2, 13))
    assert cop1.DIRECTIVES[6] == "Terminate AD"
    for op in cop1.DIRECTIVES:
        cop1.build_directive(op, 0, 0, 0, OPTS)
    for bad in (0, 1, 13):
        with pytest.raises(ProtocolError):
            cop1.build_directive(bad, 0, 0, 0, OPTS)


def test_subscription_ranges():
    for flow in range(7):
        cop1.build_output_flow_subscription(0, 0, flow, 0, OPTS)
    for auth in range(3):
        cop1.build_output_flow_subscription(0, 0, 0, auth, OPTS)
    for flow, auth in ((7, 0), (-1, 0), (0, 3), (0, -1)):
        with pytest.raises(ProtocolError):
            cop1.build_output_flow_subscription(0, 0, flow, auth, OPTS)


# ------------------------------------------------------------ helpers
def test_ip_helpers():
    assert cop1.encode_ip("192.52.109.15") == 0xC0346D0F
    assert cop1.decode_ip(0xC0346D0F) == "192.52.109.15"
    for bad in ("1.2.3", "1.2.3.256", "a.b.c.d", ""):
        with pytest.raises(ProtocolError):
            cop1.encode_ip(bad)


def test_tm_port_helpers():
    assert cop1.encode_tm_port(1, 3070) == 0x00010BFE
    assert cop1.decode_tm_port(0x00010BFE) == (1, 3070)
    assert cop1.decode_tm_port(cop1.encode_tm_port(0xFFFF, 0xFFFF)) == (0xFFFF, 0xFFFF)
    for tmu, port in ((0x10000, 0), (0, 0x10000), (-1, 0), (0, -1)):
        with pytest.raises(ProtocolError):
            cop1.encode_tm_port(tmu, port)


def test_names():
    assert sorted(cop1.CONFIG_PARAM_NAMES) == list(range(29))
    assert cop1.CONFIG_PARAM_NAMES[0] == "Spacecraft identifier"
    assert cop1.CONFIG_PARAM_NAMES[13] == "FOP1 Virtual Channel ID"
    assert cop1.CONFIG_PARAM_NAMES[16] == "FOP4 Virtual Channel ID"
    assert cop1.CONFIG_PARAM_NAMES[17] == "FOP1 Virtual Channel ID equivalence"
    assert cop1.CONFIG_PARAM_NAMES[20] == "FOP4 Virtual Channel ID equivalence"
    assert cop1.CONFIG_PARAM_NAMES[25] == "FOP1 repetition parameter"
    assert cop1.CONFIG_PARAM_NAMES[28] == "FOP4 repetition parameter"
    assert cop1.STATUS_PARAM_NAMES == {
        0: "Telecommand global status", 1: "Number of programming perturbation",
        2: "Channel auto switch occurs", 7: "COP1 version and compatibility options"}
    assert cop1.STATUS_ERROR_BITS[0x80] == "CODAGE"


# ------------------------------------------------------------ monitoring
def monitoring_report_message():
    config = [0] * 40
    config[0], config[3] = 1023, 0x00010BFE
    status = [0] * 20
    status[0], status[7] = 0x30, 0x01020400
    return server_message(0, [0, 0, 77, *config, *status])


def test_monitoring_report():
    msg = monitoring_report_message()
    assert msg.size == 268
    report = cop1.parse_monitoring_response(msg)
    assert isinstance(report, MonitoringReport)
    assert report.tag == 77
    assert len(report.config) == 40 and len(report.status) == 20
    assert report.config_named()["Spacecraft identifier"] == 1023
    assert len(report.config_named()) == 29
    assert report.status_error_flags() == ["FOP", "TIME"]
    assert report.tm_port() == (1, 3070)
    assert report.cop1_version() == (1, 2, 4)


def test_negative_responses():
    msg = server_message(0, [-1])
    assert msg.size == 20
    assert isinstance(cop1.parse_monitoring_response(msg), NegativeResponse)
    assert isinstance(cop1.parse_control_response(msg), NegativeResponse)
    assert isinstance(cop1.parse_tc_response(msg), NegativeResponse)


def test_monitoring_unknown_cases():
    assert isinstance(cop1.parse_monitoring_response(server_message(0, [0, 0, 1])),
                      UnknownResponse)
    assert isinstance(cop1.parse_monitoring_response(server_message(0, [])), UnknownResponse)
    wrong_op = server_message(0, [5, 0, 77, *([0] * 60)])
    assert isinstance(cop1.parse_monitoring_response(wrong_op), UnknownResponse)


def test_control_ack():
    ack = cop1.parse_control_response(server_message(1, [1, 0, 5, 3]))
    assert ack == ControlAck(tag=5, status=3)
    assert isinstance(cop1.parse_control_response(server_message(1, [2, 0, 5, 3])),
                      UnknownResponse)
    assert isinstance(cop1.parse_control_response(server_message(1, [])), UnknownResponse)


# ------------------------------------------------------------ tc responses
def test_tc_flow0_accept_reject():
    ok = cop1.parse_tc_response(server_message(0, [0, 20, 3, 9, 0]))
    assert ok == AcceptReject(op=20, vcid=3, tag=9, accepted=True)
    assert cop1.parse_tc_response(server_message(0, [0, 20, 3, 9, 4])).accepted is False


def test_tc_flow1_confirm():
    ok = cop1.parse_tc_response(server_message(0, [1, 30, 2, 8, 0]))
    assert ok == ConfirmResponse(op=30, vcid=2, tag=8, positive=True)
    assert cop1.parse_tc_response(server_message(0, [1, 30, 2, 8, 1])).positive is False


def test_tc_flow2_alert():
    alert = cop1.parse_tc_response(server_message(0, [2, 0, 4, 5]))
    assert alert == AlertIndication(vcid=4, code=5, name="CLCW")
    names = {1: "limit", 2: "lockout", 3: "sync", 4: "NN(R)", 5: "CLCW",
             6: "LLIF", 7: "term", 8: "not applicable"}
    for code, name in names.items():
        assert cop1.parse_tc_response(server_message(0, [2, 0, 4, code])).name == name


def test_tc_flow3_suspend():
    assert cop1.parse_tc_response(server_message(0, [3, 0, 6, 0])) == SuspendIndication(vcid=6)


def test_tc_flow4_segment_echo():
    echo = cop1.parse_tc_response(
        server_message(0, [4, 30, 3, 9, 5, 0x01020304, 0x05000000]))
    assert echo == SegmentEcho(op=30, field=3, tag=9, length=5, data=bytes([1, 2, 3, 4, 5]))


def test_tc_flow5_cltu_echo():
    echo = cop1.parse_tc_response(
        server_message(0, [5, 0, 3, 9, 11, 2, 3, 0xAABBCC00]))
    assert echo == CltuEcho(vcid=3, tag=9, frame_sequence=11, error_location=2,
                            length=3, data=bytes([0xAA, 0xBB, 0xCC]))


def test_tc_flow6_additional_info():
    body = [6, 0, 3] + list(range(100, 112))
    msg = server_message(0, body)
    assert msg.size == 76
    info = cop1.parse_tc_response(msg)
    assert info == AdditionalInfo(
        vcid=3, last_clcw=100, state=101, last_event=102, v_s=103, nn_r=104, n_r=105,
        timer_initial=106, timeout_type=107, transmission_limit=108,
        transmission_count=109, suspended=110, sliding_window=111)


def test_tc_unknown_flow_with_size_36():
    msg = server_message(0, [9, 0, 0, 0, 0])
    assert msg.size == 36
    assert isinstance(cop1.parse_tc_response(msg), UnknownResponse)


def test_tc_echo_length_larger_than_data_is_unknown():
    seg = server_message(0, [4, 30, 3, 9, 9, 0x01020304, 0x05000000])  # 9 > 8 bytes
    assert isinstance(cop1.parse_tc_response(seg), UnknownResponse)
    cltu = server_message(0, [5, 0, 3, 9, 1, 2, 5, 0xAABBCCDD])  # 5 > 4 bytes
    assert isinstance(cop1.parse_tc_response(cltu), UnknownResponse)
    negative_len = server_message(0, [4, 30, 3, 9, -1, 0])
    assert isinstance(cop1.parse_tc_response(negative_len), UnknownResponse)


@pytest.mark.parametrize("body", [
    [], [4], [4, 30, 3], [4, 30, 3, 9], [5, 0, 3], [5, 0, 3, 9, 1, 2],
    [0, 20], [1, 30, 2, 8], [2, 0], [6, 0, 3], [99],
])
def test_truncated_bodies_never_crash(body):
    msg = server_message(0, body)
    assert isinstance(cop1.parse_tc_response(msg), UnknownResponse)
    cop1.parse_control_response(msg)
    cop1.parse_monitoring_response(msg)


# ------------------------------------------------------------ unsigned tags
@pytest.mark.parametrize("tag", [0xFFFFFFFF, 0x80000000])
def test_tags_come_back_unsigned_and_unchanged(tag):
    # Server replies are packed with struct (see server_message), not with the code under test.
    ack = cop1.parse_control_response(server_message(1, [1, 0, tag, 3]))
    assert isinstance(ack, ControlAck) and ack.tag == tag

    report = cop1.parse_monitoring_response(server_message(0, [0, 0, tag, *([0] * 60)]))
    assert isinstance(report, MonitoringReport) and report.tag == tag

    accept = cop1.parse_tc_response(server_message(0, [0, 20, 3, tag, 0]))
    assert isinstance(accept, AcceptReject) and accept.tag == tag

    confirm = cop1.parse_tc_response(server_message(0, [1, 30, 2, tag, 0]))
    assert isinstance(confirm, ConfirmResponse) and confirm.tag == tag

    segment = cop1.parse_tc_response(server_message(0, [4, 30, 3, tag, 1, 0x01000000]))
    assert isinstance(segment, SegmentEcho) and segment.tag == tag

    cltu = cop1.parse_tc_response(server_message(0, [5, 0, 3, tag, 11, 2, 1, 0xAA000000]))
    assert isinstance(cltu, CltuEcho) and cltu.tag == tag


# ------------------------------------------------------------ data type of `data`
DATA_BUILDERS = [
    lambda data: cop1.build_bd_segment(1, 2, data, OPTS),
    lambda data: cop1.build_ad_segment(1, 2, data, OPTS),
    lambda data: cop1.build_cltu(1, 2, data, OPTS),
    lambda data: cop1.build_transfer_frame(0, 2, data, OPTS),
]


@pytest.mark.parametrize("build", DATA_BUILDERS)
def test_str_data_is_rejected(build):
    with pytest.raises(ProtocolError):
        build("abcdefgh")
    for bad in (None, 5, [1, 2, 3], ["a"]):
        with pytest.raises(ProtocolError):
            build(bad)


@pytest.mark.parametrize("build", DATA_BUILDERS)
def test_bytearray_and_memoryview_match_bytes(build):
    payload = bytes(range(10))
    assert build(bytearray(payload)) == build(payload)
    assert build(memoryview(payload)) == build(payload)


def test_str_rejected_before_length_check_even_without_validation():
    with pytest.raises(ProtocolError):
        cop1.build_bd_segment(1, 2, "text", OPTS, validate=False)


@pytest.mark.parametrize("mode", ["whole_packet", "excluding_postamble", "payload_only"])
def test_checksum_with_nonzero_flow_id_matches_independent_sum(mode):
    opts = ProtocolOptions(checksum_mode=mode, telecommand_flow_id=0x1234)
    raw = cop1.build_bd_segment(3, 9, bytes.fromhex("deadbeef01"), opts)
    words = struct.unpack(">11I", raw)
    assert words[2] == 0x1234
    selected = {"whole_packet": words, "excluding_postamble": words[:-1], "payload_only": words[3:-1]}[mode]
    assert sum(selected) % 2 ** 32 == 0
