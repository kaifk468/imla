"""Global push-to-talk hotkey.

Default: Right Option/Alt. Hold to record, release to finalize.

Backends
--------
* ``pynput`` (default everywhere): macOS, Windows, Linux/X11.
* ``evdev`` (Linux/Wayland): Wayland does not let ordinary apps observe
  global key events, so pynput only sees keys typed into XWayland windows.
  When running under Wayland and the optional ``evdev`` package is
  installed (``uv sync --extra wayland``) *and* the user can read
  ``/dev/input/event*`` (member of the ``input`` group), we read the
  keyboard devices directly instead.

Override the choice with ``LIKHARI_HOTKEY_BACKEND=pynput|evdev``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

from pynput import keyboard

log = logging.getLogger("imla.hotkey")


class PushToTalk:
    """Listens for a global hotkey and invokes on_press / on_release callbacks
    when the key transitions to and from the held state."""

    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        key: Optional[Any] = None,
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._key_spec = str(key) if key else "Key.alt_r"
        self._key = self._resolve_key(key or keyboard.Key.alt_r)
        self._held = False
        self._lock = threading.Lock()
        self._listener: Optional[Any] = None

    @property
    def key(self) -> keyboard.Key:
        return self._key

    @property
    def key_spec(self) -> str:
        return self._key_spec

    def set_key(self, key_spec) -> None:
        """Update the hotkey dynamically without restarting."""
        new_key = self._resolve_key(key_spec)
        with self._lock:
            self._key_spec = str(key_spec)
            if self._key == new_key:
                return
            self._key = new_key
            self._held = False
        log.info("Push-to-talk hotkey updated to %s.", self._key)

    @staticmethod
    def _resolve_key(key_spec) -> keyboard.Key:
        if isinstance(key_spec, keyboard.Key):
            return key_spec
        if isinstance(key_spec, str):
            clean = key_spec.strip()
            if clean.startswith("Key."):
                clean = clean[4:]
            aliases = {
                "ctrl_l": "ctrl",
                "alt_l": "alt",
                "cmd_l": "cmd",
                "shift_l": "shift",
            }
            clean = aliases.get(clean, clean)
            try:
                return getattr(keyboard.Key, clean)
            except AttributeError:
                pass
        return keyboard.Key.alt_r

    def start(self) -> None:
        if self._listener is not None:
            return
        if _want_evdev():
            try:
                self._listener = _EvdevListener(
                    on_press=self._handle_press,
                    on_release=self._handle_release,
                )
                self._listener.start()
                log.info(
                    "Push-to-talk listener started via evdev (hotkey=%s).",
                    self._key,
                )
                return
            except Exception as e:  # noqa: BLE001
                self._listener = None
                log.warning(
                    "evdev hotkey backend unavailable (%s). Falling back to "
                    "pynput — under Wayland the hotkey will only register "
                    "while an X11/XWayland window is focused. Install the "
                    "'wayland' extra and add yourself to the 'input' group "
                    "(sudo usermod -aG input $USER, then log out/in).",
                    e,
                )
        self._listener = keyboard.Listener(
            on_press=self._handle_press,
            on_release=self._handle_release,
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("Push-to-talk listener started (hotkey=%s).", self._key)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def wait(self) -> None:
        """Block until the listener thread exits."""
        if self._listener is not None:
            self._listener.join()

    _ALT_KEYS = {
        keyboard.Key.alt,
        keyboard.Key.alt_r,
        keyboard.Key.alt_l,
        keyboard.Key.alt_gr,
    }

    def _matches_key(self, key, for_release: bool = False) -> bool:
        if key == self._key:
            return True
        # On many non-US layouts (German, French, Nordic, …) Windows and
        # Linux/X11 report the physical Right Alt key as AltGr. Treat it as
        # the same physical key for both press and release. (On macOS
        # pynput defines alt_gr as an alias of alt_r, so this is a no-op.)
        if self._key == keyboard.Key.alt_r and key == keyboard.Key.alt_gr:
            return True
        # On macOS and Windows, release events for Alt/Option keys may be
        # dispatched as generic Key.alt or Key.alt_gr instead of Key.alt_r.
        # If the target hotkey is an Alt variant, accept any Alt variant on release.
        if for_release and self._key in self._ALT_KEYS and key in self._ALT_KEYS:
            return True
        return False

    def _handle_press(self, key) -> None:  # noqa: ANN001
        if not self._matches_key(key, for_release=False):
            return
        with self._lock:
            if self._held:
                return
            self._held = True
        try:
            self._on_press()
        except Exception:  # noqa: BLE001
            log.exception("on_press handler failed")

    def _handle_release(self, key) -> None:  # noqa: ANN001
        if not self._matches_key(key, for_release=True):
            return
        with self._lock:
            if not self._held:
                return
            self._held = False
        try:
            self._on_release()
        except Exception:  # noqa: BLE001
            log.exception("on_release handler failed")


# ---------------------------------------------------------------------------
# Linux / Wayland: evdev backend
# ---------------------------------------------------------------------------


def _is_wayland_session() -> bool:
    from ._platform import is_wayland_session

    return is_wayland_session()


def _want_evdev() -> bool:
    override = os.environ.get("LIKHARI_HOTKEY_BACKEND", "").strip().lower()
    if override == "pynput":
        return False
    if override == "evdev":
        return sys.platform.startswith("linux")
    return _is_wayland_session()


# (evdev key name, pynput Key attribute) — the pynput side must match what
# PushToTalk._resolve_key produces for the corresponding config value.
_EVDEV_KEYMAP = [
    ("KEY_RIGHTALT", "alt_r"),
    ("KEY_LEFTALT", "alt"),
    ("KEY_RIGHTCTRL", "ctrl_r"),
    ("KEY_LEFTCTRL", "ctrl"),
    ("KEY_RIGHTMETA", "cmd_r"),
    ("KEY_LEFTMETA", "cmd"),
    ("KEY_RIGHTSHIFT", "shift_r"),
    ("KEY_LEFTSHIFT", "shift"),
] + [(f"KEY_F{i}", f"f{i}") for i in range(1, 21)]


class _EvdevListener:
    """Minimal pynput-Listener-compatible reader over /dev/input keyboards.

    Translates the evdev codes we care about into ``pynput.keyboard.Key``
    members and forwards them to the same press/release handlers."""

    RESCAN_SECONDS = 3.0

    def __init__(
        self,
        on_press: Callable[[Any], None],
        on_release: Callable[[Any], None],
    ) -> None:
        import evdev  # type: ignore[import-not-found]

        self._evdev = evdev
        self._on_press = on_press
        self._on_release = on_release
        self._code_to_key: Dict[int, Any] = {}
        for ev_name, key_name in _EVDEV_KEYMAP:
            code = getattr(evdev.ecodes, ev_name, None)
            key = getattr(keyboard.Key, key_name, None)
            if code is not None and key is not None:
                self._code_to_key[code] = key
        self._devices: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.daemon = True

    def _scan(self) -> None:
        ecodes = self._evdev.ecodes
        for path in self._evdev.list_devices():
            if path in self._devices:
                continue
            try:
                dev = self._evdev.InputDevice(path)
            except OSError:
                continue  # no permission or vanished
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            # Only real keyboards (skip mice, power buttons, …).
            if ecodes.KEY_A in keys and ecodes.KEY_SPACE in keys:
                self._devices[path] = dev
                log.debug("evdev: listening on %s (%s)", path, dev.name)
            else:
                dev.close()

    def start(self) -> None:
        self._scan()
        if not self._devices:
            raise RuntimeError(
                "no readable keyboard under /dev/input (are you in the 'input' group?)"
            )
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="imla-evdev"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        for dev in list(self._devices.values()):
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass
        self._devices.clear()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        import select
        import time

        ecodes = self._evdev.ecodes
        last_scan = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_scan >= self.RESCAN_SECONDS:
                self._scan()  # pick up hot-plugged keyboards
                last_scan = now
            devices: List[Any] = list(self._devices.values())
            if not devices:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(devices, [], [], 0.25)
            except (OSError, ValueError):
                ready = []
                self._drop_dead_devices()
            for dev in ready:
                try:
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY or ev.value not in (0, 1):
                            continue  # ignore autorepeat (value 2)
                        key = self._code_to_key.get(ev.code)
                        if key is None:
                            continue
                        if ev.value == 1:
                            self._on_press(key)
                        else:
                            self._on_release(key)
                except BlockingIOError:
                    continue  # spurious wake-up; no events queued
                except OSError:
                    self._drop(dev)  # device unplugged

    def _drop(self, dev: Any) -> None:
        for path, d in list(self._devices.items()):
            if d is dev:
                self._devices.pop(path, None)
                try:
                    d.close()
                except Exception:  # noqa: BLE001
                    pass

    def _drop_dead_devices(self) -> None:
        for dev in list(self._devices.values()):
            try:
                dev.fileno()
                os.fstat(dev.fileno())
            except (OSError, ValueError):
                self._drop(dev)
