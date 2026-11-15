"""Cross-platform floating HUD overlay for Imla.

The HUD is a compact, borderless, translucent pill that floats above the
Dock / Taskbar and provides immediate visual feedback while the user is
holding the push-to-talk key.

States
------
* ``idle``        — hidden (alpha 0), collapsed geometry.
* ``recording``   — visible. With preview active: expanded (420×96) so the
                    bottom half can show the live provisional transcript.
                    Without preview (Vosk unavailable): collapsed (140×44)
                    with just the audio-reactive bars.
* ``processing``  — expanded, bottom half shows
                    "Transcribing with Whisper..." while Whisper works.

Crucially, the overlay NEVER takes keyboard focus. The text cursor keeps
blinking uninterrupted. See ``_NonActivatingWindow`` (macOS), the
``WS_EX_NOACTIVATE`` ctypes call (Windows), and the override-redirect
window (Linux/X11, which the window manager never focuses) below.
"""

from __future__ import annotations

import logging
import math
import queue
import sys
import threading
import time
from typing import Callable, List, Optional, Protocol

log = logging.getLogger("imla.hud")


class _HUDImpl(Protocol):
    def show_recording(self, preview_active: bool) -> None: ...
    def show_processing(self) -> None: ...
    def hide(self) -> None: ...
    def set_preview_text(self, text: str) -> None: ...
    def set_status_text(self, text: str) -> None: ...
    def run(self) -> None: ...
    def close(self) -> None: ...


class HUD:
    """Compact floating pill HUD with three states and an optional expanded
    preview layout."""

    # Collapsed (small pill — no preview)
    WIDTH_COLLAPSED = 140
    HEIGHT_COLLAPSED = 44
    # Expanded (preview text in bottom half)
    WIDTH_EXPANDED = 560
    HEIGHT_EXPANDED = 120

    FADE_SECONDS = 0.15       # alpha 1 → 0 transition time
    SIZE_SECONDS = 0.22       # size interpolation time
    BOTTOM_MARGIN = 80        # px above screen bottom

    # Threshold above which we render the expanded layout. Should be >=
    # HEIGHT_COLLAPSED + a margin so transient intermediate sizes don't
    # accidentally switch layout mid-animation.
    EXPANDED_LAYOUT_THRESHOLD = 70

    def __init__(
        self,
        rms_provider: Optional[Callable[[int], List[float]]] = None,
    ) -> None:
        self._rms_provider = rms_provider
        if sys.platform == "darwin":
            self._impl: _HUDImpl = _MacOSHUD(
                rms_provider,
                w_collapsed=self.WIDTH_COLLAPSED,
                h_collapsed=self.HEIGHT_COLLAPSED,
                w_expanded=self.WIDTH_EXPANDED,
                h_expanded=self.HEIGHT_EXPANDED,
                bottom_margin=self.BOTTOM_MARGIN,
                fade_seconds=self.FADE_SECONDS,
                size_seconds=self.SIZE_SECONDS,
            )
        else:
            # Windows and Linux/BSD share the Tkinter implementation; the
            # platform-specific bits (focus suppression, monitor work area,
            # hiding without a compositor) are handled inside it.
            self._impl = _TkHUD(
                rms_provider,
                w_collapsed=self.WIDTH_COLLAPSED,
                h_collapsed=self.HEIGHT_COLLAPSED,
                w_expanded=self.WIDTH_EXPANDED,
                h_expanded=self.HEIGHT_EXPANDED,
                bottom_margin=self.BOTTOM_MARGIN,
                fade_seconds=self.FADE_SECONDS,
                size_seconds=self.SIZE_SECONDS,
            )

    def show_recording(self, preview_active: bool = True) -> None:
        self._impl.show_recording(preview_active)

    def show_processing(self) -> None:
        self._impl.show_processing()

    def set_preview_text(self, text: str) -> None:
        self._impl.set_preview_text(text)

    def set_status_text(self, text: str) -> None:
        self._impl.set_status_text(text)

    def hide(self) -> None:
        self._impl.hide()

    def run(self) -> None:
        """Block until shutdown. On macOS this pumps AppKit events on the
        main thread; on Windows/Linux it waits on the daemon thread that
        owns Tk."""
        self._impl.run()

    def close(self) -> None:
        self._impl.close()


# ---------------------------------------------------------------------------
# macOS implementation (PyObjC)
# ---------------------------------------------------------------------------

if sys.platform == "darwin":

    import objc  # type: ignore[import-not-found]
    import Quartz  # type: ignore[import-not-found]
    from AppKit import (  # type: ignore[import-not-found]
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSAttributedString,
        NSBezierPath,
        NSColor,
        NSEvent,
        NSFont,
        NSFontWeightMedium,
        NSScreen,
        NSStatusWindowLevel,
        NSString,
        NSView,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorTransient,
        NSWorkspace,
    )
    from Foundation import (  # type: ignore[import-not-found]
        NSDate,
        NSDefaultRunLoopMode,
        NSObject,
    )

    _ANY_EVENT_MASK = 0xFFFFFFFFFFFFFFFF
    _TEXT_FONT_SIZE = 13.0
    _TEXT_PADDING_X = 20.0
    _SLATE_WHITE = (0.92, 0.94, 0.96, 0.68)   # rgba: provisional text at reduced opacity
    _STATUS_COLOR = (0.92, 0.94, 0.96, 0.88)  # rgba: Whisper status text

    _ATTR_FONT = "NSFont"
    _ATTR_FOREGROUND = "NSColor"

    def _find_active_screen() -> NSScreen:
        """Find the screen where the user is actively working.

        Checks:
        1. Screen containing the center of the frontmost application's main window.
        2. Screen containing the mouse pointer.
        3. Fallback to mainScreen() or first screen.
        """
        screens = NSScreen.screens()
        if not screens:
            main = NSScreen.mainScreen()
            return main if main is not None else None
        if len(screens) == 1:
            return screens[0]

        # 1. Screen containing frontmost active window
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                pid = app.processIdentifier()
                wl = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                    Quartz.kCGNullWindowID,
                )
                primary_h = screens[0].frame().size.height
                for w in wl or []:
                    if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) == 0:
                        bounds = w.get("kCGWindowBounds")
                        if bounds and bounds.get("Width", 0) > 50 and bounds.get("Height", 0) > 50:
                            cx = bounds["X"] + bounds["Width"] / 2.0
                            cy = primary_h - (bounds["Y"] + bounds["Height"] / 2.0)
                            for s in screens:
                                f = s.frame()
                                if (f.origin.x <= cx < f.origin.x + f.size.width and
                                    f.origin.y <= cy < f.origin.y + f.size.height):
                                    return s
        except Exception:
            log.debug("Failed to detect screen from active window", exc_info=True)

        # 2. Screen containing mouse cursor
        try:
            mouse = NSEvent.mouseLocation()
            for s in screens:
                f = s.frame()
                if (f.origin.x <= mouse.x < f.origin.x + f.size.width and
                    f.origin.y <= mouse.y < f.origin.y + f.size.height):
                    return s
        except Exception:
            log.debug("Failed to detect screen from mouse location", exc_info=True)

        main = NSScreen.mainScreen()
        return main if main is not None else screens[0]

    def _fit_text_ns(text: str, max_w: float, font) -> str:
        """Return ``text`` (or its left-truncated form with a leading '… ')
        that fits within ``max_w`` when rendered with ``font``.

        Truncation snaps to word boundaries so the newest spoken words are
        always visible."""
        text = text.strip()
        if not text:
            return ""
        attrs = {_ATTR_FONT: font}
        if NSString.stringWithString_(text).sizeWithAttributes_(attrs).width <= max_w:
            return text

        words = text.split()
        if not words:
            return ""

        for i in range(1, len(words)):
            cand = "… " + " ".join(words[i:])
            if NSString.stringWithString_(cand).sizeWithAttributes_(attrs).width <= max_w:
                return cand

        # Char-level fallback for a single very long word
        last_word = words[-1]
        for i in range(len(last_word) - 1, 0, -1):
            cand = "… " + last_word[-i:]
            if NSString.stringWithString_(cand).sizeWithAttributes_(attrs).width <= max_w:
                return cand
        return "…"

    class _NonActivatingWindow(NSWindow):
        """An NSWindow that refuses to ever become key or main."""

        def canBecomeKeyWindow(self) -> bool:  # noqa: N802
            return False

        def canBecomeMainWindow(self) -> bool:  # noqa: N802
            return False

        def acceptFirstResponder(self) -> bool:  # noqa: N802
            return False

    class _HUDView(NSView):
        """Draws the pill background plus state-specific indicators."""

        def initWithFrame_(self, frame):  # noqa: N802
            self = objc.super(_HUDView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._state = "idle"
            self._rms: List[float] = [0.0] * 5
            self._pulse_t = 0.0
            self._raw_preview_text = ""
            self._raw_status_text = ""
            self._fitted_preview_text = ""
            self._fitted_status_text = ""
            self._text_dirty = False
            self._text_w = 0.0
            return self

        def isFlipped(self) -> bool:  # noqa: N802
            return True

        # Setters called from the animation tick / main pump thread.
        def setState_(self, state):  # noqa: N802
            if self._state != state:
                self._state = state
                self._text_dirty = True
                self.setNeedsDisplay_(True)

        def updateRms_(self, rms):  # noqa: N802
            self._rms = list(rms)

        def tickPulse_(self, dt):  # noqa: N802
            self._pulse_t += float(dt)

        def setRawPreviewText_(self, text):  # noqa: N802
            if self._raw_preview_text != text:
                self._raw_preview_text = text
                self._text_dirty = True

        def setRawStatusText_(self, text):  # noqa: N802
            if self._raw_status_text != text:
                self._raw_status_text = text
                self._text_dirty = True

        def setTextWidth_(self, w):  # noqa: N802
            self._text_w = float(w)

        def drawRect_(self, dirtyRect):  # noqa: N802
            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height

            # Refit text if marked dirty or width changed
            if self._text_dirty or abs(self._text_w - w) > 1.0:
                self._refit_text(w)
                self._text_dirty = False

            # Pill background — slate-900 at ~88% alpha with subtle highlight stroke
            radius = 22.0
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                ((0.0, 0.0), (w, h)), radius, radius
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.055, 0.071, 0.094, 0.88
            ).set()
            path.fill()
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).set()
            path.setLineWidth_(1.0)
            path.stroke()

            if h >= 70.0:
                self._drawExpanded(w, h)
            else:
                self._drawCollapsed(w, h)

        @objc.python_method
        def _refit_text(self, w: float):
            self._text_w = w
            font = NSFont.systemFontOfSize_weight_(_TEXT_FONT_SIZE, NSFontWeightMedium)
            max_w = max(0.0, w - 2 * _TEXT_PADDING_X)
            self._fitted_preview_text = _fit_text_ns(
                self._raw_preview_text, max_w, font
            )
            self._fitted_status_text = _fit_text_ns(
                self._raw_status_text, max_w, font
            )

        @objc.python_method
        def _drawCollapsed(self, w: float, h: float) -> None:
            if self._state == "recording":
                self._drawBars(w, h)
            elif self._state == "processing":
                self._drawBreathing(w, h)

        @objc.python_method
        def _drawExpanded(self, w: float, h: float) -> None:
            top_h = h / 2.0
            # Top half: animated ink stroke waveform / breathing
            if self._state == "recording":
                self._drawInkWaveform(w, top_h)
            elif self._state == "processing":
                self._drawBreathing(w, top_h)

            # Subtle divider
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.07).set()
            NSBezierPath.bezierPathWithRect_(
                ((24.0, top_h - 0.5), (w - 48.0, 1.0))
            ).fill()

            # Bottom half: streaming provisional text or status
            is_recording = (self._state == "recording")
            text = self._fitted_preview_text if is_recording else self._fitted_status_text
            if text:
                font = NSFont.systemFontOfSize_weight_(_TEXT_FONT_SIZE, NSFontWeightMedium)
                color = _SLATE_WHITE if is_recording else _STATUS_COLOR
                attrs = {
                    _ATTR_FONT: font,
                    _ATTR_FOREGROUND: NSColor.colorWithCalibratedRed_green_blue_alpha_(*color),
                }
                text_y = top_h + (top_h - 16.0) / 2.0
                NSAttributedString.alloc().initWithString_attributes_(
                    text, attrs
                ).drawAtPoint_((_TEXT_PADDING_X, text_y))

        @objc.python_method
        def _drawInkWaveform(self, w: float, top_h: float) -> None:
            """Render a fluid, audio-reactive ink stroke waveform."""
            mid_y = top_h / 2.0
            max_rms = max(self._rms) if self._rms else 0.0

            x_start = 36.0
            x_end = w - 36.0
            span_w = x_end - x_start
            num_pts = 16

            points = []
            for i in range(num_pts + 1):
                t = i / float(num_pts)
                x = x_start + t * span_w
                env = math.sin(t * math.pi)  # Taper wave at ends
                h1 = math.sin(self._pulse_t * 3.6 + t * 4.0 * math.pi) * (2.5 + 14.0 * max_rms)
                h2 = math.cos(self._pulse_t * 2.2 + t * 2.5 * math.pi) * (1.5 + 7.0 * max_rms)
                y = mid_y + env * (h1 + h2)
                points.append((x, y))

            # Draw smooth Catmull-Rom spline
            wave_path = NSBezierPath.bezierPath()
            wave_path.moveToPoint_(points[0])
            for i in range(len(points) - 1):
                p0 = points[max(0, i - 1)]
                p1 = points[i]
                p2 = points[i + 1]
                p3 = points[min(len(points) - 1, i + 2)]
                cp1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
                cp2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
                wave_path.curveToPoint_controlPoint1_controlPoint2_(p2, cp1, cp2)

            # Glow layer
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.35, 0.85, 0.98, 0.25).set()
            wave_path.setLineWidth_(3.5)
            wave_path.stroke()

            # Crisp ink stroke
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.95, 1.00, 0.95).set()
            wave_path.setLineWidth_(2.0)
            wave_path.setLineCapStyle_(1)  # NSRoundLineCapStyle
            wave_path.setLineJoinStyle_(1)  # NSRoundLineJoinStyle
            wave_path.stroke()

        @objc.python_method
        def _drawBars(self, w: float, h: float) -> None:
            n = 5
            gap = 5.0
            bar_w = 3.0
            total = n * bar_w + (n - 1) * gap
            x0 = (w - total) / 2.0
            mid = h / 2.0
            max_h = h * 0.55
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.45, 0.94, 0.98, 1.0
            ).set()
            for i, lvl in enumerate(self._rms):
                lh = max(3.0, min(max_h, lvl * max_h * 2.2))
                x = x0 + i * (bar_w + gap)
                y = mid - lh / 2.0
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((x, y), (bar_w, lh)), bar_w / 2.0, bar_w / 2.0
                ).fill()

        @objc.python_method
        def _drawBreathing(self, w: float, h: float) -> None:
            n = 3
            d = 6.0
            gap = 7.0
            total = n * d + (n - 1) * gap
            x0 = (w - total) / 2.0
            mid_y = h / 2.0
            for i in range(n):
                phase = self._pulse_t * 2.8 - i * 0.7
                s = 0.5 + 0.5 * math.sin(phase)
                r = d / 2.0 + s * 2.0
                x = x0 + i * (d + gap) + d / 2.0 - r
                y = mid_y - r
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    0.45, 0.92, 0.98, 0.40 + 0.55 * s
                ).set()
                NSBezierPath.bezierPathWithOvalInRect_(
                    ((x, y), (2 * r, 2 * r))
                ).fill()

    class _Animator(NSObject):
        """NSTimer target — runs on the pump thread (== main thread)."""

        def initWithHud_(self, hud):  # noqa: N802
            self = objc.super(_Animator, self).init()
            if self is None:
                return None
            self._hud = hud
            return self

        def animationTick_(self, _timer):  # noqa: N802
            self._hud._tick_animation()

    class _MacOSHUD:
        def __init__(
            self,
            rms_provider: Optional[Callable[[int], List[float]]],
            w_collapsed: int,
            h_collapsed: int,
            w_expanded: int,
            h_expanded: int,
            bottom_margin: int,
            fade_seconds: float,
            size_seconds: float,
        ) -> None:
            self._rms_provider = rms_provider
            self._w_collapsed = w_collapsed
            self._h_collapsed = h_collapsed
            self._w_expanded = w_expanded
            self._h_expanded = h_expanded
            self._bottom_margin = bottom_margin
            self._fade_seconds = fade_seconds
            self._size_seconds = size_seconds
            self._state = "idle"
            self._target_alpha = 0.0
            self._current_alpha = 0.0
            self._shown = False
            self._stop = threading.Event()
            self._last_tick = time.monotonic()

            # Screen bounds state
            self._screen_origin_x: float = 0.0
            self._screen_origin_y: float = 0.0
            self._screen_w: float = 1920.0
            self._screen_h: float = 1080.0
            self._screen_dirty = False
            self._size_dirty = False

            # Size animation state
            self._target_w = w_collapsed
            self._target_h = h_collapsed
            self._current_w = float(w_collapsed)
            self._current_h = float(h_collapsed)
            self._current_x = 0.0
            self._target_x = 0.0
            self._size_anim_t = 0.0
            self._size_anim_dur = 0.0
            self._size_anim_w0 = float(w_collapsed)
            self._size_anim_h0 = float(h_collapsed)
            self._size_anim_x0 = 0.0

            self._window: Optional[_NonActivatingWindow] = None
            self._view: Optional[_HUDView] = None
            self._animator: Optional[_Animator] = None
            self._timer = None
            self._app: Optional[NSApplication] = None
            self._init_window()

        # -- Screen detection & window construction ----------------------

        def _update_screen(self) -> None:
            screen = _find_active_screen()
            if screen is None:
                return
            vframe = screen.visibleFrame()
            self._screen_origin_x = float(vframe.origin.x)
            self._screen_origin_y = float(vframe.origin.y)
            self._screen_w = float(vframe.size.width)
            self._screen_h = float(vframe.size.height)

        def _init_window(self) -> None:
            self._app = NSApplication.sharedApplication()
            self._app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            self._app.finishLaunching()

            self._update_screen()
            w = self._w_collapsed
            h = self._h_collapsed
            x = self._screen_origin_x + self._screen_w - w - 20.0
            y = self._screen_origin_y + float(self._bottom_margin)
            rect = ((x, y), (w, h))
            self._current_x = x
            self._target_x = x

            self._window = _NonActivatingWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, 0, 2, False
            )
            self._window.setLevel_(NSStatusWindowLevel)
            self._window.setOpaque_(False)
            self._window.setBackgroundColor_(NSColor.clearColor())
            self._window.setHasShadow_(True)
            self._window.setIgnoresMouseEvents_(True)
            self._window.setHidesOnDeactivate_(False)
            self._window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorTransient
                | NSWindowCollectionBehaviorStationary
            )
            self._window.setMovableByWindowBackground_(False)

            self._view = _HUDView.alloc().initWithFrame_(((0.0, 0.0), (w, h)))
            self._window.setContentView_(self._view)
            self._window.setAlphaValue_(0.0)

        # -- Public state API (thread-safe; only mutate Python attrs) --

        def show_recording(self, preview_active: bool) -> None:
            self._state = "recording"
            self._target_alpha = 1.0
            self._target_w = self._w_expanded if preview_active else self._w_collapsed
            self._target_h = self._h_expanded if preview_active else self._h_collapsed
            self._screen_dirty = True

            if self._view is not None:
                self._view.setRawPreviewText_("")
                self._view.setRawStatusText_("")

        def show_processing(self) -> None:
            self._state = "processing"
            self._target_alpha = 1.0
            self._target_w = self._w_expanded
            self._target_h = self._h_expanded
            self._size_dirty = True
            if self._view is not None:
                self._view.setRawPreviewText_("")
                self._view.setRawStatusText_("Transcribing with Whisper...")

        def set_preview_text(self, text: str) -> None:
            if self._view is not None:
                self._view.setRawPreviewText_(text)

        def set_status_text(self, text: str) -> None:
            if self._view is not None:
                self._view.setRawStatusText_(text)

        def hide(self) -> None:
            self._state = "idle"
            self._target_alpha = 0.0
            self._target_w = self._w_collapsed
            self._target_h = self._h_collapsed
            self._size_dirty = True
            if self._view is not None:
                self._view.setRawPreviewText_("")
                self._view.setRawStatusText_("")

        def close(self) -> None:
            self._stop.set()

        # -- Size animation ---------------------------------------------

        def _set_size_target(self, w: int, h: int) -> None:
            if w == self._target_w and h == self._target_h:
                return
            if getattr(self, "_manual_pos", False):
                new_x = self._current_x
            else:
                new_x = self._screen_origin_x + self._screen_w - w - 20.0
                self._target_x = new_x
            self._size_anim_w0 = self._current_w
            self._size_anim_h0 = self._current_h
            self._size_anim_x0 = self._current_x
            self._target_w = w
            self._target_h = h
            self._target_x = new_x
            self._size_anim_t = 0.0
            self._size_anim_dur = self._size_seconds

        # -- Animation pump (pump thread = main thread) -----------------

        def _tick_animation(self) -> None:
            now = time.monotonic()
            dt = max(0.0, now - self._last_tick)
            self._last_tick = now

            if self._window is None or self._view is None:
                return

            # Keep view state in sync
            self._view.setState_(self._state)

            # Handle screen changes or size target changes on main thread
            if self._screen_dirty:
                self._update_screen()
                target_w = self._target_w
                target_h = self._target_h
                new_x = self._screen_origin_x + 20.0
                if self._current_alpha <= 0.01:
                    self._current_x = new_x
                    self._target_x = new_x
                    self._current_w = float(target_w)
                    self._current_h = float(target_h)
                    self._size_anim_dur = 0.0
                    self._apply_geometry()
                else:
                    self._set_size_target(target_w, target_h)
                self._screen_dirty = False
            elif self._size_dirty:
                self._set_size_target(self._target_w, self._target_h)
                self._size_dirty = False

            # Lazy orderFront on pump thread
            if not self._shown and self._target_alpha > 0.0:
                self._window.orderFrontRegardless()
                self._shown = True

            # Alpha tween
            if abs(self._target_alpha - self._current_alpha) > 0.001:
                step = dt / max(self._fade_seconds, 1e-3)
                if self._target_alpha > self._current_alpha:
                    self._current_alpha = min(
                        self._target_alpha, self._current_alpha + step
                    )
                else:
                    self._current_alpha = max(
                        self._target_alpha, self._current_alpha - step
                    )
                self._window.setAlphaValue_(self._current_alpha)

            # When fully faded out in idle state, orderOut
            if self._current_alpha <= 0.001 and self._target_alpha == 0.0:
                if self._shown:
                    self._window.orderOut_(None)
                    self._shown = False

            # Size tween
            size_changed = (
                abs(self._target_w - self._current_w) > 0.5
                or abs(self._target_h - self._current_h) > 0.5
            )
            if size_changed and self._size_anim_dur > 0.0:
                self._size_anim_t += dt
                progress = min(1.0, self._size_anim_t / self._size_anim_dur)
                eased = 1.0 - (1.0 - progress) ** 3  # ease-out cubic
                self._current_w = self._size_anim_w0 + (
                    self._target_w - self._size_anim_w0
                ) * eased
                self._current_h = self._size_anim_h0 + (
                    self._target_h - self._size_anim_h0
                ) * eased
                self._current_x = self._size_anim_x0 + (
                    self._target_x - self._size_anim_x0
                ) * eased
                self._apply_geometry()

            # State-driven view updates
            if self._state == "recording":
                if self._rms_provider is not None:
                    self._view.updateRms_(self._rms_provider(5))
                else:
                    self._view.updateRms_([0.0] * 5)
                self._view.tickPulse_(dt)
            elif self._state == "processing":
                self._view.tickPulse_(dt)

            if self._state != "idle" or self._current_alpha > 0.01:
                self._view.setNeedsDisplay_(True)

        def _apply_geometry(self) -> None:
            if self._window is None:
                return
            base_y = self._screen_origin_y + float(self._bottom_margin)
            
            # Slide-up animation mapped to alpha (0.0 to 1.0)
            slide_offset = 15.0 * max(0.0, 1.0 - (self._current_alpha / max(0.01, self._target_alpha if self._target_alpha > 0 else 1.0)))
            y = base_y - slide_offset  # macOS origin is bottom-left, so subtract to move down
            
            rect = (
                (self._current_x, y),
                (self._current_w, self._current_h),
            )
            self._window.setFrame_display_(rect, True)
            if self._view is not None:
                self._view.setTextWidth_(self._current_w)

        def run(self) -> None:
            """Pump AppKit events on the main thread until close()."""
            assert self._app is not None
            from AppKit import NSTimer  # local import keeps top tidy

            self._animator = _Animator.alloc().initWithHud_(self)
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 60.0, self._animator, b"animationTick:", None, True
            )
            try:
                while not self._stop.is_set():
                    event = self._app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                        _ANY_EVENT_MASK,
                        NSDate.dateWithTimeIntervalSinceNow_(0.016),
                        NSDefaultRunLoopMode,
                        True,
                    )
                    if event is not None:
                        self._app.sendEvent_(event)
            finally:
                if self._timer is not None:
                    self._timer.invalidate()
                    self._timer = None
                if self._window is not None:
                    self._window.orderOut_(None)


# ---------------------------------------------------------------------------
# Windows + Linux implementation (Tkinter; ctypes on Windows)
# ---------------------------------------------------------------------------

if sys.platform != "darwin":

    import tkinter as tk
    import tkinter.font as tkfont

    _IS_WINDOWS = sys.platform == "win32"
    # Without a compositing manager, X11 ignores ``-alpha`` entirely, so an
    # "invisible" alpha-0 window would stay painted on screen. On Linux we
    # therefore unmap (withdraw) the window whenever it is fully faded out.
    _WITHDRAW_WHEN_HIDDEN = not _IS_WINDOWS

    if _IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080

        _user32 = ctypes.windll.user32
        _SetWindowLongPtrW = _user32.SetWindowLongPtrW
        _SetWindowLongPtrW.restype = wintypes.LONG
        _SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        _GetWindowLongPtrW = _user32.GetWindowLongPtrW
        _GetWindowLongPtrPtr = _GetWindowLongPtrW
        _GetWindowLongPtrW.restype = wintypes.LONG
        _GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

    _BG = "#0F172A"
    _ACCENT = "#74E0F0"
    _GLOW_ACCENT = "#164E63"
    _TEXT_COLOR = "#A0AEC0"   # slate-white at ~0.68 appearance over dark slate
    _STATUS_COLOR = "#E2E8F0"
    _DIVIDER_COLOR = "#1E293B"
    # Tk silently falls back to a default font if the family is missing.
    _TEXT_FONT_FAMILY = "Segoe UI" if _IS_WINDOWS else "DejaVu Sans"
    _TEXT_FONT_SIZE = 10
    _TEXT_PADDING_X = 20

    def _get_active_monitor_work_area():
        """Find active monitor's work area (excluding taskbar) on Windows.

        Returns None on other platforms; the caller then falls back to the
        full Tk screen size."""
        if not _IS_WINDOWS:
            return None
        try:
            hwnd = _user32.GetForegroundWindow()
            hmon = 0
            if hwnd:
                hmon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST = 2
            if not hmon:
                pt = wintypes.POINT()
                _user32.GetCursorPos(ctypes.byref(pt))
                hmon = _user32.MonitorFromPoint(pt, 2)
            if hmon:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    return (
                        mi.rcWork.left,
                        mi.rcWork.top,
                        mi.rcWork.right - mi.rcWork.left,
                        mi.rcWork.bottom - mi.rcWork.top,
                    )
        except Exception:
            pass
        return None

    def _fit_text_tk(text: str, max_w: int, font: "tkfont.Font") -> str:
        text = text.strip()
        if not text:
            return ""
        if font.measure(text) <= max_w:
            return text
        words = text.split()
        if not words:
            return ""
        for i in range(1, len(words)):
            cand = "… " + " ".join(words[i:])
            if font.measure(cand) <= max_w:
                return cand
        last_word = words[-1]
        for i in range(len(last_word) - 1, 0, -1):
            cand = "… " + last_word[-i:]
            if font.measure(cand) <= max_w:
                return cand
        return "…"

    class _TkHUD:
        """Tkinter HUD used on Windows and Linux. All Tk calls happen on a
        dedicated daemon thread; the public API only enqueues events."""

        def __init__(
            self,
            rms_provider: Optional[Callable[[int], List[float]]],
            w_collapsed: int,
            h_collapsed: int,
            w_expanded: int,
            h_expanded: int,
            bottom_margin: int,
            fade_seconds: float,
            size_seconds: float,
        ) -> None:
            self._rms_provider = rms_provider
            self._w_collapsed = w_collapsed
            self._h_collapsed = h_collapsed
            self._w_expanded = w_expanded
            self._h_expanded = h_expanded
            self._bottom_margin = bottom_margin
            self._fade_seconds = fade_seconds
            self._size_seconds = size_seconds
            self._state = "idle"
            self._target_alpha = 0.0
            self._current_alpha = 0.0
            self._shown = False
            self._events: "queue.Queue[tuple]" = queue.Queue()

            # Screen bounds state
            self._screen_origin_x: float = 0.0
            self._screen_origin_y: float = 0.0
            self._screen_w: float = 1920.0
            self._screen_h: float = 1080.0

            # Size animation state.
            self._target_w = w_collapsed
            self._target_h = h_collapsed
            self._current_w = float(w_collapsed)
            self._current_h = float(h_collapsed)
            self._current_x = 0.0
            self._target_x = 0.0
            self._size_anim_t = 0.0
            self._size_anim_dur = 0.0
            self._size_anim_w0 = float(w_collapsed)
            self._size_anim_h0 = float(h_collapsed)
            self._size_anim_x0 = 0.0
            self._pulse_t = 0.0

            # Raw + fitted text buffers.
            self._raw_preview = ""
            self._raw_status = ""
            self._fitted_preview = ""
            self._fitted_status = ""
            self._text_dirty = False

            self._top = None
            self._withdrawn = False

            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True, name="imla-hud")
            self._thread.start()

        # -- Public state API (cross-thread via event queue) -----------

        def show_recording(self, preview_active: bool) -> None:
            self._events.put(("recording", preview_active))

        def show_processing(self) -> None:
            self._events.put(("processing", True))

        def set_preview_text(self, text: str) -> None:
            self._events.put(("preview_text", text))

        def set_status_text(self, text: str) -> None:
            self._events.put(("status_text", text))

        def hide(self) -> None:
            self._events.put(("hide", None))

        def close(self) -> None:
            self._stop.set()
            self._events.put(("stop", None))
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=1.0)

        def run(self) -> None:
            # Join in short slices so Ctrl+C (KeyboardInterrupt) is delivered
            # promptly to the main thread on every platform.
            while self._thread.is_alive():
                self._thread.join(timeout=0.2)

        # -- Tk thread -------------------------------------------------

        def _update_screen(self) -> None:
            work = _get_active_monitor_work_area()
            if work:
                self._screen_origin_x, self._screen_origin_y, self._screen_w, self._screen_h = work
            elif hasattr(self, "_top") and self._top:
                self._screen_origin_x = 0.0
                self._screen_origin_y = 0.0
                self._screen_w = float(self._top.winfo_screenwidth())
                self._screen_h = float(self._top.winfo_screenheight())

        def _run(self) -> None:
            try:
                root = tk.Tk()
            except Exception:  # noqa: BLE001
                log.warning(
                    "HUD overlay unavailable (could not open Tk display); "
                    "continuing without on-screen overlay.",
                    exc_info=True,
                )
                self._stop.wait()
                return
            root.withdraw()

            top = tk.Toplevel(root)
            top.overrideredirect(True)
            self._top = top

            self._update_screen()
            x = self._screen_origin_x + self._screen_w - self._w_collapsed - 20.0
            y = self._screen_origin_y + self._screen_h - self._h_collapsed - self._bottom_margin
            top.geometry(f"{self._w_collapsed}x{self._h_collapsed}+{int(x)}+{int(y)}")
            try:
                top.attributes("-topmost", True)
            except tk.TclError:
                pass
            try:
                top.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            top.configure(bg=_BG)
            self._current_x = float(x)
            self._target_x = float(x)

            top.update_idletasks()
            if _IS_WINDOWS:
                try:
                    hwnd = (
                        int(top.wm_frame(), 16)
                        or _user32.GetParent(top.winfo_id())
                        or top.winfo_id()
                    )
                    style = _GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                    style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                    _SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "Could not apply WS_EX_NOACTIVATE; HUD may steal focus.",
                        exc_info=True,
                    )
            if _WITHDRAW_WHEN_HIDDEN:
                # Start unmapped; shown on the first recording/processing event.
                top.withdraw()
                self._withdrawn = True

            self._text_font = tkfont.Font(family=_TEXT_FONT_FAMILY, size=_TEXT_FONT_SIZE, weight="bold")
            canvas = tk.Canvas(
                top,
                width=self._w_collapsed,
                height=self._h_collapsed,
                bg=_BG,
                highlightthickness=0,
                bd=0,
            )
            canvas.pack()

            self._drag_start_x = 0
            self._drag_start_y = 0
            self._manual_pos = False

            def start_drag(event):
                self._drag_start_x = event.x
                self._drag_start_y = event.y
                self._manual_pos = True

            def on_drag(event):
                if self._top is None:
                    return
                dx = event.x - self._drag_start_x
                dy = event.y - self._drag_start_y
                self._current_x += dx
                self._target_x = self._current_x
                if not hasattr(self, '_current_y'):
                    self._current_y = self._top.winfo_y()
                self._current_y += dy
                self._top.geometry(f"+{int(self._current_x)}+{int(self._current_y)}")

            canvas.bind("<ButtonPress-1>", start_drag)
            canvas.bind("<B1-Motion>", on_drag)

            self._canvas = canvas
            self._root = root
            self._last_tick = time.monotonic()
            self._tick()
            root.mainloop()

        def _tick(self) -> None:
            now = time.monotonic()
            dt = max(0.0, now - self._last_tick)
            self._last_tick = now
            self._pulse_t += dt

            # Drain events.
            try:
                while True:
                    kind, payload = self._events.get_nowait()
                    if kind == "recording":
                        self._state = "recording"
                        self._target_alpha = 0.70
                        self._ensure_mapped()
                        self._update_screen()
                        preview_active = bool(payload)
                        target_w = self._w_expanded if preview_active else self._w_collapsed
                        target_h = self._h_expanded if preview_active else self._h_collapsed

                        if getattr(self, "_manual_pos", False):
                            new_x = self._current_x
                        else:
                            new_x = self._screen_origin_x + self._screen_w - target_w - 20.0
                        if self._current_alpha <= 0.01:
                            self._current_x = new_x
                            self._target_x = new_x
                            self._current_w = float(target_w)
                            self._current_h = float(target_h)
                            self._target_w = target_w
                            self._target_h = target_h
                            self._size_anim_dur = 0.0
                            self._apply_geometry()
                        else:
                            self._set_size_target(target_w, target_h)

                        self._raw_preview = ""
                        self._raw_status = ""
                        self._text_dirty = True
                    elif kind == "processing":
                        self._state = "processing"
                        self._target_alpha = 0.70
                        self._ensure_mapped()
                        self._set_size_target(self._w_expanded, self._h_expanded)
                        self._raw_preview = ""
                        self._raw_status = "Transcribing with Whisper..."
                        self._text_dirty = True
                    elif kind == "preview_text":
                        new_text = str(payload)
                        if new_text != self._raw_preview:
                            self._raw_preview = new_text
                            self._text_dirty = True
                    elif kind == "status_text":
                        new_text = str(payload)
                        if new_text != self._raw_status:
                            self._raw_status = new_text
                            self._text_dirty = True
                    elif kind == "hide":
                        self._state = "idle"
                        self._target_alpha = 0.0
                        self._set_size_target(self._w_collapsed, self._h_collapsed)
                        self._raw_preview = ""
                        self._raw_status = ""
                        self._text_dirty = True
                    elif kind == "stop":
                        self._root.quit()
                        return
            except queue.Empty:
                pass

            # Alpha tween.
            if abs(self._target_alpha - self._current_alpha) > 0.005:
                step = dt / max(self._fade_seconds, 1e-3)
                if self._target_alpha > self._current_alpha:
                    self._current_alpha = min(
                        self._target_alpha, self._current_alpha + step
                    )
                else:
                    self._current_alpha = max(
                        self._target_alpha, self._current_alpha - step
                    )
                try:
                    self._top.attributes("-alpha", self._current_alpha)
                except tk.TclError:
                    if self._top is None or not self._top.winfo_exists():
                        return

            if (
                self._state == "idle"
                and self._target_alpha <= 0.0
                and self._current_alpha <= 0.01
            ):
                self._maybe_withdraw()

            # Size tween.
            if (
                abs(self._target_w - self._current_w) > 0.5
                or abs(self._target_h - self._current_h) > 0.5
            ) and self._size_anim_dur > 0.0:
                self._size_anim_t += dt
                progress = min(1.0, self._size_anim_t / self._size_anim_dur)
                eased = 1.0 - (1.0 - progress) ** 3
                self._current_w = self._size_anim_w0 + (
                    self._target_w - self._size_anim_w0
                ) * eased
                self._current_h = self._size_anim_h0 + (
                    self._target_h - self._size_anim_h0
                ) * eased
                self._current_x = self._size_anim_x0 + (
                    self._target_x - self._size_anim_x0
                ) * eased
                self._apply_geometry()

            if self._text_dirty:
                self._refit_text()
                self._text_dirty = False

            if self._state != "idle" or self._current_alpha > 0.01:
                self._redraw()
            self._root.after(16, self._tick)

        def _ensure_mapped(self) -> None:
            """Map the window again if it was withdrawn (Linux only)."""
            if not self._withdrawn or self._top is None:
                return
            try:
                self._top.deiconify()
                self._top.lift()
                self._top.attributes("-topmost", True)
            except tk.TclError:
                return
            self._withdrawn = False

        def _maybe_withdraw(self) -> None:
            """Unmap the fully faded-out window (Linux only)."""
            if not _WITHDRAW_WHEN_HIDDEN or self._withdrawn or self._top is None:
                return
            try:
                self._top.withdraw()
            except tk.TclError:
                return
            self._withdrawn = True

        def _refit_text(self) -> None:
            text_w = max(0, int(self._current_w) - 2 * _TEXT_PADDING_X)
            self._fitted_preview = _fit_text_tk(
                self._raw_preview, text_w, self._text_font
            )
            self._fitted_status = _fit_text_tk(
                self._raw_status, text_w, self._text_font
            )

        def _set_size_target(self, w: int, h: int) -> None:
            if w == self._target_w and h == self._target_h:
                return
            if getattr(self, "_manual_pos", False):
                new_x = self._current_x
            else:
                new_x = self._screen_origin_x + self._screen_w - w - 20.0
                self._target_x = new_x
            self._size_anim_w0 = self._current_w
            self._size_anim_h0 = self._current_h
            self._size_anim_x0 = self._current_x
            self._target_w = w
            self._target_h = h
            self._target_x = new_x
            self._size_anim_t = 0.0
            self._size_anim_dur = self._size_seconds

        def _apply_geometry(self) -> None:
            if self._top is None:
                return
            if getattr(self, "_manual_pos", False) and hasattr(self, "_current_y"):
                base_y = self._current_y
            else:
                base_y = self._screen_origin_y + self._screen_h - self._current_h - self._bottom_margin
                self._current_y = base_y
            
            # Slide-up animation mapped to alpha (0.0 to 0.70)
            slide_offset = 15.0 * max(0.0, 1.0 - (self._current_alpha / max(0.01, self._target_alpha if self._target_alpha > 0 else 0.70)))
            y = base_y + slide_offset
            
            self._top.geometry(
                f"{int(self._current_w)}x{int(self._current_h)}"
                f"+{int(self._current_x)}+{int(y)}"
            )
            self._canvas.configure(
                width=int(self._current_w), height=int(self._current_h)
            )

        def _redraw(self) -> None:
            canvas = self._canvas
            w = self._current_w
            h = self._current_h
            canvas.delete("all")

            # Rounded rectangle background
            r = 22.0
            d = r * 2
            canvas.create_oval(0, 0, d, d, fill=_BG, outline="")
            canvas.create_oval(w - d, 0, w, d, fill=_BG, outline="")
            canvas.create_oval(0, h - d, d, h, fill=_BG, outline="")
            canvas.create_oval(w - d, h - d, w, h, fill=_BG, outline="")
            canvas.create_rectangle(r, 0, w - r, h, fill=_BG, outline="")
            canvas.create_rectangle(0, r, w, h - r, fill=_BG, outline="")

            if h >= 70.0:
                self._draw_expanded(w, h)
            else:
                self._draw_collapsed(w, h)

        def _draw_collapsed(self, w: float, h: float) -> None:
            if self._state == "recording":
                rms = self._rms_provider(5) if self._rms_provider else [0.0] * 5
                self._draw_bars(w, h, rms)
            elif self._state == "processing":
                self._draw_breathing(w, h)

        def _draw_expanded(self, w: float, h: float) -> None:
            top_h = h / 2.0
            if self._state == "recording":
                rms = self._rms_provider(5) if self._rms_provider else [0.0] * 5
                self._draw_ink_waveform(w, top_h, rms)
            elif self._state == "processing":
                self._draw_breathing(w, top_h)

            # Divider
            canvas = self._canvas
            canvas.create_rectangle(
                24, top_h - 0.5, w - 24, top_h + 0.5,
                fill=_DIVIDER_COLOR, outline="",
            )

            is_recording = (self._state == "recording")
            text = (
                self._fitted_preview
                if is_recording
                else self._fitted_status
            )
            if text:
                color = _TEXT_COLOR if is_recording else _STATUS_COLOR
                text_y = top_h + (top_h - 16.0) / 2.0
                canvas.create_text(
                    _TEXT_PADDING_X, text_y,
                    text=text, font=self._text_font,
                    fill=color, anchor="nw",
                )

        def _draw_ink_waveform(self, w: float, top_h: float, rms: List[float]) -> None:
            # Modern frequency-bar equalizer
            n = 7
            gap = 6.0
            bar_w = 4.0
            total = n * bar_w + (n - 1) * gap
            x0 = (w - total) / 2.0
            mid = top_h / 2.0
            max_h = top_h * 0.65
            
            # Smooth out the RMS to 7 bars
            levels = []
            if rms and len(rms) == 5:
                levels = [rms[0], (rms[0]+rms[1])/2, rms[1], rms[2], rms[3], (rms[3]+rms[4])/2, rms[4]]
            else:
                levels = [0.0] * n
            
            for i, lvl in enumerate(levels):
                lh = max(4.0, min(max_h, lvl * max_h * 2.5))
                # Add a subtle sine bounce even when quiet
                bounce = math.sin(self._pulse_t * 3.5 + i * 0.5) * 2.5
                lh = max(4.0, lh + bounce)
                x = x0 + i * (bar_w + gap)
                self._canvas.create_oval(
                    x, mid - lh / 2.0,
                    x + bar_w, mid + lh / 2.0,
                    fill=_ACCENT, outline="",
                )

        def _draw_bars(self, w: float, h: float, rms: List[float]) -> None:
            n = 5
            gap = 5.0
            bar_w = 3.0
            total = n * bar_w + (n - 1) * gap
            x0 = (w - total) / 2.0
            mid = h / 2.0
            max_h = h * 0.55
            for i, lvl in enumerate(rms):
                lh = max(3.0, min(max_h, lvl * max_h * 2.2))
                x = x0 + i * (bar_w + gap)
                self._canvas.create_oval(
                    x, mid - lh / 2.0,
                    x + bar_w, mid + lh / 2.0,
                    fill=_ACCENT, outline="",
                )

        def _draw_breathing(self, w: float, h: float) -> None:
            n = 3
            d = 6.0
            gap = 7.0
            total = n * d + (n - 1) * gap
            x0 = (w - total) / 2.0
            mid_y = h / 2.0
            for i in range(n):
                phase = self._pulse_t * 2.8 - i * 0.7
                s = 0.5 + 0.5 * math.sin(phase)
                r = d / 2.0 + s * 2.0
                self._canvas.create_oval(
                    x0 + i * (d + gap) + d / 2.0 - r,
                    mid_y - r,
                    x0 + i * (d + gap) + d / 2.0 + r,
                    mid_y + r,
                    fill=_ACCENT, outline="",
                )

    # Backwards-compatible name.
    _WindowsHUD = _TkHUD
