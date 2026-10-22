"""Laptop receiver checks: which UDP messages turn into key presses."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))
from pieeg_receiver import Receiver  # noqa: E402

HOST = "192.168.1.50"


class Taps:
    def __init__(self):
        self.keys = []

    def tap(self, key):
        self.keys.append(key)


def packet(seq, action, **extra):
    message = {"app": "pieeg-clicker", "v": 1, "id": "3fa91c2e", "seq": seq, "action": action, **extra}
    return json.dumps(message).encode()


def test_message_cannot_choose_the_key():
    taps = Taps()
    receiver = Receiver(taps, overrides={"previous": "left"})
    receiver.handle(packet(1, "next", key="enter"), HOST)
    receiver.handle(packet(2, "previous", key="enter"), HOST)
    receiver.handle(packet(3, "unknown", key="f4"), HOST)
    assert taps.keys == ["page_down", "left"]


def test_wrong_token_duplicates_and_junk_are_ignored():
    taps = Taps()
    receiver = Receiver(taps, token="SOME-SECRET")
    receiver.handle(packet(1, "next"), HOST)                    # no token
    receiver.handle(packet(2, "next", token="guess"), HOST)     # wrong token
    receiver.handle(packet(3, "next", token="SOME-SECRET"), HOST)
    receiver.handle(packet(3, "next", token="SOME-SECRET"), HOST)  # the Pi sends every message twice
    receiver.handle(b"\xff not json", HOST)
    receiver.handle(b'{"app": "something-else", "v": 1, "action": "next"}', HOST)
    assert taps.keys == ["page_down"]
