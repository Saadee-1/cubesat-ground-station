import copy
import json
from pathlib import Path

import pytest

from groundstation.config import load_config
from groundstation.errors import ConfigError

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"

GOOD = {"parameters": [
    {"name": "A", "position": 0, "length": 2, "dataType": "UInt16",
     "equation": "RAW", "unit": "V"},
]}


def write(tmp_path, data):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_sample_5():
    params = load_config(str(SAMPLES / "sample_config_5.json"))
    assert len(params) == 5
    assert params[0].name == "Battery Voltage" and params[0].position == 20


def test_sample_10():
    assert len(load_config(str(SAMPLES / "sample_config_10.json"))) == 10


def test_good_minimal(tmp_path):
    assert len(load_config(write(tmp_path, GOOD))) == 1


@pytest.mark.parametrize("key", ["name", "position", "length", "dataType", "equation", "unit"])
def test_missing_required_key(tmp_path, key):
    data = copy.deepcopy(GOOD)
    del data["parameters"][0][key]
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def _mutated(**changes):
    data = copy.deepcopy(GOOD)
    data["parameters"][0].update(changes)
    return data


@pytest.mark.parametrize("data", [
    _mutated(dataType="UInt12"),
    _mutated(length=4),
    _mutated(position=-1),
    _mutated(equation="RAW ** 2"),
    _mutated(endianness="middle"),
])
def test_invalid_values(tmp_path, data):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_duplicate_names(tmp_path):
    data = copy.deepcopy(GOOD)
    data["parameters"].append(copy.deepcopy(data["parameters"][0]))
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_empty_list(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, {"parameters": []}))


def test_number_of_parameters_mismatch(tmp_path):
    data = copy.deepcopy(GOOD)
    data["numberOfParameters"] = 2
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))
