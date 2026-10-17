from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from groundstation.config import load_config  # noqa: E402
from groundstation.gui import TelemetryPanel  # noqa: E402
from groundstation.source import SimFrameSource  # noqa: E402
from groundstation.tm import TelemetryPipeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"


def test_rows_follow_config(tk_root):
    panel = None  # defined first so the finally block can always check it
    try:
        p5 = load_config(str(SAMPLES / "sample_config_5.json"))
        p10 = load_config(str(SAMPLES / "sample_config_10.json"))
        panel = TelemetryPanel(tk_root, TelemetryPipeline(SimFrameSource(p5), p5))
        assert len(panel.rows) == 5
        panel.rebuild(p10)
        assert len(panel.rows) == 10
        panel.rebuild(p5)
        assert len(panel.rows) == 5
    finally:
        if panel is not None:
            panel.destroy()  # keep the shared root alive
