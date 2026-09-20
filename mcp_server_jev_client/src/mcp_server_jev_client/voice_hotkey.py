"""Global press/release hotkey wrapper. Callbacks only hand off events."""

from __future__ import annotations

from typing import Any, Callable

NAMED_KEYS = {
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "esc", "escape", "space", "tab",
}


def parse_key(spec: str):
    from pynput.keyboard import Key, KeyCode

    name = spec.strip().lower()
    if name in {"esc", "escape"}:
        return Key.esc
    if name == "space":
        return Key.space
    if name == "tab":
        return Key.tab
    if name.startswith("f") and name[1:].isdigit():
        return getattr(Key, name)
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise ValueError(f"Unsupported key {spec!r}; use a function key such as f8 or a single character")


def _matches(pressed: Any, expected: Any) -> bool:
    if pressed == expected:
        return True
    char = getattr(pressed, "char", None)
    expected_char = getattr(expected, "char", None)
    if char and expected_char and char.lower() == expected_char.lower():
        return True
    return False


class HotkeyListener:
    def __init__(
        self,
        talk_key: str,
        stop_key: str,
        *,
        on_talk_press: Callable[[], None],
        on_talk_release: Callable[[], None],
        on_stop: Callable[[], None],
        on_escape: Callable[[], None],
    ):
        from pynput.keyboard import Key

        self._talk = parse_key(talk_key)
        self._stop = parse_key(stop_key)
        self._escape = Key.esc
        self._on_talk_press = on_talk_press
        self._on_talk_release = on_talk_release
        self._on_stop = on_stop
        self._on_escape = on_escape
        self._talk_down = False
        self._listener = None

    def start(self) -> None:
        from pynput.keyboard import Listener

        self._listener = Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener.join(timeout=1)
            self._listener = None

    def _on_press(self, key) -> None:
        if _matches(key, self._escape):
            self._on_escape()
            return
        if _matches(key, self._stop):
            self._on_stop()
            return
        if _matches(key, self._talk):
            if self._talk_down:
                return
            self._talk_down = True
            self._on_talk_press()

    def _on_release(self, key) -> None:
        if _matches(key, self._talk):
            if not self._talk_down:
                return
            self._talk_down = False
            self._on_talk_release()
