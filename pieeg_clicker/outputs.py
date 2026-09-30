"""Turn logical actions ("next", "previous", "blank") into key presses.

Outputs, chosen with :func:`create_output`:

* ``console`` - dry run, only logs what would be pressed.
* ``udp``     - sends JSON datagrams to ``receiver/pieeg_receiver.py`` on the presenter's laptop.
* ``uinput``  - types on a Linux uinput virtual keyboard (the Pi itself shows the slides).

Each output takes a ``keys`` mapping of action -> key name, by default
``{"next": "page_down", "previous": "page_up", "blank": "b"}``. PgDn/PgUp is what commercial
clickers send; PowerPoint, Keynote, Google Slides, LibreOffice Impress and PDF viewers accept it.
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import string
import time
from collections.abc import Mapping

logger = logging.getLogger(__name__)

__all__ = ["SUPPORTED_KEYS", "normalize_key", "Output", "ConsoleOutput", "UdpOutput",
           "UinputOutput", "create_output"]

_NAMED_KEYS = ("space", "enter", "escape", "tab", "backspace", "left", "right", "up", "down",
               "page_up", "page_down", "home", "end", "period", "comma")
#: Canonical key names (aliases such as "esc" or "pgdn" are accepted by normalize_key only).
SUPPORTED_KEYS: frozenset[str] = frozenset(
    (*string.ascii_lowercase, *string.digits, *_NAMED_KEYS, *(f"f{n}" for n in range(1, 13)))
)
_ALIASES = {"esc": "escape", ".": "period", ",": "comma", "pagedown": "page_down",
            "pgdn": "page_down", "pageup": "page_up", "pgup": "page_up"}
_EVDEV_NAMES = {"escape": "KEY_ESC", "period": "KEY_DOT", "comma": "KEY_COMMA",
                "page_up": "KEY_PAGEUP", "page_down": "KEY_PAGEDOWN"}  # others: KEY_<NAME>

_PROTOCOL_APP = "pieeg-clicker"
_PROTOCOL_VERSION = 1
_KEY_HOLD_S = 0.02  # uinput: time between key down and key up
_SETTLE_S = 0.5     # uinput: let the compositor (labwc) notice the new device
_UINPUT_HELP = (
    "cannot create the uinput virtual keyboard: {error}\n"
    "  1. load the kernel module: sudo modprobe uinput "
    "(at every boot: echo uinput | sudo tee /etc/modules-load.d/uinput.conf)\n"
    "  2. install the udev rule: sudo cp extras/99-pieeg-uinput.rules /etc/udev/rules.d/ "
    "&& sudo udevadm control --reload-rules && sudo udevadm trigger\n"
    "  3. be in the 'input' group: sudo usermod -aG input $USER (then log out and back in)"
)


def normalize_key(name: str) -> str:
    """Return the canonical key name for ``name``; raise ValueError if it is unsupported."""
    key = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    if key not in SUPPORTED_KEYS:
        raise ValueError(
            f"unsupported key {name!r}; valid names: letters a-z, digits 0-9, f1-f12, "
            f"{', '.join(_NAMED_KEYS)} (aliases: esc, '.', ',', pgup, pgdn)"
        )
    return key


class Output:
    """Base class: looks up the key for an action and hands it to :meth:`_send_key`."""

    def __init__(self, keys: Mapping[str, str] | None = None) -> None:
        self.keys: dict[str, str] = {}
        for action, key in (keys or {}).items():
            try:
                self.keys[action] = normalize_key(key)
            except ValueError as exc:
                raise ValueError(f"key for action {action!r}: {exc}") from exc

    def send(self, action: str) -> None:
        """Press the key mapped to ``action``; unknown actions are logged and ignored."""
        key = self.keys.get(action)
        if key is None:
            logger.warning("no key mapped for action %r (known: %s)", action, ", ".join(self.keys))
            return
        self._send_key(action, key)

    def _send_key(self, action: str, key: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release resources; safe to call more than once."""

    def __enter__(self) -> Output:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class ConsoleOutput(Output):
    """Dry run: log the action and key, press nothing."""

    def _send_key(self, action: str, key: str) -> None:
        logger.info("ACTION %s -> key %s", action, key)


class UdpOutput(Output):
    """Send each action as a JSON datagram (broadcast by default) to the laptop receiver.

    A message is transmitted ``repeats`` times back to back as cheap insurance against Wi-Fi
    packet loss; the receiver drops the copies using (id, seq). Socket errors are only logged.
    """

    def __init__(self, keys: Mapping[str, str], host: str = "255.255.255.255",
                 port: int = 5005, token: str = "", repeats: int = 2) -> None:
        super().__init__(keys)
        self.address = (host, int(port))
        self.token = token
        self.repeats = max(1, int(repeats))
        self.sender_id = secrets.token_hex(4)  # new per instance: a restart never reuses old seqs
        self._seq = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def _send_key(self, action: str, key: str) -> None:
        self._seq += 1
        message = {"app": _PROTOCOL_APP, "v": _PROTOCOL_VERSION, "id": self.sender_id,
                   "seq": self._seq, "action": action, "key": key, "token": self.token}
        payload = json.dumps(message).encode("utf-8")
        try:
            for _ in range(self.repeats):
                self._sock.sendto(payload, self.address)
        except OSError as exc:  # e.g. network unreachable while Wi-Fi reconnects
            logger.warning("UDP send to %s:%d failed, %r may be lost: %s",
                           *self.address, action, exc)
        else:
            logger.debug("UDP %s:%d seq=%d %s -> %s", *self.address, self._seq, action, key)

    def close(self) -> None:
        self._sock.close()


class UinputOutput(Output):
    """Press keys on a Linux uinput virtual keyboard (needs python-evdev and /dev/uinput access)."""

    def __init__(self, keys: Mapping[str, str], device_name: str = "PiEEG Clicker") -> None:
        super().__init__(keys)
        try:
            import evdev  # lazy: the module must import on machines without evdev
        except ImportError as exc:
            raise RuntimeError("the uinput output needs python-evdev: "
                               "sudo apt install python3-evdev (or: pip install evdev)") from exc
        ecodes = evdev.ecodes
        self._ev_key = ecodes.EV_KEY
        self._codes = {key: getattr(ecodes, _EVDEV_NAMES.get(key, "KEY_" + key.upper()))
                       for key in set(self.keys.values())}
        # Also declare key codes 1-31 (Esc, digits, Q..S): udev only tags a device with all of
        # them as ID_INPUT_KEYBOARD, which some X11/Wayland setups require before using it.
        capabilities = sorted(set(range(1, 32)) | set(self._codes.values()))
        try:
            # evdev raises UInputError (not an OSError) for a missing or unwritable /dev/uinput.
            self._ui = evdev.UInput(events={self._ev_key: capabilities}, name=device_name)
        except (OSError, evdev.UInputError) as exc:
            raise RuntimeError(_UINPUT_HELP.format(error=exc)) from exc
        time.sleep(_SETTLE_S)
        logger.info("uinput virtual keyboard %r ready (keys: %s)", device_name,
                    ", ".join(sorted(self._codes)))

    def _send_key(self, action: str, key: str) -> None:
        if self._ui is None:
            logger.warning("uinput output is closed, dropping %r", action)
            return
        code = self._codes[key]
        try:
            self._ui.write(self._ev_key, code, 1)
            self._ui.syn()
            time.sleep(_KEY_HOLD_S)
            self._ui.write(self._ev_key, code, 0)
            self._ui.syn()
        except OSError as exc:
            logger.warning("uinput write failed, %r lost: %s", action, exc)

    def close(self) -> None:
        ui, self._ui = self._ui, None
        if ui is not None:
            ui.close()


def create_output(mode: str, *, keys: Mapping[str, str], udp_host: str = "255.255.255.255",
                  udp_port: int = 5005, token: str = "") -> Output:
    """Build the output for ``mode`` ("udp", "uinput" or "console")."""
    if mode == "udp":
        return UdpOutput(keys, host=udp_host, port=udp_port, token=token)
    if mode == "uinput":
        return UinputOutput(keys)
    if mode == "console":
        return ConsoleOutput(keys)
    raise ValueError(f"unknown output mode {mode!r}; expected 'udp', 'uinput' or 'console'")
