"""Synthetic PiEEG-like data, so the clicker can be tried and tested without hardware."""
from __future__ import annotations

import time
from typing import List, Tuple

import numpy as np
from scipy.signal import sosfilt

from .dsp import StreamingFilter, bandpass_sos, lowpass_sos

# How strongly each artifact shows on inputs 1..8 (assumed Fp1, Fp2, F7, F8, C3, C4, O1, O2).
BLINK_WEIGHTS = np.array([1.0, 0.95, 0.55, 0.5, 0.25, 0.25, 0.08, 0.08])
EMG_WEIGHTS = np.array([0.7, 0.7, 1.0, 1.0, 0.5, 0.5, 0.3, 0.3])
DEMO_SCRIPT = ("double_blink", "triple_blink", "clench")


class SyntheticSource:
    """Background EEG + natural blinks + gaze shifts, plus gestures on demand.

    `demo=True` performs a double blink, triple blink and jaw clench in turn every
    `demo_every_s` seconds. `cue()` lets calibration prompts trigger a response.
    """

    n_channels = 8
    full_scale_uv = 4.5e6

    def __init__(self, fs: int = 250, block_size: int = 10, realtime: bool = True,
                 demo: bool = True, demo_every_s: float = 6.0, seed=None):
        self.fs = fs
        self.block_size = block_size
        self.realtime = realtime
        self.rng = np.random.default_rng(seed)
        self._i = 0
        self._events: List[Tuple[int, np.ndarray]] = []
        self._offsets = self.rng.uniform(-30_000, 30_000, self.n_channels)  # electrode DC offsets
        self._drift = np.zeros(self.n_channels)
        self._eeg = StreamingFilter(lowpass_sos(3.0, fs, order=1))
        self._emg_band = bandpass_sos(25.0, 110.0, fs)
        self._next_blink = self._natural_gap()
        self._next_glance = int(self.rng.uniform(3, 6) * fs)
        self._demo_every = int(demo_every_s * fs) if demo else 0
        self._demo_step = 0
        self._t0 = 0.0

    # --- Source interface -------------------------------------------------
    def start(self) -> None:
        self._t0 = time.monotonic()

    def stop(self) -> None:
        pass

    def read(self) -> np.ndarray:
        n, i = self.block_size, self._i
        self._schedule(i + n)
        t = (i + np.arange(n)) / self.fs
        noise = self.rng.normal(0.0, 41.0, (n, self.n_channels))
        x = self._eeg(noise) + self.rng.normal(0.0, 2.0, (n, self.n_channels))
        x += np.outer(6.0 * np.sin(2 * np.pi * 10.0 * t), [2, 2, 2, 2, 3, 3, 6, 6]) / 6.0  # alpha
        x += 3.0 * np.sin(2 * np.pi * 50.0 * t)[:, None]  # mains hum
        self._drift += self.rng.normal(0.0, 0.3, self.n_channels) * np.sqrt(n)
        x += self._offsets + self._drift
        x += self._render(i, n)
        self._i += n
        if self.realtime:
            delay = self._t0 + self._i / self.fs - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        return x

    def cue(self, kind: str, duration_s: float = 0.0) -> None:
        """React to a calibration prompt like a cooperative user (after ~0.4 s)."""
        start = self._i + int(self.rng.uniform(0.3, 0.6) * self.fs)
        if kind == "blink":
            self._blink(start, deliberate=True)
        elif kind == "clench":
            self._clench(start)
        elif kind == "talk":
            self._talk(start, duration_s)

    # --- artifact generators ------------------------------------------------
    def _natural_gap(self) -> int:
        return int((1.5 + self.rng.exponential(2.5)) * self.fs)  # ~15 blinks/min, never back-to-back

    def _add(self, start: int, wave: np.ndarray) -> None:
        self._events.append((start, wave if wave.ndim == 2 else np.outer(wave, np.ones(self.n_channels))))

    def _blink(self, start: int, deliberate: bool) -> None:
        amp = self.rng.uniform(220, 350) if deliberate else self.rng.uniform(70, 140)
        dur = self.rng.uniform(0.30, 0.40) if deliberate else self.rng.uniform(0.20, 0.30)
        length = int(dur * self.fs)
        shape = np.sin(np.pi * np.arange(length) / length) ** 2
        self._add(start, np.outer(amp * shape, BLINK_WEIGHTS * self.rng.uniform(0.9, 1.1, self.n_channels)))

    def _pattern(self, start: int, count: int) -> None:
        for _ in range(count):
            self._blink(start, deliberate=True)
            start += int(self.rng.uniform(0.35, 0.5) * self.fs)

    def _emg_burst(self, start: int, dur: float, rms: float) -> None:
        length = int(dur * self.fs)
        pad = 64  # let the band-pass settle before the burst starts
        white = self.rng.normal(0.0, 1.0, (length + pad, self.n_channels))
        burst = sosfilt(self._emg_band, white, axis=0)[pad:]
        burst *= rms / burst.std()
        ramp = np.minimum(1.0, np.minimum(np.arange(length), np.arange(length)[::-1]) / (0.05 * self.fs))
        self._add(start, burst * ramp[:, None] * EMG_WEIGHTS)

    def _clench(self, start: int) -> None:
        self._emg_burst(start, self.rng.uniform(0.9, 1.3), rms=self.rng.uniform(50, 80))
        length = int(0.6 * self.fs)  # jaw movement also shifts the electrodes a little
        self._add(start, np.outer(25.0 * np.sin(np.pi * np.arange(length) / length), EMG_WEIGHTS))

    def _talk(self, start: int, duration_s: float) -> None:
        t = start
        while t < start + duration_s * self.fs:
            dur = self.rng.uniform(0.15, 0.4)
            self._emg_burst(t, dur, rms=self.rng.uniform(6, 14))
            t += int((dur + self.rng.uniform(0.05, 0.3)) * self.fs)

    def _glance(self, start: int) -> None:
        """Look down at notes for 1-3 s, then back up: a vertical-EOG step pair."""
        amp = self.rng.uniform(60, 150)
        down = int(self.rng.uniform(1.0, 3.0) * self.fs)
        ramp = int(0.05 * self.fs)
        wave = np.full(down + 2 * ramp, -amp)
        wave[:ramp] = np.linspace(0, -amp, ramp)
        wave[-ramp:] = np.linspace(-amp, 0, ramp)
        self._add(start, np.outer(wave, BLINK_WEIGHTS))

    def _schedule(self, end: int) -> None:
        while self._next_blink < end:
            self._blink(self._next_blink, deliberate=False)
            self._next_blink += self._natural_gap()
        while self._next_glance < end:
            self._glance(self._next_glance)
            self._next_glance += int(self.rng.uniform(5, 12) * self.fs)
        if self._demo_every and self._i // self._demo_every != end // self._demo_every:
            gesture = DEMO_SCRIPT[self._demo_step % len(DEMO_SCRIPT)]
            self._demo_step += 1
            start = (end // self._demo_every) * self._demo_every
            if gesture == "clench":
                self._clench(start)
            else:
                self._pattern(start, 2 if gesture == "double_blink" else 3)

    def _render(self, i: int, n: int) -> np.ndarray:
        out = np.zeros((n, self.n_channels))
        keep = []
        for start, wave in self._events:
            lo, hi = max(start, i), min(start + len(wave), i + n)
            if lo < hi:
                out[lo - i:hi - i] += wave[lo - start:hi - start]
            if start + len(wave) > i + n:
                keep.append((start, wave))
        self._events = keep
        return out
