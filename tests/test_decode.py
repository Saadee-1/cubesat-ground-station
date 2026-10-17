import dataclasses
from pathlib import Path

import pytest

from groundstation.config import load_config
from groundstation.decode import decode_frame

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"


@pytest.fixture
def params():
    return load_config(str(SAMPLES / "sample_config_5.json"))


def make_frame(size=64, voltage=370, temp=650):
    frame = bytearray(size)
    frame[20:22] = voltage.to_bytes(2, "big")
    if size >= 37:
        frame[35:37] = temp.to_bytes(2, "big", signed=True)
    return bytes(frame)


def by_name(results, name):
    return next(r for r in results if r.name == name)


def test_basic_values(params):
    res = decode_frame(make_frame(), params)
    batt = by_name(res, "Battery Voltage")
    assert batt.raw == 370 and batt.value == pytest.approx(3.70) and batt.status == "OK"
    assert by_name(res, "Temperature").value == pytest.approx(25.0)


def test_negative_signed(params):
    res = decode_frame(make_frame(temp=-100), params)
    assert by_name(res, "Temperature").value == pytest.approx(-50.0)


def test_little_endian(params):
    little = [dataclasses.replace(params[0], endianness="little")]
    frame = bytearray(64)
    frame[20:22] = bytes([0x01, 0x72])
    assert decode_frame(bytes(frame), little)[0].raw == 29185


def test_short_frame(params):
    res = decode_frame(make_frame(size=30), params)
    assert by_name(res, "Battery Voltage").status == "OK"
    assert by_name(res, "Temperature").status == "SHORT_FRAME"


def test_conversion_error(params):
    from groundstation.convert import compile_equation
    bad = [dataclasses.replace(params[0], equation="1 / RAW", fn=compile_equation("1 / RAW"))]
    res = decode_frame(bytes(64), bad)  # raw 0 at bytes 20-21
    assert res[0].status == "CONVERSION_ERROR"


def test_out_of_range(params):
    limited = [dataclasses.replace(params[0], limit_min=3.0, limit_max=4.2)]
    res = decode_frame(make_frame(voltage=500), limited)
    assert res[0].status == "OUT_OF_RANGE"


def test_config_order(params):
    res = decode_frame(make_frame(), params)
    assert [r.name for r in res] == [p.name for p in params]
