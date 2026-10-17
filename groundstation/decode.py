"""Frame decoding: raw bytes -> engineering values."""
import math
import struct
from dataclasses import dataclass
from typing import Optional, Union

from .config import DATA_TYPES, ParamDef, byte_offset

# Status values (our own extension; the parameter file format defines none).
STATUS_OK = "OK"
STATUS_OUT_OF_RANGE = "OUT_OF_RANGE"
STATUS_SHORT_FRAME = "SHORT_FRAME"
STATUS_CONVERSION_ERROR = "CONVERSION_ERROR"


@dataclass
class ParamValue:
    """The decoded result for one parameter of one frame."""
    name: str
    param_id: Optional[object]
    raw: Optional[Union[int, float]]
    value: Optional[float]
    unit: str
    status: str
    decimals: int


def decode_frame(frame: bytes, params: list[ParamDef]) -> list[ParamValue]:
    """Decode one frame into one ParamValue per parameter, in config order.

    Required behaviour:
    - A parameter whose bytes do not fit inside the frame gets status
      SHORT_FRAME (raw and value None); the other parameters still decode.
    - Integers use the parameter's endianness and signedness; floats use struct.
    - The compiled equation (param.fn) is applied to the raw value.
    - ZeroDivisionError / OverflowError from the equation give
      CONVERSION_ERROR (value None, raw kept).
    - A value outside limit_min / limit_max gives OUT_OF_RANGE.
    - Otherwise the status is OK.
    - Never raises because of bad frame data.
    """
    results: list[ParamValue] = []

    for p in params:
        # Helper to build the result for this parameter with less repetition.
        def make(raw, value, status, p=p) -> ParamValue:
            return ParamValue(p.name, p.param_id, raw, value, p.unit, status, p.decimals)

        # Zero-based first byte (honours the position assumption in config.py),
        # and where this parameter's bytes would end inside the frame.
        start = byte_offset(p)
        end = start + p.length

        # Not enough bytes in the frame: report SHORT_FRAME and move on.
        if end > len(frame):
            results.append(make(None, None, STATUS_SHORT_FRAME))
            continue

        # The exact bytes that belong to this parameter.
        chunk = frame[start:end]

        raw: Union[int, float]
        if p.data_type.startswith("Float"):
            # Floats: struct needs '>' (big) or '<' (little) plus 'f' or 'd'.
            fmt = (">" if p.endianness == "big" else "<") + ("f" if p.data_type == "Float32" else "d")
            try:
                # A wrong chunk size is treated like a struct error.
                if len(chunk) != DATA_TYPES[p.data_type]:
                    raise struct.error("chunk size does not match data type")
                raw = struct.unpack(fmt, chunk)[0]
            except (struct.error, KeyError):
                results.append(make(None, None, STATUS_CONVERSION_ERROR))
                continue
            # NaN or infinity cannot be converted meaningfully; keep the raw value.
            if not math.isfinite(raw):
                results.append(make(raw, None, STATUS_CONVERSION_ERROR))
                continue
        else:
            # Integers: only types named "Int..." are signed ("UInt..." is not).
            raw = int.from_bytes(chunk, p.endianness, signed=p.data_type.startswith("Int"))

        # Apply the compiled equation; math errors become CONVERSION_ERROR.
        try:
            value = p.fn(raw)
        except (ZeroDivisionError, OverflowError):
            results.append(make(raw, None, STATUS_CONVERSION_ERROR))
            continue

        # NaN/infinity is a conversion failure. math.isfinite raises OverflowError
        # for an int too big to become a float, which we also treat as non-finite.
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            results.append(make(raw, None, STATUS_CONVERSION_ERROR))
            continue

        # Each limit is only checked when it is set (not None).
        if ((p.limit_min is not None and value < p.limit_min)
                or (p.limit_max is not None and value > p.limit_max)):
            status = STATUS_OUT_OF_RANGE
        else:
            status = STATUS_OK
        results.append(make(raw, value, status))

    return results
