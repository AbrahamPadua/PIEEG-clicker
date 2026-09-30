#!/usr/bin/env python3
"""PiEEG Clicker receiver: turns UDP messages from the Raspberry Pi into key presses.

Run it on the laptop that shows the slides (Windows, macOS or Linux):

    pip install pynput
    python pieeg_receiver.py --token SECRET

then start the clicker on the Pi with --host <the IP address printed at startup>.
Standalone: standard library + pynput (or python-evdev for --backend uinput on Linux).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import string
import sys
import time
from collections import deque

APP, VERSION = "pieeg-clicker", 1
DEFAULT_KEYS = {"next": "page_down", "previous": "page_up", "blank": "b"}
NAMED_KEYS = ("space", "enter", "escape", "tab", "backspace", "left", "right", "up", "down",
              "page_up", "page_down", "home", "end", "period", "comma")
SUPPORTED_KEYS = frozenset(
    (*string.ascii_lowercase, *string.digits, *NAMED_KEYS, *(f"f{n}" for n in range(1, 13)))
)
ALIASES = {"esc": "escape", ".": "period", ",": "comma", "pagedown": "page_down",
           "pgdn": "page_down", "pageup": "page_up", "pgup": "page_up"}
EVDEV_NAMES = {"escape": "KEY_ESC", "period": "KEY_DOT", "comma": "KEY_COMMA",
               "page_up": "KEY_PAGEUP", "page_down": "KEY_PAGEDOWN"}  # others: KEY_<NAME>
KEY_HOLD_S = 0.02


def normalize_key(name: str) -> str:
    """Return the canonical key name for ``name``; raise ValueError if it is unsupported."""
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    key = ALIASES.get(key, key)
    if key not in SUPPORTED_KEYS:
        raise ValueError(
            f"unsupported key {name!r}; valid names: letters a-z, digits 0-9, f1-f12, "
            f"{', '.join(NAMED_KEYS)} (aliases: esc, '.', ',', pgup, pgdn)"
        )
    return key


def is_wayland() -> bool:
    """True on a Linux Wayland session (where pynput cannot reach native Wayland apps)."""
    return sys.platform.startswith("linux") and (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


class PynputBackend:
    """Key presses through pynput: Windows, macOS and Linux/X11."""

    name = "pynput"

    def __init__(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:  # ImportError, or pynput's own failure (e.g. no X display)
            raise RuntimeError(
                f"cannot load pynput: {exc}\nInstall it with: pip install pynput "
                "(on Linux it needs an X server; under Wayland use --backend uinput)"
            ) from exc
        self._keyboard = keyboard
        self._controller = keyboard.Controller()
        if is_wayland():
            print("warning: Wayland session: pynput cannot inject keys into native Wayland "
                  "apps, only XWayland ones. Prefer --backend uinput (needs python-evdev).",
                  file=sys.stderr)

    def tap(self, key: str) -> None:
        kb = self._keyboard
        if key in ("period", "comma"):
            target = kb.KeyCode.from_char("." if key == "period" else ",")
        elif len(key) == 1:  # letters and digits
            target = kb.KeyCode.from_char(key)
        else:  # space, enter, page_down, f5, ... (pynput calls escape "esc")
            target = getattr(kb.Key, "esc" if key == "escape" else key)
        self._controller.press(target)
        time.sleep(KEY_HOLD_S)
        self._controller.release(target)

    def close(self) -> None:
        pass


class UinputBackend:
    """Key presses through a uinput virtual keyboard: Linux only, works under Wayland."""

    name = "uinput"

    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("the uinput backend only works on Linux; use --backend pynput")
        try:
            import evdev
        except ImportError as exc:
            raise RuntimeError("python-evdev is not installed "
                               "(pip install evdev, or sudo apt install python3-evdev)") from exc
        ecodes = evdev.ecodes
        self._ev_key = ecodes.EV_KEY
        self._codes = {key: getattr(ecodes, EVDEV_NAMES.get(key, "KEY_" + key.upper()))
                       for key in SUPPORTED_KEYS}
        # Key codes 1-31 too: udev only tags devices having all of them as ID_INPUT_KEYBOARD.
        capabilities = sorted(set(range(1, 32)) | set(self._codes.values()))
        try:
            self._ui = evdev.UInput(events={self._ev_key: capabilities}, name="PiEEG Receiver")
        except (OSError, evdev.UInputError) as exc:  # UInputError is not an OSError
            raise RuntimeError(
                f"cannot create the uinput device: {exc}\nIt needs write access to /dev/uinput: "
                "run 'sudo modprobe uinput', install extras/99-pieeg-uinput.rules and join the "
                "'input' group (log out and in), or start this script with sudo."
            ) from exc
        time.sleep(0.5)  # let the compositor notice the new device before the first key

    def tap(self, key: str) -> None:
        code = self._codes[key]
        self._ui.write(self._ev_key, code, 1)
        self._ui.syn()
        time.sleep(KEY_HOLD_S)
        self._ui.write(self._ev_key, code, 0)
        self._ui.syn()

    def close(self) -> None:
        self._ui.close()


def create_backend(choice: str) -> PynputBackend | UinputBackend:
    """Build the key backend; raises RuntimeError with a fix-it message if unavailable."""
    if choice == "auto" and is_wayland():
        try:
            return UinputBackend()
        except RuntimeError as exc:
            print(f"warning: uinput backend unavailable, falling back to pynput: {exc}",
                  file=sys.stderr)
    return UinputBackend() if choice == "uinput" else PynputBackend()


class Receiver:
    """Filters and de-duplicates datagrams, then taps the requested key (or only prints)."""

    def __init__(self, backend, token: str = "", overrides: dict[str, str] | None = None) -> None:
        self.backend = backend  # None = dry run
        self.token = token
        self.overrides = overrides or {}
        self._seen: deque = deque(maxlen=256)  # recent (id, seq) pairs
        self._warned: set[str] = set()

    def handle(self, data: bytes, host: str) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError):  # not JSON (UnicodeDecodeError is a ValueError)
            return
        if not isinstance(msg, dict) or msg.get("app") != APP or msg.get("v") != VERSION:
            return
        if self.token and msg.get("token") != self.token:
            if host not in self._warned:
                self._warned.add(host)
                print(f"ignoring messages from {host}: wrong or missing token", flush=True)
            return
        ident = (msg.get("id"), msg.get("seq"))
        if None not in ident:  # the Pi sends each message twice: drop the copy
            if ident in self._seen:
                return
            self._seen.append(ident)
        action = msg.get("action")
        if not isinstance(action, str):
            return
        name = self.overrides.get(action) or msg.get("key") or DEFAULT_KEYS.get(action)
        stamp = time.strftime("%H:%M:%S")
        try:
            if not name:
                raise ValueError(f"no key known for action {action!r}")
            key = normalize_key(name)
            if self.backend is not None:
                self.backend.tap(key)
        except Exception as exc:  # never let one bad message stop the presentation
            print(f"{stamp} {host} {action}: not pressed: {exc}", flush=True)
            return
        note = "  (dry run)" if self.backend is None else ""
        print(f"{stamp} {host:<15} {action:<9} -> {key}{note}", flush=True)


def lan_addresses() -> list[str]:
    """Best guesses for this computer's LAN IPv4 address(es), without sending any packet."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))  # UDP connect only selects a route
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found += [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                  if ip not in found and not ip.startswith("127.")]
    except OSError:
        pass
    return found


def parse_map(text: str) -> tuple[str, str]:
    action, sep, key = text.partition("=")
    if not sep or not action.strip():
        raise argparse.ArgumentTypeError("expected ACTION=KEY, for example next=right")
    try:
        return action.strip().lower(), normalize_key(key)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def print_banner(bind: str, port: int, backend, token: str) -> None:
    """Tell the user where we listen, which IP the Pi needs, and platform gotchas."""
    mode = "dry run, nothing will be pressed" if backend is None else f"{backend.name} backend"
    print(f"PiEEG receiver listening on UDP {bind}:{port} ({mode})")
    hosts = " or ".join(lan_addresses()) or "<this computer's IP> (ipconfig / ifconfig / ip addr)"
    print(f"Start the clicker on the Pi with --host {hosts}")
    windows = " (Windows asks the first time: allow it on private networks)"
    print(f"Firewall: allow inbound UDP port {port}{windows if sys.platform == 'win32' else ''}.")
    if sys.platform == "darwin":
        print("macOS: give your terminal / Python app Accessibility access (System Settings > "
              "Privacy & Security > Accessibility), otherwise key presses are ignored.")
    if not token:
        print("Note: no --token set, so anyone on this network can press keys on this computer.")
    print("Press Ctrl+C to stop.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Turn PiEEG Clicker UDP messages into key presses")
    parser.add_argument("--port", type=int, default=5005, help="UDP port (default: %(default)s)")
    parser.add_argument("--bind", default="0.0.0.0",
                        help="address to bind; keep the default to also receive broadcasts")
    parser.add_argument("--token", default="",
                        help="only accept messages carrying this shared secret")
    parser.add_argument("--backend", choices=("auto", "pynput", "uinput"), default="auto",
                        help="how to press keys; auto picks uinput on Linux/Wayland, else pynput")
    parser.add_argument("--dry-run", action="store_true", help="only print, press nothing")
    parser.add_argument("--map", action="append", default=[], type=parse_map, metavar="ACTION=KEY",
                        help="use KEY for ACTION whatever the Pi sends, e.g. --map next=right "
                             "(repeatable)")
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((args.bind, args.port))
    except OSError as exc:
        print(f"error: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    sock.settimeout(0.5)  # wake up regularly so Ctrl+C also works on Windows

    backend = None
    if not args.dry_run:
        try:
            backend = create_backend(args.backend)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print_banner(args.bind, args.port, backend, args.token)
    receiver = Receiver(backend, args.token, dict(args.map))
    try:
        while True:
            try:
                data, (host, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:  # e.g. connection reset on Windows: keep listening
                print(f"socket error: {exc}", file=sys.stderr)
                time.sleep(0.5)
                continue
            receiver.handle(data, host)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()
        if backend is not None:
            backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
