"""Loading and validating the JSON parameter-definition file."""
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from .convert import compile_equation
from .errors import ConfigError

# ASSUMPTIONS (both unconfirmed against a real system; change here if they differ).
POSITION_IS_BYTE_OFFSET_ZERO_BASED = True  # False would mean "position 1 = first byte"
DEFAULT_ENDIANNESS = "big"                 # used when a parameter has no "endianness" key

# Data type name -> size in bytes.
DATA_TYPES = {
    "UInt8": 1, "Int8": 1,
    "UInt16": 2, "Int16": 2,
    "UInt32": 4, "Int32": 4,
    "UInt64": 8, "Int64": 8,
    "Float32": 4, "Float64": 8,
}

# Data type name -> struct format character (helper for packing/unpacking).
STRUCT_CODES = {
    "UInt8": "B", "Int8": "b",
    "UInt16": "H", "Int16": "h",
    "UInt32": "I", "Int32": "i",
    "UInt64": "Q", "Int64": "q",
    "Float32": "f", "Float64": "d",
}

REQUIRED_KEYS = ("name", "position", "length", "dataType", "equation", "unit")
VALID_ENDIANNESS = ("big", "little")


@dataclass
class ParamDef:
    """One telemetry parameter as described in the config file."""
    name: str
    position: int
    length: int
    data_type: str
    endianness: str
    equation: str
    fn: Callable[[float], float] = field(repr=False, compare=False)  # compiled equation
    unit: str
    param_id: Optional[object] = None
    decimals: int = 3
    limit_min: Optional[float] = None
    limit_max: Optional[float] = None


def byte_offset(param: ParamDef) -> int:
    """Return the zero-based byte offset of a parameter inside the frame."""
    return param.position if POSITION_IS_BYTE_OFFSET_ZERO_BASED else param.position - 1


def _is_int(value: object) -> bool:
    """True for real ints (bool is a subclass of int, so exclude it)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """True for int or float, but not bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_param(index: int, entry: object) -> ParamDef:
    """Validate one entry of the "parameters" list and build a ParamDef."""
    if not isinstance(entry, dict):
        raise ConfigError(f"parameter #{index}: must be a JSON object")
    name = entry.get("name")
    label = f"parameter #{index} ({name!r})"  # prefix for every error below

    missing = [k for k in REQUIRED_KEYS if k not in entry]
    if missing:
        raise ConfigError(f"{label}: missing required key(s): {', '.join(missing)}")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{label}: name must be a non-empty string")

    data_type = entry["dataType"]
    if data_type not in DATA_TYPES:
        raise ConfigError(f"{label}: unknown dataType {data_type!r}")
    length = entry["length"]
    if length != DATA_TYPES[data_type] or not _is_int(length):
        raise ConfigError(
            f"{label}: length {length!r} does not match {data_type} "
            f"({DATA_TYPES[data_type]} bytes)")
    position = entry["position"]
    # Smallest legal position depends on the assumption flag (read at call time,
    # so tests can change it): zero-based starts at 0, one-based starts at 1.
    min_position = 0 if POSITION_IS_BYTE_OFFSET_ZERO_BASED else 1
    if not _is_int(position) or position < min_position:
        basis = "zero-based" if POSITION_IS_BYTE_OFFSET_ZERO_BASED else "one-based"
        raise ConfigError(
            f"{label}: position must be an integer >= {min_position} "
            f"(positions are {basis}), got {position!r}")

    endianness = entry.get("endianness", DEFAULT_ENDIANNESS)
    if endianness not in VALID_ENDIANNESS:
        raise ConfigError(f"{label}: endianness must be 'big' or 'little', got {endianness!r}")

    unit = entry["unit"]
    if not isinstance(unit, str):
        raise ConfigError(f"{label}: unit must be a string")

    try:
        fn = compile_equation(entry["equation"])
    except ConfigError as exc:
        raise ConfigError(f"{label}: bad equation: {exc}") from exc

    display = entry.get("display", {})
    if not isinstance(display, dict):
        raise ConfigError(f"{label}: display must be an object")
    decimals = display.get("decimals", 3)
    if not _is_int(decimals) or decimals < 0:
        raise ConfigError(f"{label}: display.decimals must be an integer >= 0")

    limits = entry.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError(f"{label}: limits must be an object")
    limit_min, limit_max = limits.get("min"), limits.get("max")
    for key, val in (("min", limit_min), ("max", limit_max)):
        if val is not None and not _is_number(val):
            raise ConfigError(f"{label}: limits.{key} must be a number")
    if limit_min is not None and limit_max is not None and limit_min > limit_max:
        raise ConfigError(f"{label}: limits.min is greater than limits.max")

    return ParamDef(
        name=name, position=position, length=length, data_type=data_type,
        endianness=endianness, equation=entry["equation"], fn=fn, unit=unit,
        param_id=entry.get("id"), decimals=decimals,
        limit_min=limit_min, limit_max=limit_max,
    )


def load_config(path: str) -> list[ParamDef]:
    """Read the JSON file at `path` and return the validated parameter list."""
    try:
        with open(path, "rb") as handle:  # read raw bytes; we pick the encoding ourselves
            raw_bytes = handle.read()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path!r}: {exc}") from exc

    try:
        if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            # A UTF-16 byte order mark (Notepad "Unicode"); "utf-16" reads and drops it.
            text = raw_bytes.decode("utf-16")
        else:
            try:
                text = raw_bytes.decode("utf-8-sig")  # -sig also drops a UTF-8 BOM
            except UnicodeDecodeError:
                # Not valid UTF-8: assume an old Windows file (e.g. degree sign = 0xB0).
                text = raw_bytes.decode("cp1252")
    except UnicodeDecodeError as exc:
        # Also covers broken UTF-16 (e.g. an odd number of bytes).
        raise ConfigError(f"config file {path!r} is not readable text: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {path!r} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("parameters"), list):
        raise ConfigError('config must be an object with a "parameters" list')
    entries = data["parameters"]
    if not entries:
        raise ConfigError("the parameters list is empty")
    if "numberOfParameters" in data and data["numberOfParameters"] != len(entries):
        raise ConfigError(
            f"numberOfParameters is {data['numberOfParameters']!r} "
            f"but the list has {len(entries)} entries")

    params: list[ParamDef] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        param = _parse_param(index, entry)
        if param.name in seen:
            raise ConfigError(f"parameter #{index} ({param.name!r}): duplicate name")
        seen.add(param.name)
        params.append(param)
    return params
