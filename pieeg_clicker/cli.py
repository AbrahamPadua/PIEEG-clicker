"""Command-line interface: run, monitor, calibrate, init-config."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

from . import __version__
from .config import Config, default_config_path
from .detectors import Event, GestureEngine
from .dsp import StreamingFilter, bandpass_sos

logger = logging.getLogger("pieeg_clicker")


def open_source(cfg: Config, simulate: bool, demo: bool = True):
    if simulate:
        from .simulate import SyntheticSource
        return SyntheticSource(fs=cfg.hardware.sample_rate, block_size=cfg.hardware.block_size,
                               n_channels=cfg.hardware.n_channels, demo=demo)
    from .pieeg import PiEEG
    return PiEEG(cfg.hardware)


def _describe_event(ev: Event, cfg: Config) -> str:
    if ev.kind == "blink":
        return f"blink {ev.amplitude:.0f} µV, {ev.duration * 1000:.0f} ms"
    if ev.kind == "clench":
        text = f"jaw clench {ev.amplitude:.0f} µV"
    else:
        text = ev.kind.replace("_", " ")
    if ev.action:
        return f"{text} -> {ev.action.upper()} ({cfg.keys.get(ev.action, '?')})"
    return f"{text} (no action mapped)"


def cmd_run(cfg: Config, args) -> int:
    from .outputs import create_output

    out = cfg.output
    mode = args.output or out.mode
    host = args.host or out.host
    port = args.port or out.port
    token = args.token if args.token is not None else out.token
    output = create_output(mode, keys=cfg.keys, udp_host=host, udp_port=port, token=token)
    source = open_source(cfg, args.simulate)
    engine = GestureEngine(cfg, source.fs, source.n_channels)
    mapped = ", ".join(f"{g} -> {a}" for g, a in cfg.mapping.items() if a) or "nothing!"
    logger.info("Gestures: %s. Output: %s%s", mapped, mode, f" to {host}:{port}" if mode == "udp" else "")
    if cfg.blink.threshold_uv is None:
        logger.warning("Blink threshold not calibrated yet: run `calibrate` for fewer mistakes.")
    with output:
        source.start()
        try:
            while True:
                for ev in engine.process(source.read()):
                    level = logging.INFO if ev.action else logging.DEBUG
                    logger.log(level, _describe_event(ev, cfg))
                    if ev.action:
                        output.send(ev.action)
        except KeyboardInterrupt:
            pass
        finally:
            source.stop()
    return 0


def _bar(value: float, threshold: float, width: int = 20) -> str:
    if not np.isfinite(threshold) or threshold <= 0:
        return "[" + "." * width + "]"
    filled = int(min(1.0, max(0.0, value / (2 * threshold))) * width)
    bar = ["#"] * filled + ["-"] * (width - filled)
    bar[width // 2] = "|"  # the threshold sits in the middle
    return "[" + "".join(bar) + "]"


def cmd_monitor(cfg: Config, args) -> int:
    """Live view of signal quality and detector levels; sends no keys."""
    source = open_source(cfg, args.simulate)
    engine = GestureEngine(cfg, source.fs, source.n_channels)
    quality = StreamingFilter(bandpass_sos(1.0, 40.0, source.fs, order=2))
    power = np.zeros(source.n_channels)
    count = 0
    railed = np.zeros(source.n_channels, dtype=bool)
    next_status, next_quality = 0.0, 2.0
    tty = sys.stdout.isatty()
    print("Ctrl+C to stop. Bars: '|' marks the detection threshold.")
    source.start()
    try:
        while True:
            block = source.read()
            events = engine.process(block)
            y = quality(block)
            power += (y * y).sum(axis=0)
            count += len(y)
            railed |= np.abs(block).max(axis=0) > 0.95 * source.full_scale_uv
            for ev in events:
                print(("\r" if tty else "") + f"{engine.time:8.2f}s  {_describe_event(ev, cfg):60s}")
            if engine.time >= next_quality:
                rms = np.sqrt(power / max(count, 1))
                chans = "  ".join(f"ch{c + 1} {'RAIL' if railed[c] else f'{r:5.1f}'}" for c, r in enumerate(rms))
                print(("\r" if tty else "") + f"signal RMS 1-40 Hz (µV): {chans}")
                power[:], count, railed[:] = 0.0, 0, False
                next_quality += 2.0
            if engine.time >= next_status:
                b, c = engine.blink, engine.clench
                status = (f"blink {b.level:5.0f}/{b.threshold:4.0f} µV {_bar(b.level, b.threshold)}   "
                          f"EMG {c.level:5.1f}/{c.threshold:4.1f} µV {_bar(c.level, c.threshold)}")
                if engine.time < engine.WARMUP_S:
                    status = "warming up..."
                print(("\r" + status) if tty else status, end="" if tty else "\n", flush=True)
                next_status += 0.25 if tty else 2.0
    except KeyboardInterrupt:
        print()
    finally:
        source.stop()
    return 0


def cmd_calibrate(cfg: Config, args) -> int:
    from .calibrate import analyse, record

    print("Calibration takes about 1.5 minutes. Put the electrodes on first and check them with `monitor`.")
    source = open_source(cfg, args.simulate, demo=False)
    rec, script = record(source, cfg, with_clench=not args.skip_clench)
    new, report = analyse(rec, script, cfg)
    print("\n" + "\n".join(report))
    new.save(args.config)
    print(f"Saved to {args.config}")
    return 0


def cmd_init(cfg: Config, args) -> int:
    if args.config.exists() and not args.force:
        logger.error("%s already exists (use --force to overwrite)", args.config)
        return 1
    Config().save(args.config)
    print(f"Wrote default config to {args.config}")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, default=default_config_path(),
                        help="JSON config file (default: %(default)s)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    source = argparse.ArgumentParser(add_help=False)
    source.add_argument("--simulate", action="store_true", help="use synthetic data instead of the PiEEG")

    parser = argparse.ArgumentParser(
        prog="pieeg-clicker",
        description="Hands-free presentation clicker: eye blinks (and optionally jaw clenches) "
                    "measured with a PiEEG turn into slide-change key presses.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", parents=[common, source], help="detect gestures and send slide keys")
    p.add_argument("--output", choices=["udp", "uinput", "console"], help="override output.mode")
    p.add_argument("--host", help="laptop IP running receiver/pieeg_receiver.py (override output.host)")
    p.add_argument("--port", type=int, help="UDP port (override output.port)")
    p.add_argument("--token", help="shared secret, must match the receiver (override output.token)")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("monitor", parents=[common, source], help="live signal check, sends no keys")
    p.set_defaults(func=cmd_monitor)
    p = sub.add_parser("calibrate", parents=[common, source], help="guided threshold calibration")
    p.add_argument("--skip-clench", action="store_true", help="skip the jaw-clench part")
    p.set_defaults(func=cmd_calibrate)
    p = sub.add_parser("init-config", parents=[common], help="write the default config file")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        cfg = Config.load(args.config)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error("Invalid config %s: %s", args.config, exc)
        return 2
    try:
        return args.func(cfg, args)
    except (RuntimeError, OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
