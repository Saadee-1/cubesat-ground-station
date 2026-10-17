"""Robustness of the config reader, the telemetry panel poll loop, the decoder and the command line."""
import dataclasses
import json
import re
from pathlib import Path

import pytest

import groundstation.config as config
from groundstation.app import main
from groundstation.config import load_config
from groundstation.convert import compile_equation
from groundstation.decode import ParamValue, decode_frame
from groundstation.errors import ConfigError
from groundstation.source import SimFrameSource, build_frame
from groundstation.tm import TelemetryPipeline

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"


def one_param_json(tmp_path, position):
    """Write a config with a single UInt8 parameter at `position`."""
    data = {"parameters": [{"name": "A", "position": position, "length": 1,
                            "dataType": "UInt8", "equation": "RAW", "unit": "x"}]}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return str(path)


def make_panel(root):
    """Create a TelemetryPanel as a child of the shared Tk root."""
    from groundstation.gui import TelemetryPanel
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    pipeline = TelemetryPipeline(SimFrameSource(p5), p5)
    return root, TelemetryPanel(root, pipeline), pipeline


# ---- FIX 1 ---------------------------------------------------------------
def test_one_based_position_reads_first_byte(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "POSITION_IS_BYTE_OFFSET_ZERO_BASED", False)
    params = load_config(one_param_json(tmp_path, 1))
    result = decode_frame(bytes([42, 7, 7]), params)
    assert result[0].raw == 42 and result[0].status == "OK"


def test_one_based_rejects_position_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "POSITION_IS_BYTE_OFFSET_ZERO_BASED", False)
    with pytest.raises(ConfigError, match="one-based"):
        load_config(one_param_json(tmp_path, 0))


def test_one_based_round_trip(monkeypatch):
    zero_based = load_config(str(SAMPLES / "sample_config_10.json"))
    monkeypatch.setattr(config, "POSITION_IS_BYTE_OFFSET_ZERO_BASED", False)
    # Shift every position by +1 so the same bytes are addressed one-based.
    params = [dataclasses.replace(p, position=p.position + 1) for p in zero_based]
    raws = [1.5 if p.data_type.startswith("Float") else (100 if p.data_type.startswith("U") else -7)
            for p in params]
    decoded = decode_frame(build_frame(params, raws, 64), params)
    assert [d.raw for d in decoded] == raws


# ---- FIX 2 ---------------------------------------------------------------
def test_poll_survives_exception_and_rearms(monkeypatch, tk_root):
    root, panel, pipeline = make_panel(tk_root)
    try:
        def boom():
            raise RuntimeError("bad poll")
        monkeypatch.setattr(pipeline, "poll", boom)
        scheduled = []
        monkeypatch.setattr(panel, "after", lambda ms, fn: scheduled.append(fn) or "id")
        panel._poll()
        assert scheduled == [panel._poll]  # next poll was still scheduled
        assert panel._last_error == "ERROR: RuntimeError: bad poll"
        assert re.search(r"Last error \(\d\d:\d\d:\d\d\): ERROR: RuntimeError: bad poll", panel._status_text.get())
    finally:
        panel.destroy()  # keep the shared root alive


# ---- FIX 3 ---------------------------------------------------------------
def test_short_frame_does_not_count_as_update(tk_root):
    root, panel, _ = make_panel(tk_root)
    try:
        row = panel.rows[0]
        panel._apply(row, ParamValue("x", None, None, None, "V", "SHORT_FRAME", 2))
        assert row.last_update is None
        panel._apply(row, ParamValue("x", None, 1, 1.0, "V", "OK", 2))
        assert row.last_update is not None
    finally:
        panel.destroy()  # keep the shared root alive


# ---- FIX 4 ---------------------------------------------------------------
def test_huge_integer_result_is_conversion_error():
    params = load_config(str(SAMPLES / "sample_config_5.json"))
    expr = "RAW * " + "9" * 400
    huge = dataclasses.replace(params[0], equation=expr, fn=compile_equation(expr))
    frame = bytearray(64)
    frame[20:22] = (1).to_bytes(2, "big")  # raw value 1
    result = decode_frame(bytes(frame), [huge])[0]
    assert result.status == "CONVERSION_ERROR" and result.value is None and result.raw == 1


# ---- FIX 5 ---------------------------------------------------------------
def test_set_params_drops_stale_frames():
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    p10 = load_config(str(SAMPLES / "sample_config_10.json"))
    source = SimFrameSource(p5)
    pipeline = TelemetryPipeline(source, p5)
    source.frames.put(source.next_frame())
    source.frames.put(source.next_frame())
    pipeline.set_params(p10)
    assert pipeline.poll() == []


# ---- FIX 6 ---------------------------------------------------------------
def pipeline_with(frames):
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    source = SimFrameSource(p5)
    for f in frames:
        source.frames.put(f)
    return TelemetryPipeline(source, p5), p5


def test_error_and_out_of_range_counters():
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    # Raw values per parameter: Bus Current is index 2 and has limits 0..0.8 A.
    def frame(bus_raw):
        return build_frame(p5, [370, 650, bus_raw, 1.0, 5], 64)
    ok = frame(100)            # 0.1 A: fine
    out_of_range = frame(900)  # 0.9 A: above the 0.8 limit
    short = b"\x00" * 10       # too short: SHORT_FRAME
    pipeline, _ = pipeline_with([ok, out_of_range, short])
    pipeline.poll()
    assert pipeline.frames_total == 3
    assert pipeline.frames_with_errors == 1     # only the short frame
    assert pipeline.frames_out_of_range == 1    # only the out-of-limits frame


def test_error_frame_not_also_counted_out_of_range():
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    # Bus Current (0..9 bytes region) is present and out of range, but the frame is
    # too short for Battery Voltage at byte 20, so it is an error frame.
    short_and_oor = build_frame(p5, [370, 650, 900, 1.0, 5], 64)[:12]
    pipeline, _ = pipeline_with([short_and_oor])
    pipeline.poll()
    assert pipeline.frames_with_errors == 1 and pipeline.frames_out_of_range == 0


def test_status_bar_shows_out_of_limits(tk_root):
    root, panel, pipeline = make_panel(tk_root)
    try:
        pipeline.frames_out_of_range = 4
        panel._refresh_status_bar()
        assert "Out of limits: 4" in panel._status_text.get()
    finally:
        panel.destroy()  # keep the shared root alive


# ---- FIX 7 ---------------------------------------------------------------
@pytest.mark.parametrize("argv", [["--rate", "0"], ["--rate", "-1"], ["--corrupt", "5"],
                                  ["--corrupt", "-0.1"]])
def test_bad_command_line_values_exit(argv):
    with pytest.raises(SystemExit):
        main(argv)
