"""Guided calibration: compares your natural and deliberate blinks (and jaw clench vs
talking) and stores thresholds that separate them."""
from __future__ import annotations

import copy
import logging
import statistics
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

import numpy as np

from .config import Config
from .detectors import BlinkDetector, ClenchDetector, Event

logger = logging.getLogger(__name__)

REST_S = 15.0
TALK_S = 15.0
BLINK_PROMPTS = 6
CLENCH_PROMPTS = 4
PROMPT_EVERY_S = 3.0
CLENCH_EVERY_S = 4.0
RESPONSE_WINDOW = (0.1, 1.6)  # seconds after a prompt in which the response is searched


@dataclass
class Step:
    t: float        # stream time (s)
    kind: str       # say | rest | talk | blink | clench | end
    text: str


def build_script(with_clench: bool) -> List[Step]:
    t = 3.0  # let the filters settle
    steps = [Step(0.0, "say", "Calibration starting: sit still and look ahead."),
             Step(t, "rest", f"[1/4] Relax and blink NATURALLY for {REST_S:.0f} s.")]
    t += REST_S
    steps.append(Step(t, "talk", f"[2/4] TALK out loud as if presenting for {TALK_S:.0f} s (blink naturally)."))
    t += TALK_S
    steps.append(Step(t, "say", "[3/4] Blink FIRMLY once each time you see BLINK."))
    t += 2.0
    for n in range(BLINK_PROMPTS):
        steps.append(Step(t, "blink", f"  BLINK ({n + 1}/{BLINK_PROMPTS})"))
        t += PROMPT_EVERY_S
    if with_clench:
        steps.append(Step(t, "say", "[4/4] Clench your jaw firmly for ~1 s each time you see CLENCH."))
        t += 2.0
        for n in range(CLENCH_PROMPTS):
            steps.append(Step(t, "clench", f"  CLENCH ({n + 1}/{CLENCH_PROMPTS})"))
            t += CLENCH_EVERY_S
    steps.append(Step(t, "end", "Done."))
    return steps


@dataclass
class Recording:
    fs: float
    blinks: Dict[int, List[Event]] = field(default_factory=lambda: {1: [], -1: []})  # per polarity
    emg: List[np.ndarray] = field(default_factory=list)
    noise_sigma: Dict[int, float] = field(default_factory=dict)

    def emg_between(self, t0: float, t1: float) -> np.ndarray:
        env = np.concatenate(self.emg) if self.emg else np.zeros(0)
        return env[int(t0 * self.fs):int(t1 * self.fs)]


def record(source, cfg: Config, with_clench: bool, say=print) -> Tuple[Recording, List[Step]]:
    """Play the prompt script while recording blink candidates and the EMG envelope."""
    fs = source.fs
    probes = {}
    for pol in (1, -1):  # try both polarities so reversed electrodes are detected too
        bcfg = replace(cfg.blink, polarity=pol, threshold_uv=None, min_threshold_uv=30.0, adaptive_k=4.0)
        probes[pol] = BlinkDetector(bcfg, fs, source.n_channels)
    clench = ClenchDetector(cfg.clench, fs, source.n_channels)
    rec = Recording(fs)
    script = build_script(with_clench)
    pending = list(script)
    n = 0
    source.start()
    try:
        while pending:
            now = n / fs
            while pending and pending[0].t <= now:
                step = pending.pop(0)
                say(step.text)
                if step.kind in ("blink", "clench"):
                    getattr(source, "cue", lambda *a: None)(step.kind)
                elif step.kind == "talk":
                    getattr(source, "cue", lambda *a: None)("talk", TALK_S)
            block = source.read()
            for pol, det in probes.items():
                rec.blinks[pol] += det.process(block, n)
            rec.emg.append(clench.envelope(block))
            n += len(block)
    finally:
        source.stop()
    rec.noise_sigma = {pol: det.noise.sigma for pol, det in probes.items()}
    return rec, script


def _responses(events: List[Event], prompts: List[float]) -> List[float]:
    """Largest blink amplitude after each prompt (missing responses are skipped)."""
    out = []
    for p in prompts:
        amps = [e.amplitude for e in events if p + RESPONSE_WINDOW[0] <= e.t <= p + RESPONSE_WINDOW[1]]
        if amps:
            out.append(max(amps))
    return out


def analyse(rec: Recording, script: List[Step], cfg: Config) -> Tuple[Config, List[str]]:
    """Derive thresholds from a recording. Returns the updated config and a report."""
    report: List[str] = []
    new = copy.deepcopy(cfg)
    rest_t0 = next(s.t for s in script if s.kind == "rest")
    talk_t0 = next(s.t for s in script if s.kind == "talk")
    blink_prompts = [s.t for s in script if s.kind == "blink"]
    clench_prompts = [s.t for s in script if s.kind == "clench"]

    # Blinks: pick the polarity under which the prompted blinks show up best.
    scores = {pol: _responses(rec.blinks[pol], blink_prompts) for pol in (1, -1)}
    pol = max((1, -1), key=lambda p: (len(scores[p]), sum(scores[p])))
    deliberate = scores[pol]
    natural = [e.amplitude for e in rec.blinks[pol] if rest_t0 <= e.t < talk_t0 + TALK_S]
    if len(deliberate) < BLINK_PROMPTS // 2:
        raise RuntimeError(
            f"Only {len(deliberate)}/{BLINK_PROMPTS} prompted blinks were found. Check that the "
            "Fp1/Fp2 electrodes touch the skin and that blink.channels matches your wiring, then retry.")
    if pol != cfg.blink.polarity:
        report.append(f"Blinks are {'negative' if pol < 0 else 'positive'} peaks on your wiring: "
                      f"blink.polarity set to {pol}.")
    floor = max(40.0, 5.0 * rec.noise_sigma.get(pol, 0.0))
    d_med, d_min = statistics.median(deliberate), min(deliberate)
    thr = 0.6 * d_med
    if natural:
        thr = max(thr, 1.15 * float(np.percentile(natural, 90)))
    thr = max(floor, min(thr, 0.8 * d_min))
    new.blink.polarity = pol
    new.blink.threshold_uv = round(thr, 1)
    report.append(f"Deliberate blinks: median {d_med:.0f} µV (smallest {d_min:.0f}), found {len(deliberate)}/{BLINK_PROMPTS}.")
    if natural:
        above = sum(a >= thr for a in natural)
        report.append(f"Natural blinks: median {statistics.median(natural):.0f} µV, "
                      f"{above}/{len(natural)} above the new threshold.")
        if above > len(natural) / 2:
            report.append("  Your natural and deliberate blinks are similar in size. The double-blink rule "
                          "still protects you, but blinking harder on purpose will make it more reliable.")
    report.append(f"Blink threshold set to {thr:.0f} µV.")

    # Jaw clench: must stand out from the EMG you produce while talking.
    if clench_prompts:
        rest = rec.emg_between(rest_t0 + 3.0, talk_t0)
        talk = rec.emg_between(talk_t0 + 1.0, talk_t0 + TALK_S)
        levels = [float(np.percentile(rec.emg_between(p + 0.3, p + 1.3), 70)) for p in clench_prompts]
        speech = max(float(np.percentile(talk, 99)), 3.0 * float(np.median(rest)))
        c = statistics.median(levels)
        if c > 1.5 * speech:
            cthr = float(np.sqrt(speech * c))  # geometric midpoint
            report.append(f"Jaw clench: {c:.0f} µV vs talking {speech:.0f} µV. Threshold set to {cthr:.0f} µV.")
        else:
            cthr = max(0.8 * c, 1.2 * speech)  # never below talking: better missed than misfired
            report.append(f"Jaw clench ({c:.0f} µV) barely exceeds talking ({speech:.0f} µV): the threshold "
                          f"stays above talking ({cthr:.0f} µV), so clenches may be hard to trigger. "
                          "Clench harder, add temple electrodes, or leave 'clench' unmapped.")
        new.clench.threshold_uv = round(cthr, 1)
    return new, report
