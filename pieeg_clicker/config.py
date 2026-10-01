"""Configuration: dataclasses with sensible defaults, stored as a JSON file."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

ACTIONS = ("next", "previous", "blank")
GESTURES = ("double_blink", "triple_blink", "clench")
DATA_RATES = (250, 500, 1000)
GAINS = (1, 2, 4, 6, 8, 12, 24)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pieeg-clicker" / "config.json"


@dataclass
class HardwareConfig:
    spi_bus: int = 0
    spi_device: int = 0
    spi_speed_hz: int = 1_000_000
    drdy_gpio: int = 26               # BCM number of the ADS1299 DRDY line (header pin 37)
    gpiochip: Optional[str] = None    # e.g. "/dev/gpiochip0"; None = find the chip owning "GPIO<drdy_gpio>"
    sample_rate: int = 250            # ADS1299 data rate (SPS)
    gain: int = 1                     # PGA gain applied to every channel
    block_size: int = 10              # samples per processing block (10 @ 250 SPS = 40 ms)
    n_channels: int = 8               # PiEEG-8: 8; PiEEG-16: 16
    spi_device_2: int = 1             # second ADS1299: /dev/spidev0.1
    drdy_gpio_2: int = 13             # second ADS1299 DRDY (header pin 33)
    cs_gpio_2: int = 19               # second ADS1299 chip select (header pin 35)


@dataclass
class BlinkConfig:
    channels: List[int] = field(default_factory=lambda: [1, 2])  # 1-based inputs wired to Fp1/Fp2
    lowpass_hz: float = 10.0
    polarity: int = 1                 # +1: blinks are positive at Fp1/Fp2 vs an ear reference
    threshold_uv: Optional[float] = None  # written by `calibrate`
    min_threshold_uv: float = 80.0    # used until calibrated
    adaptive_k: float = 6.0           # threshold is never below k * robust noise sigma
    min_duration_s: float = 0.05
    max_duration_s: float = 0.6       # rise-to-fall time; slower = gaze shift / drift
    max_amplitude_uv: float = 2000.0  # larger = electrode pop or movement


@dataclass
class ClenchConfig:
    channels: List[int] = field(default_factory=lambda: [1, 2])
    band_hz: List[float] = field(default_factory=lambda: [20.0, 100.0])
    notch_hz: List[float] = field(default_factory=lambda: [50.0, 60.0])  # mains + harmonics
    threshold_uv: Optional[float] = None  # written by `calibrate`
    min_threshold_uv: float = 25.0
    adaptive_k: float = 5.0           # threshold is never below k * resting EMG envelope
    min_hold_s: float = 0.4
    gate_blinks: bool = True          # ignore "blinks" while the jaw is clenched


@dataclass
class GestureConfig:
    min_gap_s: float = 0.2            # blinks closer than this are treated as one
    max_gap_s: float = 0.7            # longest pause between blinks of one pattern
    cooldown_s: float = 1.0           # dead time after every action
    min_relative_amplitude: float = 0.5  # blinks smaller than this x the pattern's largest are ignored


@dataclass
class OutputConfig:
    mode: str = "udp"                 # udp | uinput | console
    host: str = "255.255.255.255"     # laptop IP address, or broadcast
    port: int = 5005
    token: str = ""                   # optional shared secret checked by the receiver


def _default_mapping() -> Dict[str, Optional[str]]:
    return {"double_blink": "next", "triple_blink": "previous", "clench": None}


def _default_keys() -> Dict[str, str]:
    return {"next": "page_down", "previous": "page_up", "blank": "b"}


@dataclass
class Config:
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
    blink: BlinkConfig = field(default_factory=BlinkConfig)
    clench: ClenchConfig = field(default_factory=ClenchConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    mapping: Dict[str, Optional[str]] = field(default_factory=_default_mapping)
    keys: Dict[str, str] = field(default_factory=_default_keys)

    @classmethod
    def load(cls, path: Union[str, Path, None]) -> "Config":
        """Load `path` on top of the defaults (a missing file just gives the defaults)."""
        cfg = cls()
        if path is not None and Path(path).exists():
            data = json.loads(Path(path).read_text())
            _merge(cfg, data, "config")
            logger.info("Loaded config from %s", path)
        cfg.validate()
        return cfg

    def save(self, path: Union[str, Path]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    def validate(self) -> None:
        hw = self.hardware
        if type(hw.n_channels) is not int or hw.n_channels not in (8, 16):
            raise ValueError("hardware.n_channels must be 8 (PiEEG-8) or 16 (PiEEG-16)")
        if hw.n_channels == 16:
            if hw.spi_device_2 == hw.spi_device:
                raise ValueError("hardware.spi_device_2 must differ from hardware.spi_device")
            if len({hw.drdy_gpio, hw.drdy_gpio_2, hw.cs_gpio_2}) != 3:
                raise ValueError("hardware.drdy_gpio, drdy_gpio_2 and cs_gpio_2 must be different")
        if hw.sample_rate not in DATA_RATES:
            raise ValueError(f"hardware.sample_rate must be one of {DATA_RATES}")
        if hw.gain not in GAINS:
            raise ValueError(f"hardware.gain must be one of {GAINS}")
        for name, chans in (("blink", self.blink.channels), ("clench", self.clench.channels)):
            if not chans or any(type(c) is not int or not 1 <= c <= hw.n_channels for c in chans):
                raise ValueError(f"{name}.channels must be PiEEG inputs 1..{hw.n_channels}, got {chans}")
        if self.blink.polarity not in (1, -1):
            raise ValueError("blink.polarity must be 1 or -1")
        lo, hi = self.clench.band_hz
        if not 0 < lo < hi:
            raise ValueError("clench.band_hz must be [low, high] with 0 < low < high")
        for gesture, action in self.mapping.items():
            if gesture not in GESTURES:
                raise ValueError(f"unknown gesture {gesture!r} in mapping (valid: {GESTURES})")
            if action is not None and action not in ACTIONS:
                raise ValueError(f"unknown action {action!r} for {gesture} (valid: {ACTIONS})")
        if self.output.mode not in ("udp", "uinput", "console"):
            raise ValueError("output.mode must be udp, uinput or console")


def _merge(obj: Any, data: Dict[str, Any], where: str) -> None:
    """Recursively copy JSON values onto a dataclass instance, warning about unknown keys."""
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            logger.warning("Ignoring unknown config key %s.%s", where, key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value, f"{where}.{key}")
        elif isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        else:
            setattr(obj, key, value)
