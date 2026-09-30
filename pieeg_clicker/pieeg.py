"""PiEEG driver: TI ADS1299 over SPI (spidev), DRDY watched with libgpiod edge events."""
from __future__ import annotations

import glob
import logging
import re
import time
from datetime import timedelta
from typing import Optional

import numpy as np

from .config import HardwareConfig

logger = logging.getLogger(__name__)

# ADS1299 opcodes and registers (TI datasheet SBAS499).
WAKEUP, STOP, RESET, START = 0x02, 0x0A, 0x06, 0x08
RDATAC, SDATAC = 0x10, 0x11
RREG, WREG = 0x20, 0x40
ID, CONFIG1, CONFIG2, CONFIG3, LOFF, CH1SET = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
BIAS_SENSP, BIAS_SENSN, LOFF_SENSP, LOFF_SENSN, LOFF_FLIP = 0x0D, 0x0E, 0x0F, 0x10, 0x11
GPIO, MISC1, CONFIG4 = 0x14, 0x15, 0x17

DATA_RATE_BITS = {250: 0b110, 500: 0b101, 1000: 0b100}
GAIN_BITS = {1: 0b000, 2: 0b001, 4: 0b010, 6: 0b011, 8: 0b100, 12: 0b101, 24: 0b110}
VREF = 4.5
N_CHANNELS = 8
FRAME_BYTES = 3 + 3 * N_CHANNELS  # 24-bit status word + 8 x 24-bit samples


def _import_gpiod():
    try:
        import gpiod
    except ImportError as exc:
        raise RuntimeError("libgpiod Python bindings missing: `sudo apt install python3-libgpiod` "
                           "(or `pip install gpiod`).") from exc
    return gpiod


def _chip_paths():
    return sorted(glob.glob("/dev/gpiochip*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))


class _DrdyV2:
    """DRDY falling edges via the libgpiod 2.x API (PyPI gpiod >= 2, Raspberry Pi OS Trixie)."""

    def __init__(self, gpiod, gpio: int, chip: Optional[str]):
        from gpiod.line import Direction, Edge

        path, offset = chip, gpio
        if chip is None:  # find the chip that owns "GPIO26" (gpiochip0 on Pi 4/5, gpiochip4 on older Pi 5 kernels)
            path = "/dev/gpiochip0"
            for candidate in _chip_paths():
                try:
                    with gpiod.Chip(candidate) as c:
                        offset = c.line_offset_from_id(f"GPIO{gpio}")
                    path = candidate
                    break
                except (OSError, ValueError):
                    continue
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.FALLING)
        self._req = gpiod.request_lines(path, consumer="pieeg-clicker", config={offset: settings})
        logger.info("DRDY on %s line %d", path, offset)

    def wait(self, timeout_s: float) -> int:
        if not self._req.wait_edge_events(timedelta(seconds=timeout_s)):
            return 0
        return len(self._req.read_edge_events())

    def close(self) -> None:
        self._req.release()


class _DrdyV1:
    """DRDY falling edges via the libgpiod 1.x API (python3-libgpiod on Raspberry Pi OS Bookworm)."""

    def __init__(self, gpiod, gpio: int, chip: Optional[str]):
        line = gpiod.find_line(f"GPIO{gpio}") if chip is None else None
        if line is None:
            line = gpiod.Chip(chip or "gpiochip0").get_line(gpio)
        line.request(consumer="pieeg-clicker", type=gpiod.LINE_REQ_EV_FALLING_EDGE)
        self._line = line
        logger.info("DRDY on %s line %d", line.owner().name(), line.offset())

    def wait(self, timeout_s: float) -> int:
        sec = int(timeout_s)
        if not self._line.event_wait(sec=sec, nsec=int((timeout_s - sec) * 1e9)):
            return 0
        return len(self._line.event_read_multiple())

    def close(self) -> None:
        self._line.release()


def open_drdy(gpio: int, chip: Optional[str]):
    gpiod = _import_gpiod()
    if hasattr(gpiod, "request_lines"):
        return _DrdyV2(gpiod, gpio, chip)
    return _DrdyV1(gpiod, gpio, chip)


class PiEEG:
    """8-channel PiEEG. `read()` returns blocks of shape (block_size, 8) in microvolts."""

    n_channels = N_CHANNELS

    def __init__(self, hw: HardwareConfig):
        self.hw = hw
        self.fs = hw.sample_rate
        self.block_size = hw.block_size
        self.full_scale_uv = VREF / hw.gain * 1e6
        self._uv_per_lsb = VREF / hw.gain / (2 ** 23 - 1) * 1e6
        self._spi = None
        self._drdy = None
        self.missed = 0      # samples lost because we were too slow
        self.bad_frames = 0  # frames without the 0b1100 status header

    # --- low-level SPI -------------------------------------------------------------
    def _command(self, opcode: int) -> None:
        self._spi.xfer2([opcode])
        time.sleep(0.001)

    def _write(self, register: int, value: int) -> None:
        self._spi.xfer2([WREG | register, 0x00, value])
        time.sleep(0.001)

    def _read(self, register: int) -> int:
        return self._spi.xfer2([RREG | register, 0x00, 0x00])[2]

    # --- source interface ----------------------------------------------------------
    def start(self) -> None:
        try:
            import spidev
        except ImportError as exc:
            raise RuntimeError("spidev missing: `sudo apt install python3-spidev` (or `pip install spidev`).") from exc
        hw = self.hw
        self._spi = spidev.SpiDev()
        try:
            self._spi.open(hw.spi_bus, hw.spi_device)
        except FileNotFoundError as exc:
            raise RuntimeError(f"/dev/spidev{hw.spi_bus}.{hw.spi_device} not found: enable SPI with "
                               "`sudo raspi-config nonint do_spi 0` and reboot.") from exc
        self._spi.max_speed_hz = hw.spi_speed_hz
        self._spi.mode = 0b01  # ADS1299: CPOL=0, CPHA=1
        self._spi.bits_per_word = 8
        self._drdy = open_drdy(hw.drdy_gpio, hw.gpiochip)
        self._configure()
        self._command(RDATAC)
        self._command(START)
        logger.info("PiEEG streaming at %d SPS, gain %d", self.fs, hw.gain)

    def _configure(self) -> None:
        self._command(WAKEUP)
        self._command(STOP)
        self._command(RESET)
        time.sleep(0.01)
        self._command(SDATAC)  # the chip powers up in continuous-read mode
        device_id = self._read(ID)
        if device_id in (0x00, 0xFF):
            raise RuntimeError(f"No answer from the ADS1299 (ID register = 0x{device_id:02X}). Is the PiEEG "
                               "seated on the header, SPI enabled and the battery switched on?")
        if device_id & 0x1F != 0x1E:
            logger.warning("Unexpected ADS1299 ID 0x%02X (an 8-channel ADS1299 reads 0x3E)", device_id)
        config1 = 0x90 | DATA_RATE_BITS[self.fs]
        chset = GAIN_BITS[self.hw.gain] << 4  # normal electrode input, SRB2 open
        writes = [
            (GPIO, 0x80), (CONFIG1, config1), (CONFIG2, 0xD4), (CONFIG3, 0xFF), (LOFF, 0x00),
            (BIAS_SENSP, 0x00), (BIAS_SENSN, 0x00), (LOFF_SENSP, 0x00), (LOFF_SENSN, 0x00),
            (LOFF_FLIP, 0x00), (MISC1, 0x20), (CONFIG4, 0x00),  # MISC1: SRB1 = common reference
        ] + [(CH1SET + ch, chset) for ch in range(N_CHANNELS)]
        for register, value in writes:
            self._write(register, value)
        if self._read(CONFIG1) != config1:
            raise RuntimeError("ADS1299 register write did not stick (SPI wiring or speed problem?)")

    def read(self) -> np.ndarray:
        rows = []
        while len(rows) < self.block_size:
            edges = self._drdy.wait(1.0)
            if edges == 0:
                raise RuntimeError("No data from the PiEEG for 1 s (DRDY never went low). Check the board, "
                                   "the battery and hardware.drdy_gpio.")
            self.missed += edges - 1
            sample = self._decode(self._spi.readbytes(FRAME_BYTES))
            if sample is not None:
                rows.append(sample)
        return np.array(rows)

    def _decode(self, frame) -> Optional[np.ndarray]:
        b = np.frombuffer(bytes(frame), dtype=np.uint8)
        if b[0] & 0xF0 != 0xC0:  # every ADS1299 frame starts with 0b1100
            self.bad_frames += 1
            return None
        d = b[3:].reshape(N_CHANNELS, 3).astype(np.int32)
        raw = (d[:, 0] << 16) | (d[:, 1] << 8) | d[:, 2]
        raw -= (raw & 0x800000) << 1  # sign-extend 24-bit two's complement
        return raw * self._uv_per_lsb

    def stop(self) -> None:
        if self._spi is not None:
            try:
                self._command(STOP)
                self._command(SDATAC)
            finally:
                self._spi.close()
                self._spi = None
        if self._drdy is not None:
            self._drdy.close()
            self._drdy = None
        if self.missed or self.bad_frames:
            logger.info("Lost %d samples, dropped %d malformed frames", self.missed, self.bad_frames)
