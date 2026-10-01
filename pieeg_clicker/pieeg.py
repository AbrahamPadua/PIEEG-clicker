"""PiEEG driver: TI ADS1299 over SPI (spidev), DRDY watched with libgpiod edge events.

Wiring and register setup follow the official PiEEG-8 and PiEEG-16 scripts:
chip 1 uses SPI0 CE0 (/dev/spidev0.0), DRDY on BCM GPIO26. PiEEG-16 adds
/dev/spidev0.1 with manual chip select on GPIO19 and DRDY on GPIO13. Each chip
provides a separate 27-byte frame. REF is on SRB1 (MISC1 = 0x20).
"""
from __future__ import annotations

import glob
import logging
import re
import time
from contextlib import ExitStack, contextmanager
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
MAX_JUMP_UV = 2500.0  # bigger sample-to-sample jumps are SPI glitches (cf. PiEEG-server)


def _import_gpiod():
    try:
        import gpiod
    except ImportError as exc:
        raise RuntimeError("libgpiod Python bindings missing: `sudo apt install python3-libgpiod` "
                           "(or `pip install gpiod`).") from exc
    return gpiod


def _chip_paths():
    return sorted(glob.glob("/dev/gpiochip*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))


def _v2_line(gpiod, gpio: int, chip: Optional[str]):
    if chip is not None:
        return chip, gpio
    for candidate in _chip_paths():
        try:
            with gpiod.Chip(candidate) as c:
                offset = c.line_offset_from_id(f"GPIO{gpio}")
            return candidate, offset
        except (OSError, ValueError):
            continue
    raise RuntimeError(f"Cannot find GPIO{gpio} on /dev/gpiochip*. "
                       "Set hardware.gpiochip to the chip containing the Pi's header GPIOs.")


def _legacy_line(gpiod, gpio: int, chip: Optional[str]):
    line = gpiod.find_line(f"GPIO{gpio}") if chip is None else None
    if not line:
        constructor = getattr(gpiod, "Chip", None) or gpiod.chip
        line = constructor(chip or "gpiochip0").get_line(gpio)
    return line


# The header GPIOs are gpiochip0 on the Pi 4 and on the Pi 5 since mid-2024 kernels;
# earlier Pi 5 kernels used gpiochip4, and the number has moved again since. With
# `gpiochip` unset we look the line up by its name ("GPIO26"), which works on all of them.

class _DrdyV2:
    """libgpiod 2.x API: PyPI gpiod >= 2, python3-libgpiod on Raspberry Pi OS Trixie."""

    def __init__(self, gpiod, gpio: int, chip: Optional[str]):
        from gpiod.line import Direction, Edge

        path, offset = _v2_line(gpiod, gpio, chip)
        settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.FALLING)
        self._req = gpiod.request_lines(path, consumer="pieeg-clicker", config={offset: settings})
        logger.info("DRDY on %s line %d (libgpiod 2)", path, offset)

    def wait(self, timeout_s: float) -> int:
        if not self._req.wait_edge_events(timedelta(seconds=timeout_s)):
            return 0
        return len(self._req.read_edge_events())

    def close(self) -> None:
        self._req.release()


class _DrdyV1:
    """libgpiod 1.x API: python3-libgpiod on Raspberry Pi OS Bookworm."""

    def __init__(self, gpiod, gpio: int, chip: Optional[str]):
        line = _legacy_line(gpiod, gpio, chip)
        line.request(consumer="pieeg-clicker", type=gpiod.LINE_REQ_EV_FALLING_EDGE)
        self._line = line
        logger.info("DRDY on %s line %d (libgpiod 1)", line.owner().name(), line.offset())

    def wait(self, timeout_s: float) -> int:
        sec = int(timeout_s)
        if not self._line.event_wait(sec=sec, nsec=int((timeout_s - sec) * 1e9)):
            return 0
        return len(self._line.event_read_multiple())

    def close(self) -> None:
        self._line.release()


class _DrdyCxx:
    """PyPI gpiod 1.5.x (python3-gpiod by hhk7734), which PiEEG's quick-start installs."""

    def __init__(self, gpiod, gpio: int, chip: Optional[str]):
        line = _legacy_line(gpiod, gpio, chip)
        request = gpiod.line_request()
        request.consumer = "pieeg-clicker"
        request.request_type = gpiod.line_request.EVENT_FALLING_EDGE
        line.request(request)
        self._line = line
        logger.info("DRDY on line %d (gpiod 1.5)", line.offset)

    def wait(self, timeout_s: float) -> int:
        if not self._line.event_wait(timedelta(seconds=timeout_s)):
            return 0
        edges = 0
        while True:  # drain queued events: no event_read_multiple() in this package
            self._line.event_read()
            edges += 1
            if not self._line.event_wait(timedelta(0)):
                return edges

    def close(self) -> None:
        self._line.release()


def open_drdy(gpio: int, chip: Optional[str]):
    gpiod = _import_gpiod()
    if hasattr(gpiod, "request_lines"):
        return _DrdyV2(gpiod, gpio, chip)
    if hasattr(gpiod, "LINE_REQ_EV_FALLING_EDGE"):
        return _DrdyV1(gpiod, gpio, chip)
    if hasattr(gpiod, "line_request"):
        return _DrdyCxx(gpiod, gpio, chip)
    raise RuntimeError(f"Unrecognised gpiod module {getattr(gpiod, '__file__', gpiod)!r}")


class _ChipSelect:
    """PiEEG-16 chip 2's active-low GPIO select, initially deselected."""

    def __init__(self, gpio: int, chip: Optional[str]):
        gpiod = _import_gpiod()
        if hasattr(gpiod, "request_lines"):
            from gpiod.line import Direction, Value

            path, offset = _v2_line(gpiod, gpio, chip)
            settings = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)
            request = gpiod.request_lines(path, consumer="pieeg-clicker-cs", config={offset: settings})
            self._set = lambda value: request.set_value(offset, Value.ACTIVE if value else Value.INACTIVE)
            self._release = request.release
        else:
            line = _legacy_line(gpiod, gpio, chip)
            if hasattr(gpiod, "LINE_REQ_DIR_OUT"):
                line.request(consumer="pieeg-clicker-cs", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
            elif hasattr(gpiod, "line_request"):
                request = gpiod.line_request()
                request.consumer = "pieeg-clicker-cs"
                request.request_type = gpiod.line_request.DIRECTION_OUTPUT
                line.request(request, default_val=1)
            else:
                raise RuntimeError(f"Unrecognised gpiod module {getattr(gpiod, '__file__', gpiod)!r}")
            self._set = line.set_value
            self._release = line.release
        logger.info("Chip 2 select on GPIO%d (active low)", gpio)

    def set(self, value: int) -> None:
        self._set(value)

    def close(self) -> None:
        try:
            self.set(1)
        finally:
            self._release()


class PiEEG:
    """PiEEG-8 or PiEEG-16; blocks have shape (block_size, n_channels), in microvolts."""

    n_channels = N_CHANNELS

    def __init__(self, hw: HardwareConfig):
        if hw.n_channels not in (8, 16):
            raise ValueError("hardware.n_channels must be 8 or 16")
        self.hw = hw
        self.n_channels = hw.n_channels
        self.fs = hw.sample_rate
        self.block_size = hw.block_size
        self.full_scale_uv = VREF / hw.gain * 1e6
        # Datasheet LSB = VREF / gain / 2^23 (0.536 µV at gain 1). PiEEG's example scripts
        # use 4.5e6 / 16777215 (half of that); calibration makes the difference irrelevant.
        self._uv_per_lsb = VREF / hw.gain / (2 ** 23 - 1) * 1e6
        self._spi = None
        self._drdy = None
        self._spi2 = None
        self._drdy2 = None
        self._cs2 = None
        self._last: Optional[np.ndarray] = None
        self._held = 0
        self.missed = 0      # samples lost because we were too slow
        self.bad_frames = 0  # frames without the 0b1100 status header
        self.glitches = 0    # frames replaced because of an impossible jump

    # --- low-level SPI -------------------------------------------------------------
    @contextmanager
    def _selected_spi(self, chip: int):
        if chip == 1:
            yield self._spi
        else:
            self._cs2.set(0)
            try:
                yield self._spi2
            finally:
                self._cs2.set(1)

    def _command(self, opcode: int, chip: int = 1) -> None:
        with self._selected_spi(chip) as spi:
            spi.xfer2([opcode])
        time.sleep(0.001)

    def _write(self, register: int, value: int, chip: int = 1) -> None:
        with self._selected_spi(chip) as spi:
            spi.xfer2([WREG | register, 0x00, value])
        time.sleep(0.001)

    def _read(self, register: int, chip: int = 1) -> int:
        with self._selected_spi(chip) as spi:
            return spi.xfer2([RREG | register, 0x00, 0x00])[2]

    # --- source interface ----------------------------------------------------------
    def start(self) -> None:
        try:
            import spidev
        except ImportError as exc:
            raise RuntimeError("spidev missing: `sudo apt install python3-spidev` (or `pip install spidev`).") from exc
        hw = self.hw
        try:
            if self.n_channels == 16:
                # Keep chip 2 deselected while initializing and reading chip 1.
                self._cs2 = _ChipSelect(hw.cs_gpio_2, hw.gpiochip)
            devices = [("_spi", hw.spi_device)]
            if self.n_channels == 16:
                devices.append(("_spi2", hw.spi_device_2))
            for attr, device in devices:
                spi = spidev.SpiDev()
                setattr(self, attr, spi)
                try:
                    spi.open(hw.spi_bus, device)
                except FileNotFoundError as exc:
                    raise RuntimeError(f"/dev/spidev{hw.spi_bus}.{device} not found: enable SPI with "
                                       "`sudo raspi-config nonint do_spi 0` and reboot.") from exc
                spi.max_speed_hz = hw.spi_speed_hz
                spi.mode = 0b01  # ADS1299: CPOL=0, CPHA=1
                spi.bits_per_word = 8
            self._drdy = open_drdy(hw.drdy_gpio, hw.gpiochip)
            if self.n_channels == 16:
                self._drdy2 = open_drdy(hw.drdy_gpio_2, hw.gpiochip)
            chips = range(1, self.n_channels // N_CHANNELS + 1)
            for chip in chips:
                self._configure(chip)
            for chip in chips:
                self._command(RDATAC, chip)
                self._command(START, chip)
        except Exception:
            try:
                self.stop()
            except Exception:
                logger.debug("Error releasing PiEEG after failed startup", exc_info=True)
            raise
        logger.info("PiEEG-%d streaming at %d SPS, gain %d", self.n_channels, self.fs, hw.gain)

    def _configure(self, chip: int = 1) -> None:
        self._command(WAKEUP, chip)
        self._command(STOP, chip)
        self._command(RESET, chip)
        time.sleep(0.01)
        self._command(SDATAC, chip)  # the chip powers up in continuous-read mode
        device_id = self._read(ID, chip)
        if device_id in (0x00, 0xFF):
            raise RuntimeError(f"No answer from ADS1299 chip {chip} (ID register = 0x{device_id:02X}). Is the PiEEG "
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
            self._write(register, value, chip)
        if self._read(CONFIG1, chip) != config1:
            raise RuntimeError(f"ADS1299 chip {chip} register write did not stick (SPI wiring or speed problem?)")

    def read(self) -> np.ndarray:
        rows = []
        while len(rows) < self.block_size:
            samples = []
            lines = [self._drdy] + ([self._drdy2] if self.n_channels == 16 else [])
            for chip, line in enumerate(lines, start=1):
                edges = line.wait(1.0)
                if edges == 0:
                    field = "drdy_gpio" if chip == 1 else "drdy_gpio_2"
                    raise RuntimeError(f"No data from PiEEG chip {chip} for 1 s (DRDY never went low). "
                                       f"Check the board, the battery and hardware.{field}.")
                self.missed += edges - 1
                with self._selected_spi(chip) as spi:
                    samples.append(self._decode(spi.readbytes(FRAME_BYTES)))
            # Drop the pair if either status header is corrupt; channel order stays 1..16.
            if all(sample is not None for sample in samples):
                rows.append(self._deglitch(np.concatenate(samples)))
        return np.array(rows)

    def _deglitch(self, sample: np.ndarray) -> np.ndarray:
        """Hold the previous sample over a corrupted frame; accept a real step after 3 frames."""
        if self._last is not None and self._held < 2 and np.abs(sample - self._last).max() > MAX_JUMP_UV:
            self._held += 1
            self.glitches += 1
            return self._last
        self._held = 0
        self._last = sample
        return sample

    def _decode(self, frame) -> Optional[np.ndarray]:
        b = np.frombuffer(bytes(frame), dtype=np.uint8)
        if len(b) != FRAME_BYTES or b[0] & 0xF0 != 0xC0:  # every frame starts with 0b1100
            self.bad_frames += 1
            return None
        d = b[3:].reshape(N_CHANNELS, 3).astype(np.int32)
        raw = (d[:, 0] << 16) | (d[:, 1] << 8) | d[:, 2]
        raw -= (raw & 0x800000) << 1  # sign-extend 24-bit two's complement
        return raw * self._uv_per_lsb

    def stop(self) -> None:
        attrs = ("_cs2", "_drdy2", "_drdy", "_spi2", "_spi")
        try:
            with ExitStack() as cleanup:
                for attr in attrs:
                    resource = getattr(self, attr)
                    if resource is not None:
                        cleanup.callback(resource.close)
                for chip, spi in ((1, self._spi), (2, self._spi2)):
                    if spi is not None:
                        self._command(STOP, chip)
                        self._command(SDATAC, chip)
        finally:
            for attr in attrs:
                setattr(self, attr, None)
        if self.missed or self.bad_frames or self.glitches:
            logger.info("Lost %d samples, dropped %d malformed and %d glitched frames "
                        "(many? add core_freq_fixed=1 to /boot/firmware/config.txt or lower spi_speed_hz)",
                        self.missed, self.bad_frames, self.glitches)
