"""Blink and jaw-clench detection, blink-pattern grouping and gesture -> action logic."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .config import BlinkConfig, ClenchConfig, Config, GestureConfig
from .dsp import RobustStats, StreamingFilter, bandpass_sos, highpass_sos, lowpass_sos, notch_sos

logger = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str                     # blink, single_blink, double_blink, triple_blink, multi_blink, clench
    t: float                      # stream time (s)
    amplitude: float = 0.0        # µV: blink peak above baseline, or clench EMG RMS
    duration: float = 0.0         # s
    count: int = 1                # blinks in a pattern
    action: Optional[str] = None  # set when the event triggered an action


def channel_index(channels: Sequence[int], n_channels: int) -> List[int]:
    idx = [c - 1 for c in channels]
    if not idx or min(idx) < 0 or max(idx) >= n_channels:
        raise ValueError(f"channels {list(channels)} out of range 1..{n_channels}")
    return idx


class BlinkDetector:
    """Detects single blinks on the averaged frontal channels (Fp1/Fp2).

    Peak/valley detector on the low-passed signal: a blink rises at least
    `threshold` above the recent valley and then falls back by half of that rise
    within `max_duration_s`. Gaze shifts (looking up from your notes) and drifts
    are steps that never fall back, so they are rejected, and a blink riding on
    top of a step is still measured from its own starting level.
    """

    _IDLE, _ACTIVE = range(2)
    VALLEY_LEAK_UV_S = 50.0  # the valley creeps up this fast, so slow drift is ignored

    def __init__(self, cfg: BlinkConfig, fs: float, n_channels: int = 8):
        self.cfg = cfg
        self.fs = fs
        self.idx = channel_index(cfg.channels, n_channels)
        self._lowpass = StreamingFilter(lowpass_sos(cfg.lowpass_hz, fs))
        self._highpass = StreamingFilter(highpass_sos(0.5, fs))  # only for the noise estimate
        self.noise = RobustStats(fs)
        self._leak = self.VALLEY_LEAK_UV_S / fs
        self._valley: Optional[float] = None
        self._recent: deque = deque(maxlen=max(2, int(0.3 * fs)))  # for re-anchoring after a step
        self._state = self._IDLE
        self._start = 0
        self._base = self._peak = 0.0
        self._gated = False
        self.threshold = float("inf")
        self.level = 0.0  # largest rise above the valley in the last block, for the monitor

    @property
    def busy(self) -> bool:
        return self._state == self._ACTIVE

    def current_threshold(self) -> float:
        if np.isnan(self.noise.sigma):
            return float("inf")  # still warming up
        base = self.cfg.threshold_uv if self.cfg.threshold_uv is not None else self.cfg.min_threshold_uv
        return max(base, self.cfg.adaptive_k * self.noise.sigma)

    def transform(self, block: np.ndarray) -> np.ndarray:
        """Low-passed frontal signal (µV) with blinks pointing upwards."""
        return self.cfg.polarity * self._lowpass(block[:, self.idx]).mean(axis=1)

    def process(self, block: np.ndarray, i0: int, gate: Optional[np.ndarray] = None) -> List[Event]:
        s = self.transform(block)
        self.noise.update(self._highpass(s))
        self.threshold = thr = self.current_threshold()
        max_len = self.cfg.max_duration_s * self.fs
        if self._valley is None:
            self._valley = float(s[0])
        events: List[Event] = []
        level = 0.0
        for k, v in enumerate(s):
            i = i0 + k
            self._recent.append(v)
            if self._state == self._IDLE:
                self._valley = min(v, self._valley + self._leak)
                level = max(level, v - self._valley)
                if v - self._valley > thr:
                    self._state, self._start = self._ACTIVE, i
                    self._base, self._peak = self._valley, v
                    self._gated = bool(gate is not None and gate[k])
            else:
                self._peak = max(self._peak, v)
                self._gated |= bool(gate is not None and gate[k])
                level = max(level, self._peak - self._base)
                if v < self._peak - 0.5 * (self._peak - self._base):
                    event = self._finish(i)
                    if event:
                        events.append(event)
                    self._state, self._valley = self._IDLE, v
                elif i - self._start > max_len:  # never came back down: a step, not a blink
                    self._reanchor(i, v, thr)
        self.level = level
        return events

    def _reanchor(self, i: int, v: float, thr: float) -> None:
        """After a step, keep tracking a blink that may be riding on top of it."""
        recent = list(self._recent)
        j = int(np.argmin(recent))
        if v - recent[j] > thr:
            self._start = i - (len(recent) - 1) + j
            self._base, self._peak = recent[j], max(recent[j:])
        else:
            logger.debug("Rejected blink candidate at %.2fs: too long (gaze shift/drift)", self._start / self.fs)
            self._state, self._valley = self._IDLE, v

    def _finish(self, i: int) -> Optional[Event]:
        amplitude = self._peak - self._base
        duration = (i - self._start) / self.fs
        if duration < self.cfg.min_duration_s:
            reason = "too short"
        elif amplitude > self.cfg.max_amplitude_uv:
            reason = "too large (electrode pop?)"
        elif self._gated:
            reason = "during jaw clench"
        else:
            return Event("blink", t=self._start / self.fs, amplitude=amplitude, duration=duration)
        logger.debug("Rejected blink candidate at %.2fs (%.0f µV): %s", self._start / self.fs, amplitude, reason)
        return None


class ClenchDetector:
    """Detects a held jaw clench from the EMG envelope (20-100 Hz RMS)."""

    def __init__(self, cfg: ClenchConfig, fs: float, n_channels: int = 8):
        self.cfg = cfg
        self.fs = fs
        self.idx = channel_index(cfg.channels, n_channels)
        sos = np.vstack([bandpass_sos(cfg.band_hz[0], cfg.band_hz[1], fs), notch_sos(cfg.notch_hz, fs)])
        self._bandpass = StreamingFilter(sos)
        self._smooth = StreamingFilter(lowpass_sos(3.0, fs, order=2))  # ~100 ms RMS window
        self.baseline = RobustStats(fs, window_s=30.0)
        self._above_since: Optional[int] = None
        self._fired = False
        self._peak = 0.0
        self._gate_until = -1
        self.threshold = float("inf")
        self.level = 0.0

    def current_threshold(self) -> float:
        if np.isnan(self.baseline.low):
            return float("inf")
        base = self.cfg.threshold_uv if self.cfg.threshold_uv is not None else self.cfg.min_threshold_uv
        # Resting EMG level (20th percentile), so talking doesn't push the threshold up.
        return max(base, self.cfg.adaptive_k * self.baseline.low)

    def envelope(self, block: np.ndarray) -> np.ndarray:
        """EMG RMS envelope (µV), averaged over the clench channels."""
        y = self._bandpass(block[:, self.idx])
        power = self._smooth(np.mean(y * y, axis=1))
        return np.sqrt(np.maximum(power, 0.0))

    def process(self, block: np.ndarray, i0: int) -> Tuple[List[Event], np.ndarray]:
        """Returns clench events and a per-sample mask of 'jaw muscles active'."""
        env = self.envelope(block)
        self.baseline.update(env)
        self.threshold = thr = self.current_threshold()
        hold = self.cfg.min_hold_s * self.fs
        gate = np.zeros(len(env), dtype=bool)
        events: List[Event] = []
        for k, e in enumerate(env):
            i = i0 + k
            if e > thr:
                self._gate_until = i + int(0.3 * self.fs)
                if self._above_since is None:
                    self._above_since, self._peak = i, e
                self._peak = max(self._peak, e)
                if not self._fired and i - self._above_since >= hold:
                    self._fired = True
                    events.append(Event("clench", t=i / self.fs, amplitude=self._peak,
                                        duration=(i - self._above_since) / self.fs))
            elif e < 0.7 * thr:
                self._above_since, self._fired = None, False
            gate[k] = i <= self._gate_until
        self.level = float(env[-1]) if len(env) else 0.0
        return events, gate


class BlinkPatterns:
    """Groups blinks into single/double/triple patterns by the pauses between them.

    A blink only counts if it is at least `min_relative_amplitude` of the largest
    blink in the pattern, so a small spontaneous blink next to two deliberate ones
    doesn't turn a double blink into a triple.
    """

    NAMES = {1: "single_blink", 2: "double_blink", 3: "triple_blink"}

    def __init__(self, cfg: GestureConfig, max_count: int = 3):
        self.cfg = cfg
        self.max_count = max_count  # fire as soon as this many blinks are seen
        self._blinks: List[Event] = []

    def add(self, blink: Event) -> List[Event]:
        out: List[Event] = []
        if self._blinks and blink.t - self._blinks[-1].t > self.cfg.max_gap_s:
            out += self._flush(self._blinks[-1].t)
        if self._blinks and blink.t - self._blinks[-1].t < self.cfg.min_gap_s:
            return out
        self._blinks.append(blink)
        if len(self._significant()) >= self.max_count:
            out += self._flush(blink.t)
        return out

    def tick(self, now: float, busy: bool = False) -> List[Event]:
        """Close the current pattern once no further blink can join it."""
        if self._blinks and not busy and now - self._blinks[-1].t > self.cfg.max_gap_s:
            return self._flush(now)
        return []

    def reset(self) -> None:
        self._blinks = []

    def _significant(self) -> List[Event]:
        largest = max(b.amplitude for b in self._blinks)
        return [b for b in self._blinks if b.amplitude >= self.cfg.min_relative_amplitude * largest]

    def _flush(self, t: float) -> List[Event]:
        kept = self._significant()
        self._blinks = []
        n = len(kept)
        return [Event(self.NAMES.get(n, "multi_blink"), t=t, amplitude=max(b.amplitude for b in kept), count=n)]


class GestureEngine:
    """Feeds raw blocks through all detectors and maps gestures to actions."""

    WARMUP_S = 3.0  # filters, baseline and noise estimates settle

    def __init__(self, cfg: Config, fs: float, n_channels: int = 8):
        self.cfg = cfg
        self.fs = fs
        self.blink = BlinkDetector(cfg.blink, fs, n_channels)
        self.clench = ClenchDetector(cfg.clench, fs, n_channels)
        counts = [n for g, n in (("double_blink", 2), ("triple_blink", 3)) if cfg.mapping.get(g)]
        self.patterns = BlinkPatterns(cfg.gestures, max_count=max(counts, default=3))
        self._gate_blinks = bool(cfg.clench.gate_blinks and cfg.mapping.get("clench"))
        self._n = 0
        self._cooldown_until = float("-inf")

    @property
    def time(self) -> float:
        return self._n / self.fs

    def process(self, block: np.ndarray) -> List[Event]:
        i0 = self._n
        self._n += len(block)
        clench_events, gate = self.clench.process(block, i0)
        blinks = self.blink.process(block, i0, gate if self._gate_blinks else None)
        if self.time < self.WARMUP_S:
            return []

        gestures: List[Event] = []
        for b in blinks:
            if b.t >= self._cooldown_until:
                gestures += self.patterns.add(b)
        gestures += self.patterns.tick(self.time, busy=self.blink.busy)
        gestures += clench_events

        for g in sorted(gestures, key=lambda e: e.t):
            action = self.cfg.mapping.get(g.kind)
            if action and g.t >= self._cooldown_until:
                g.action = action
                self._cooldown_until = g.t + self.cfg.gestures.cooldown_s
                self.patterns.reset()
        return blinks + gestures
