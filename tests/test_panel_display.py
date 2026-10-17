"""Telemetry panel: raw float display, statistics reset, error age."""
import re
from pathlib import Path

import pytest

from groundstation.config import load_config
from groundstation.decode import ParamValue
from groundstation.gui import CLEAN_POLLS_TO_CLEAR
from groundstation.source import SimFrameSource
from groundstation.tm import TelemetryPipeline

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"


def make_panel(root):
    """Create a TelemetryPanel as a child of the shared Tk root."""
    from groundstation.gui import TelemetryPanel
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    pipeline = TelemetryPipeline(SimFrameSource(p5), p5)
    return root, TelemetryPanel(root, pipeline), pipeline


def test_raw_float_formatting(tk_root):
    root, panel, _ = make_panel(tk_root)
    try:
        row = panel.rows[0]
        panel._apply(row, ParamValue("x", None, 0.30000000000000004, 1.0, "V", "OK", 2))
        assert row.labels["Raw"].cget("text") == "0.3"  # 6 significant digits
        panel._apply(row, ParamValue("x", None, 370, 3.7, "V", "OK", 2))
        assert row.labels["Raw"].cget("text") == "370"  # ints untouched
    finally:
        panel.destroy()  # keep the shared root alive


@pytest.mark.parametrize("name", ["sample_config_5.json", "sample_config_10.json"])
def test_temperature_unit_is_degrees_celsius(name):
    params = load_config(str(SAMPLES / name))
    temp = next(p for p in params if p.name == "Temperature")
    assert temp.unit == "\u00b0C"


def test_set_params_resets_counters():
    p5 = load_config(str(SAMPLES / "sample_config_5.json"))
    p10 = load_config(str(SAMPLES / "sample_config_10.json"))
    pipeline = TelemetryPipeline(SimFrameSource(p5), p5)
    pipeline.frames_total, pipeline.frames_with_errors, pipeline.frames_out_of_range = 5, 3, 2
    pipeline.set_params(p10)
    assert (pipeline.frames_total, pipeline.frames_with_errors,
            pipeline.frames_out_of_range) == (0, 0, 0)


def test_error_has_time_and_clears_after_clean_polls(monkeypatch, tk_root):
    root, panel, pipeline = make_panel(tk_root)
    try:
        monkeypatch.setattr(panel, "after", lambda ms, fn: "id")  # don't really schedule
        real_poll = pipeline.poll
        monkeypatch.setattr(pipeline, "poll", lambda: (_ for _ in ()).throw(ValueError("x")))
        panel._poll()
        assert re.search(r"Last error \(\d\d:\d\d:\d\d\): ERROR: ValueError: x",
                         panel._status_text.get())
        monkeypatch.setattr(pipeline, "poll", real_poll)
        for _ in range(CLEAN_POLLS_TO_CLEAR - 1):  # one short of the limit: still shown
            panel._poll()
        assert panel._last_error is not None
        panel._poll()  # the CLEAN_POLLS_TO_CLEAR-th clean poll clears it
        assert panel._last_error is None
        assert "Last error" not in panel._status_text.get()
    finally:
        panel.destroy()  # keep the shared root alive
