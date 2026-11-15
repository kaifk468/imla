"""System tray controller.

macOS   — direct ``NSStatusItem`` (pystray's darwin backend races with
          our HUD's ``NSApp`` pump on Apple Silicon; a manual status item
          sidesteps that).
Windows — ``pystray`` on a daemon thread (Windows message loop is
          thread-friendly).
Linux   — ``pystray`` as well (AppIndicator / GTK / X11 backend, whichever
          is available). If no tray can be created the app keeps running
          without one.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("imla.tray")


# ---------------------------------------------------------------------------
# Icon image
# ---------------------------------------------------------------------------


def _make_icon_image(muted: bool = False):
    """Build a 64×64 RGBA icon (a stylized microphone)."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    accent = (120, 120, 130, 255) if muted else (74, 222, 128, 255)
    draw.rounded_rectangle((24, 12, 40, 38), radius=8, fill=accent)
    draw.line((32, 38, 32, 50), fill=accent, width=3)
    draw.line((22, 50, 42, 50), fill=accent, width=3)
    draw.arc((18, 18, 46, 46), start=200, end=340, fill=accent, width=2)
    return img


# ---------------------------------------------------------------------------
# macOS — NSStatusItem (created on main thread)
# ---------------------------------------------------------------------------


if sys.platform == "darwin":

    import objc  # type: ignore[import-not-found]
    from AppKit import (  # type: ignore[import-not-found]
        NSImage,
        NSMenu,
        NSMenuItem,
        NSStatusBar,
        NSStatusItem,
    )
    from Foundation import NSObject  # type: ignore[import-not-found]

    class _MacosTrayDelegate(NSObject):  # type: ignore[misc]
        """Bridges NSMenuItem actions back to Python callbacks."""

        def initWithCallbacks_(self, callbacks):  # noqa: N802
            self = objc.super(_MacosTrayDelegate, self).init()
            if self is None:
                return None
            self._callbacks = callbacks
            return self

        @objc.python_method
        def _safe(self, fn, *args):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("Tray callback failed.")

        def openSettings_(self, _sender):  # noqa: N802
            self._safe(self._callbacks["on_settings"])

        def quitApp_(self, _sender):  # noqa: N802
            self._safe(self._callbacks["on_quit"])

        def toggleMute_(self, sender):  # noqa: N802
            cb = self._callbacks
            new_value = not cb["is_muted"]()
            self._safe(cb["on_toggle_mute"], new_value)
            try:
                sender.setState_(1 if new_value else 0)
            except Exception:  # noqa: BLE001
                pass

    class _MacosTray:
        """macOS NSStatusItem-backed tray. Must be created on the main thread."""

        def __init__(
            self,
            *,
            on_settings: Callable[[], None],
            on_quit: Callable[[], None],
            on_toggle_mute: Callable[[bool], None],
            is_muted: Callable[[], bool],
            status_text: Callable[[], str],
        ) -> None:
            self._status_item: Optional[NSStatusItem] = None
            callbacks = {
                "on_settings": on_settings,
                "on_quit": on_quit,
                "on_toggle_mute": on_toggle_mute,
                "is_muted": is_muted,
                "status_text": status_text,
            }
            self._delegate = _MacosTrayDelegate.alloc().initWithCallbacks_(callbacks)
            self._on_settings = on_settings
            self._on_quit = on_quit
            self._on_toggle_mute = on_toggle_mute
            self._is_muted = is_muted
            self._status_text = status_text
            self._build()

        def _build(self) -> None:
            bar = NSStatusBar.systemStatusBar()
            self._status_item = bar.statusItemWithLength_(-1.0)
            try:
                btn = self._status_item.button()
                if btn is not None:
                    btn.setTitle_("🎙")
            except Exception:  # noqa: BLE001
                pass

            menu = NSMenu.alloc().init()
            # Status (informational, disabled)
            status_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                self._status_text(), None, ""
            )
            status_item.setEnabled_(False)
            menu.addItem_(status_item)

            menu.addItem_(NSMenuItem.separatorItem())

            # Mute toggle
            mute_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quick Mute", "toggleMute:", ""
            )
            mute_item.setTarget_(self._delegate)
            mute_item.setState_(1 if self._is_muted() else 0)
            menu.addItem_(mute_item)

            # Settings
            settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Settings…", "openSettings:", ""
            )
            settings_item.setTarget_(self._delegate)
            menu.addItem_(settings_item)

            menu.addItem_(NSMenuItem.separatorItem())

            # Quit
            quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit", "quitApp:", ""
            )
            quit_item.setTarget_(self._delegate)
            menu.addItem_(quit_item)

            self._status_item.setMenu_(menu)
            self._menu = menu
            self._status_menu_item = status_item

        def update(self) -> None:
            if self._status_item is None:
                return
            self._status_menu_item.setTitle_(self._status_text())
            try:
                self._status_item.button().setTitle_(
                    "🔇" if self._is_muted() else "🎙"
                )
            except Exception:  # noqa: BLE001
                pass

        def stop(self) -> None:
            if self._status_item is not None:
                try:
                    NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
                except Exception:  # noqa: BLE001
                    pass
                self._status_item = None


# ---------------------------------------------------------------------------
# Windows + Linux — pystray on a daemon thread
# ---------------------------------------------------------------------------


if sys.platform != "darwin":

    class _PystrayTray:
        def __init__(
            self,
            *,
            on_settings: Callable[[], None],
            on_quit: Callable[[], None],
            on_toggle_mute: Callable[[bool], None],
            is_muted: Callable[[], bool],
            status_text: Callable[[], str],
        ) -> None:
            # Imported lazily: on Linux pystray picks a backend (AppIndicator,
            # GTK, or plain X11) at import time and may raise if none works.
            import pystray

            self._pystray = pystray
            self._on_settings = on_settings
            self._on_quit = on_quit
            self._on_toggle_mute = on_toggle_mute
            self._is_muted = is_muted
            self._status_text = status_text
            self._icon = None
            self._thread: Optional[threading.Thread] = None
            self.start()

        def _build_menu(self):
            Menu = self._pystray.Menu
            MenuItem = self._pystray.MenuItem
            return Menu(
                MenuItem(
                    lambda item: self._status_text(),
                    None,
                    enabled=False,
                ),
                MenuItem(
                    "Quick Mute",
                    self._toggle_mute_clicked,
                    checked=lambda item: self._is_muted(),
                ),
                # default=True: backends without menu support (pystray's
                # plain X11 backend on Linux) open Settings on icon click.
                MenuItem("Settings…", self._settings_clicked, default=True),
                Menu.SEPARATOR,
                MenuItem("Quit", self._quit_clicked),
            )

        def start(self) -> None:
            if self._icon is not None:
                return
            menu = self._build_menu()
            self._icon = self._pystray.Icon(
                "imla",
                _make_icon_image(muted=self._is_muted()),
                "Imla",
                menu,
            )
            self._thread = threading.Thread(
                target=self._icon.run, daemon=True, name="imla-tray"
            )
            self._thread.start()
            log.info("Tray controller started.")

        def stop(self) -> None:
            if self._icon is not None:
                try:
                    self._icon.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._icon = None

        def update(self) -> None:
            if self._icon is not None:
                try:
                    self._icon.icon = _make_icon_image(muted=self._is_muted())
                    self._icon.update_menu()
                except Exception:  # noqa: BLE001
                    pass

        def notify(self, title: str, message: str) -> None:
            """Balloon/toast from the tray icon. The icon needs a moment to
            become visible after start(), so retry briefly in a thread."""

            def _go() -> None:
                for _ in range(20):
                    icon = self._icon
                    if icon is not None and getattr(icon, "visible", False):
                        try:
                            icon.notify(message, title)
                        except Exception:  # noqa: BLE001
                            log.debug("pystray notify failed.", exc_info=True)
                        return
                    time.sleep(0.25)

            threading.Thread(target=_go, daemon=True, name="imla-notify").start()

        def _toggle_mute_clicked(self, icon, item) -> None:
            try:
                self._on_toggle_mute(not self._is_muted())
            except Exception:  # noqa: BLE001
                log.exception("Mute toggle failed.")

        def _settings_clicked(self, icon, item) -> None:
            try:
                self._on_settings()
            except Exception:  # noqa: BLE001
                log.exception("Settings callback failed.")

        def _quit_clicked(self, icon, item) -> None:
            try:
                self._on_quit()
            except Exception:  # noqa: BLE001
                log.exception("Quit callback failed.")

    # Backwards-compatible name.
    _WindowsTray = _PystrayTray


class _NullTray:
    """Used when no system tray is available (e.g. GNOME without the
    AppIndicator extension, or a headless session). Dictation still works;
    quit with Ctrl+C and open settings with ``imla --settings``."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def update(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Public façade
# ---------------------------------------------------------------------------


class TrayController:
    """Platform-appropriate tray. Constructed on the calling thread; on
    macOS the underlying ``NSStatusItem`` is created synchronously here."""

    def __init__(
        self,
        *,
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle_mute: Callable[[bool], None],
        is_muted: Callable[[], bool],
        status_text: Callable[[], str] = lambda: "Ready",
    ) -> None:
        if sys.platform == "darwin":
            self._impl: object = _MacosTray(
                on_settings=on_settings,
                on_quit=on_quit,
                on_toggle_mute=on_toggle_mute,
                is_muted=is_muted,
                status_text=status_text,
            )
        else:
            try:
                self._impl = _PystrayTray(
                    on_settings=on_settings,
                    on_quit=on_quit,
                    on_toggle_mute=on_toggle_mute,
                    is_muted=is_muted,
                    status_text=status_text,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "System tray unavailable on this desktop; continuing "
                    "without a tray icon. Use Ctrl+C to quit and "
                    "`imla --settings` to open Settings.",
                    exc_info=True,
                )
                self._impl = _NullTray()
                return
        log.info("Tray controller started.")

    def start(self) -> None:
        # No-op for macOS (already constructed); Windows impl self-starts.
        if hasattr(self._impl, "start"):
            self._impl.start()

    def stop(self) -> None:
        self._impl.stop()

    def update(self) -> None:
        self._impl.update()

    def notify(self, title: str, message: str) -> None:
        """Show a short desktop notification (toast/balloon). Best effort."""
        fn = getattr(self._impl, "notify", None)
        if fn is None:
            return
        try:
            fn(title, message)
        except Exception:  # noqa: BLE001
            log.debug("Tray notification failed.", exc_info=True)
