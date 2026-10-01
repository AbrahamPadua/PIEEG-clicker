"""SPI/GPIO contract and failure checks; no Raspberry Pi or gpiod required."""
from collections import deque
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import numpy as np

from pieeg_clicker.calibrate import analyse, record
from pieeg_clicker.cli import open_source
from pieeg_clicker.config import Config, HardwareConfig
from pieeg_clicker.detectors import GestureEngine
from pieeg_clicker import pieeg


def frame(values, status=0xC0):
    data = [status, 0, 8]
    for value in values:
        raw = int(value) & 0xFFFFFF
        data.extend([raw >> 16, (raw >> 8) & 0xFF, raw & 0xFF])
    return data


class FakeSelect:
    def __init__(self):
        self.value = 1
        self.closed = False

    def set(self, value):
        self.value = value

    def close(self):
        self.value = 1
        self.closed = True


class FakeSpi:
    def __init__(self, select):
        self.select = select
        self.device = None
        self.closed = False
        self.open_error = None
        self.registers = {pieeg.ID: 0x3E}
        self.frames = deque()
        self.commands = []
        self.reads = 0

    def open(self, bus, device):
        self.bus, self.device = bus, device
        if self.open_error:
            raise self.open_error

    def check_selected(self):
        expected = 0 if self.device == 1 else 1
        if self.select.value != expected:
            raise AssertionError(f"SPI device {self.device} selected with GPIO19={self.select.value}")

    def xfer2(self, data):
        self.check_selected()
        self.commands.append(data)
        if len(data) == 3:
            register = data[0] & 0x1F
            if data[0] & 0xE0 == pieeg.WREG:
                self.registers[register] = data[2]
            elif data[0] & 0xE0 == pieeg.RREG:
                return [0, 0, self.registers.get(register, 0)]
        return [0] * len(data)

    def readbytes(self, length):
        self.check_selected()
        if length != 27:
            raise AssertionError("Each ADS1299 must receive a separate 27-byte read")
        self.reads += 1
        data = self.frames.popleft() if self.frames else frame(range(1 + 8 * self.device, 9 + 8 * self.device))
        if isinstance(data, Exception):
            raise data
        return data

    def close(self):
        self.closed = True


@contextmanager
def hardware(n_channels=16):
    select = FakeSelect()
    spis = [FakeSpi(select), FakeSpi(select)]
    lines = {gpio: Mock(wait=Mock(return_value=1)) for gpio in (26, 13)}
    module = ModuleType("spidev")
    module.SpiDev = Mock(side_effect=spis)
    source = pieeg.PiEEG(HardwareConfig(n_channels=n_channels, block_size=1))
    with patch.dict("sys.modules", {"spidev": module}), \
            patch.object(pieeg, "_ChipSelect", return_value=select) as make_select, \
            patch.object(pieeg, "open_drdy", side_effect=lambda gpio, chip: lines[gpio]) as make_drdy, \
            patch.object(pieeg.time, "sleep"):
        yield SimpleNamespace(source=source, spis=spis, lines=lines, select=select,
                              make_select=make_select, make_drdy=make_drdy, module=module)
        source.stop()


class DriverTests(unittest.TestCase):
    def test_sixteen_channel_initialization_and_signed_channel_order(self):
        with hardware() as hw:
            hw.source.start()
            hw.make_select.assert_called_once_with(19, None)
            self.assertEqual(hw.make_drdy.call_args_list, [call(26, None), call(13, None)])
            for device, spi in enumerate(hw.spis):
                self.assertEqual((spi.bus, spi.device, spi.mode), (0, device, 1))
                self.assertEqual(spi.registers[pieeg.CONFIG1], 0x96)
                self.assertEqual(spi.registers[pieeg.MISC1], 0x20)
                self.assertIn([pieeg.START], spi.commands)
            first = [0, 1, -1, 0x7FFFFF, -0x800000, 20, -20, 80]
            second = [-9, 10, -11, 12, -13, 14, -15, 16]
            hw.spis[0].frames.append(frame(first))
            hw.spis[1].frames.append(frame(second))
            data = hw.source.read()
            self.assertEqual(data.shape, (1, 16))
            np.testing.assert_allclose(data[0], np.array(first + second) * hw.source._uv_per_lsb)
            self.assertEqual(hw.select.value, 1)

    def test_eight_channel_mode_does_not_touch_second_chip(self):
        with hardware(8) as hw:
            hw.source.start()
            self.assertEqual(hw.source.read().shape, (1, 8))
            hw.make_select.assert_not_called()
            hw.make_drdy.assert_called_once_with(26, None)
            self.assertEqual(hw.module.SpiDev.call_count, 1)
            self.assertEqual(hw.spis[1].commands, [])

    def test_corrupt_second_frame_drops_whole_pair(self):
        with hardware() as hw:
            hw.source.start()
            hw.spis[0].frames.extend([frame([100] * 8), frame([200] * 8)])
            hw.spis[1].frames.extend([frame([300] * 8, status=0), frame([400] * 8)])
            hw.lines[13].wait.side_effect = [2, 1]
            data = hw.source.read()
            np.testing.assert_allclose(data[0], np.array([200] * 8 + [400] * 8) * hw.source._uv_per_lsb)
            self.assertEqual((hw.source.bad_frames, hw.source.missed), (1, 1))
            self.assertEqual([spi.reads for spi in hw.spis], [2, 2])

    def test_truncated_frame_is_rejected(self):
        source = pieeg.PiEEG(HardwareConfig())
        self.assertIsNone(source._decode([]))
        self.assertIsNone(source._decode([0xC0] * 26))
        self.assertEqual(source.bad_frames, 2)

    def test_second_chip_timeout_identifies_gpio_setting(self):
        with hardware() as hw:
            hw.source.start()
            hw.lines[13].wait.return_value = 0
            with self.assertRaisesRegex(RuntimeError, "chip 2.*hardware.drdy_gpio_2"):
                hw.source.read()

    def test_second_spi_failure_deselects_chip(self):
        with hardware() as hw:
            hw.source.start()
            hw.spis[1].frames.append(OSError("read failed"))
            with self.assertRaisesRegex(OSError, "read failed"):
                hw.source.read()
            self.assertEqual(hw.select.value, 1)

    def test_second_chip_startup_failure_releases_all_resources(self):
        with hardware() as hw:
            hw.spis[1].registers[pieeg.ID] = 0
            with self.assertRaisesRegex(RuntimeError, "chip 2.*0x00"):
                hw.source.start()
            self.assertTrue(all(spi.closed for spi in hw.spis))
            self.assertTrue(hw.select.closed)
            for line in hw.lines.values():
                line.close.assert_called_once()

    def test_missing_second_spi_preserves_original_error(self):
        with hardware() as hw:
            hw.spis[1].open_error = FileNotFoundError("missing")
            with self.assertRaisesRegex(RuntimeError, "/dev/spidev0.1 not found"):
                hw.source.start()
            self.assertTrue(all(spi.closed for spi in hw.spis))
            self.assertTrue(hw.select.closed)

    def test_shutdown_failure_still_releases_every_resource(self):
        with hardware() as hw:
            hw.source.start()
            with patch.object(hw.source, "_command", side_effect=OSError("stop failed")):
                with self.assertRaisesRegex(OSError, "stop failed"):
                    hw.source.stop()
            self.assertTrue(all(spi.closed for spi in hw.spis))
            self.assertTrue(hw.select.closed)
            for line in hw.lines.values():
                line.close.assert_called_once()


class GpioTests(unittest.TestCase):
    def test_v2_finds_header_line_on_pi5_chip4(self):
        def chip(path):
            instance = Mock()
            instance.__enter__ = Mock(return_value=instance)
            instance.__exit__ = Mock(return_value=False)
            instance.line_offset_from_id.side_effect = ValueError("missing") if path.endswith("0") else None
            instance.line_offset_from_id.return_value = 19
            return instance
        gpiod = SimpleNamespace(Chip=Mock(side_effect=chip))
        with patch.object(pieeg, "_chip_paths", return_value=["/dev/gpiochip0", "/dev/gpiochip4"]):
            self.assertEqual(pieeg._v2_line(gpiod, 19, None), ("/dev/gpiochip4", 19))

    def test_v2_missing_named_line_reports_error(self):
        with patch.object(pieeg, "_chip_paths", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "Cannot find GPIO19"):
                pieeg._v2_line(SimpleNamespace(), 19, None)

    def test_v2_chip_select_defaults_high_and_releases(self):
        class Value(Enum):
            INACTIVE = 0
            ACTIVE = 1
        line_module = ModuleType("gpiod.line")
        line_module.Direction = SimpleNamespace(OUTPUT="output")
        line_module.Value = Value
        request = Mock()
        gpiod = SimpleNamespace(LineSettings=Mock(), request_lines=Mock(return_value=request))
        with patch.dict("sys.modules", {"gpiod.line": line_module}), \
                patch.object(pieeg, "_import_gpiod", return_value=gpiod):
            select = pieeg._ChipSelect(19, "/dev/gpiochip4")
            gpiod.LineSettings.assert_called_once_with(direction="output", output_value=Value.ACTIVE)
            select.set(0)
            select.set(1)
            select.close()
        self.assertEqual(request.set_value.call_args_list,
                         [call(19, Value.INACTIVE), call(19, Value.ACTIVE), call(19, Value.ACTIVE)])
        request.release.assert_called_once()

    def test_v1_chip_select_defaults_high(self):
        line = Mock()
        gpiod = SimpleNamespace(LINE_REQ_DIR_OUT=3, find_line=Mock(return_value=line))
        with patch.object(pieeg, "_import_gpiod", return_value=gpiod):
            select = pieeg._ChipSelect(19, None)
            line.request.assert_called_once_with(consumer="pieeg-clicker-cs", type=3, default_vals=[1])
            select.set(0)
            select.close()
        self.assertEqual(line.set_value.call_args_list, [call(0), call(1)])
        line.release.assert_called_once()

    def test_old_pypi_chip_select_uses_single_default_val(self):
        class Request:
            DIRECTION_OUTPUT = 3
        line = Mock()
        # The old PyPI package accepts default_val, unlike libgpiod 1's default_vals.
        def request(config, default_val=0):
            self.assertEqual(config.request_type, Request.DIRECTION_OUTPUT)
            self.assertEqual(default_val, 1)
        line.request.side_effect = request
        gpiod = SimpleNamespace(line_request=Request, find_line=Mock(return_value=line))
        with patch.object(pieeg, "_import_gpiod", return_value=gpiod):
            select = pieeg._ChipSelect(19, None)
            select.close()
        line.release.assert_called_once()

    def test_old_pypi_drdy_uses_offset_property_and_drains_edges(self):
        class Request:
            EVENT_FALLING_EDGE = 4
        line = Mock(offset=13)
        line.event_wait.side_effect = [True, True, False]
        gpiod = SimpleNamespace(line_request=Request, find_line=Mock(return_value=line))
        drdy = pieeg._DrdyCxx(gpiod, 13, None)
        self.assertEqual(drdy.wait(1.0), 2)
        self.assertEqual(line.event_read.call_count, 2)
        drdy.close()
        line.release.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    def test_channel_validation_matches_selected_board(self):
        cfg = Config()
        cfg.blink.channels = [9, 16]
        with self.assertRaisesRegex(ValueError, "inputs 1..8"):
            cfg.validate()
        cfg.hardware.n_channels = 16
        cfg.validate()
        cfg.blink.channels = [17]
        with self.assertRaisesRegex(ValueError, "inputs 1..16"):
            cfg.validate()

    def test_sixteen_channel_hardware_rejects_conflicting_lines_and_devices(self):
        cfg = Config()
        cfg.hardware.n_channels = 16
        cfg.hardware.cs_gpio_2 = cfg.hardware.drdy_gpio
        with self.assertRaisesRegex(ValueError, "must be different"):
            cfg.validate()
        cfg.hardware.cs_gpio_2 = 19
        cfg.hardware.spi_device_2 = cfg.hardware.spi_device
        with self.assertRaisesRegex(ValueError, "must differ"):
            cfg.validate()

    def test_saved_configuration_keeps_sixteen_channel_selection(self):
        cfg = Config.load("config.pieeg16.example.json")
        test_root = Path(__file__).resolve().parents[1]
        with NamedTemporaryFile(dir=test_root, prefix=".pieeg-test-", suffix=".json", delete=False) as handle:
            path = Path(handle.name)
        try:
            cfg.save(path)
            loaded = Config.load(path)
        finally:
            assert path.parent.resolve() == test_root
            path.unlink()
        self.assertEqual(loaded.hardware.n_channels, 16)
        self.assertEqual(loaded.hardware.drdy_gpio_2, 13)
        self.assertEqual(loaded.hardware.cs_gpio_2, 19)

    def test_sixteen_channel_simulation_and_calibration_use_second_bank(self):
        cfg = Config()
        cfg.hardware.n_channels = 16
        cfg.blink.channels = cfg.clench.channels = [9, 10]
        cfg.blink.threshold_uv = 150
        cfg.validate()
        source = open_source(cfg, simulate=True)
        source.realtime = False
        source.rng = np.random.default_rng(7)
        engine = GestureEngine(cfg, source.fs, source.n_channels)
        actions = []
        for _ in range(30 * source.fs // source.block_size):
            block = source.read()
            self.assertEqual(block.shape, (source.block_size, 16))
            actions.extend(event.action for event in engine.process(block) if event.action)
        self.assertIn("next", actions)
        self.assertIn("previous", actions)
        source = open_source(cfg, simulate=True, demo=False)
        source.realtime = False
        source.rng = np.random.default_rng(3)
        recording, script = record(source, cfg, with_clench=False, say=lambda _: None)
        calibrated, _ = analyse(recording, script, cfg)
        self.assertEqual(calibrated.hardware.n_channels, 16)
        self.assertEqual(calibrated.blink.channels, [9, 10])
        self.assertGreater(calibrated.blink.threshold_uv, 100)


if __name__ == "__main__":
    unittest.main()
