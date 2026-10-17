import time
from pathlib import Path

import pytest

from groundstation.config import load_config
from groundstation.decode import decode_frame
from groundstation.source import SimFrameSource, build_frame
from groundstation.tm import TelemetryPipeline

SAMPLES = Path(__file__).resolve().parent.parent / "groundstation" / "samples"


@pytest.fixture
def params10():
    return load_config(str(SAMPLES / "sample_config_10.json"))


def test_round_trip(params10):
    # Pick a value that fits each type (floats use exactly representable numbers).
    raws = []
    for p in params10:
        if p.data_type.startswith("Float"):
            raws.append(1.5)
        elif p.data_type.startswith("U"):
            raws.append(100)
        else:
            raws.append(-7)
    frame = build_frame(params10, raws, 64)
    decoded = decode_frame(frame, params10)
    assert [d.raw for d in decoded] == raws


def test_corrupt_frames_never_raise(params10):
    source = SimFrameSource(params10, corrupt_prob=1.0, seed=42)
    pipeline = TelemetryPipeline(source, params10)
    for _ in range(200):
        source.frames.put(source.next_frame())
        pipeline.poll()
    assert pipeline.frames_total == 200


def test_start_stop_ends_thread(params10):
    source = SimFrameSource(params10, rate_hz=50)
    source.start()
    assert source.is_running()
    began = time.perf_counter()
    source.stop()
    assert time.perf_counter() - began < 1.0
    assert not source.is_running()
