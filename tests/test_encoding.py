import json

import pytest

from groundstation.config import load_config
from groundstation.errors import ConfigError

# One-parameter config whose unit contains a degree sign.
TEXT = json.dumps({"parameters": [{
    "name": "Temperature", "position": 0, "length": 2, "dataType": "Int16",
    "equation": "RAW", "unit": "°C"}]}, ensure_ascii=False)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252", "utf-16"])
def test_encodings_load_degree_sign(tmp_path, encoding):
    path = tmp_path / "cfg.json"
    # "utf-16" in Python writes a byte order mark, like Notepad's "Unicode".
    path.write_bytes(TEXT.encode(encoding))
    assert load_config(str(path))[0].unit == "°C"


def test_undecodable_bytes_raise_config_error(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_bytes(b"\x81\x8d\x8f\x90")  # invalid in both UTF-8 and cp1252
    with pytest.raises(ConfigError):
        load_config(str(path))
