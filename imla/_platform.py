"""Small platform helpers shared across modules."""

from __future__ import annotations

import os
import sys

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def is_wayland_session() -> bool:
    """True when running inside a Wayland desktop session on Linux."""
    if not IS_LINUX:
        return False
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")

