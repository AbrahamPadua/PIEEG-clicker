"""Causal, block-wise DSP helpers (everything here runs in real time)."""
from __future__ import annotations

from typing import Iterable

import numpy as np
from scipy import signal


def lowpass_sos(cutoff_hz: float, fs: float, order: int = 4) -> np.ndarray:
    return signal.butter(order, cutoff_hz, btype="lowpass", fs=fs, output="sos")


def highpass_sos(cutoff_hz: float, fs: float, order: int = 2) -> np.ndarray:
    return signal.butter(order, cutoff_hz, btype="highpass", fs=fs, output="sos")


def bandpass_sos(low_hz: float, high_hz: float, fs: float, order: int = 4) -> np.ndarray:
    high_hz = min(high_hz, 0.45 * fs)  # stay clear of Nyquist at low sample rates
    return signal.butter(order, [low_hz, high_hz], btype="bandpass", fs=fs, output="sos")


def notch_sos(mains_hz: Iterable[float], fs: float, q: float = 30.0) -> np.ndarray:
    """Notch filters at each mains frequency and its harmonics below Nyquist."""
    sections = []
    for f0 in mains_hz:
        harmonic = f0
        while 0 < harmonic < 0.48 * fs:
            b, a = signal.iirnotch(harmonic, q, fs=fs)
            sections.append(signal.tf2sos(b, a))
            harmonic += f0
    return np.vstack(sections) if sections else np.zeros((0, 6))


class StreamingFilter:
    """IIR filter (second-order sections) that keeps its state between blocks.

    Blocks are shaped (n_samples,) or (n_samples, n_channels).
    """

    def __init__(self, sos: np.ndarray):
        self.sos = np.asarray(sos, dtype=float).reshape(-1, 6)
        self._zi = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if len(self.sos) == 0 or len(x) == 0:
            return x
        flat = x.ndim == 1
        x2 = x[:, None] if flat else x
        if self._zi is None:
            # Start in steady state for the first sample so electrode DC offsets
            # (often tens of mV) don't cause a long start-up transient.
            self._zi = signal.sosfilt_zi(self.sos)[:, :, None] * x2[0][None, None, :]
        y, self._zi = signal.sosfilt(self.sos, x2, axis=0, zi=self._zi)
        return y[:, 0] if flat else y


class RingBuffer:
    """Fixed-length 1-D history of the most recent samples."""

    def __init__(self, length: int):
        self._buf = np.zeros(max(1, int(length)))
        self._pos = 0
        self._count = 0

    def extend(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=float)[-len(self._buf):]
        n = len(x)
        end = self._pos + n
        if end <= len(self._buf):
            self._buf[self._pos:end] = x
        else:
            split = len(self._buf) - self._pos
            self._buf[self._pos:] = x[:split]
            self._buf[:n - split] = x[split:]
        self._pos = end % len(self._buf)
        self._count = min(self._count + n, len(self._buf))

    def __len__(self) -> int:
        return self._count

    def values(self) -> np.ndarray:
        if self._count < len(self._buf):
            return self._buf[:self._count]
        return self._buf


class RobustStats:
    """Median, MAD-based sigma and 20th percentile over a sliding window, refreshed periodically."""

    def __init__(self, fs: float, window_s: float = 20.0, update_s: float = 0.5, min_s: float = 2.0):
        self._ring = RingBuffer(int(window_s * fs))
        self._update_every = max(1, int(update_s * fs))
        self._min_count = int(min_s * fs)
        self._since_update = 0
        self.median = float("nan")
        self.sigma = float("nan")
        self.low = float("nan")  # 20th percentile: the resting level of a bursty signal

    def update(self, x: np.ndarray) -> None:
        self._ring.extend(x)
        self._since_update += len(x)
        if self._since_update >= self._update_every and len(self._ring) >= self._min_count:
            self._since_update = 0
            v = self._ring.values()
            self.median = float(np.median(v))
            self.sigma = float(1.4826 * np.median(np.abs(v - self.median)))
            self.low = float(np.percentile(v, 20))
