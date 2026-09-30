"""Minimal end-to-end checks on synthetic data (no hardware needed)."""
from pieeg_clicker.calibrate import analyse, record
from pieeg_clicker.config import Config
from pieeg_clicker.detectors import GestureEngine
from pieeg_clicker.simulate import SyntheticSource


def run_engine(cfg, source, seconds):
    engine = GestureEngine(cfg, source.fs)
    actions = []
    for _ in range(int(seconds * source.fs / source.block_size)):
        actions += [(e.t, e.action) for e in engine.process(source.read()) if e.action]
    return actions


def test_gestures_detected_and_no_false_clicks():
    cfg = Config()
    cfg.mapping["clench"] = "blank"
    cfg.blink.threshold_uv = 150.0  # as if calibrated

    # Demo script: every 6 s a double blink, triple blink or jaw clench, in turn.
    demo = SyntheticSource(realtime=False, demo=True, seed=7)
    actions = run_engine(cfg, demo, 60)
    expected = {1: "next", 2: "previous", 0: "blank"}
    correct = sum(expected[round(t / 6) % 3] == a for t, a in actions)
    assert correct >= 8 and len(actions) <= 9  # 9 scripted gestures

    # Natural blinking, glances at notes and talking must not click.
    idle = SyntheticSource(realtime=False, demo=False, seed=8)
    idle.cue("talk", 100)
    assert run_engine(cfg, idle, 120) == []


def test_calibration_separates_natural_and_deliberate_blinks():
    class Reversed(SyntheticSource):  # electrodes wired the other way round
        def read(self):
            return -super().read()

    cfg = Config()
    source = Reversed(realtime=False, demo=False, seed=3)
    rec, script = record(source, cfg, with_clench=True, say=lambda _: None)
    new, _ = analyse(rec, script, cfg)
    assert new.blink.polarity == -1
    assert 120 < new.blink.threshold_uv < 220  # natural ~70-140 µV, deliberate ~220-350 µV
    assert 9 < new.clench.threshold_uv < 35    # talking ~5-10 µV, clench ~35-55 µV
