"""Start-on-login integration.

macOS   — LaunchAgent plist at ``~/Library/LaunchAgents/<service>.plist``
Windows — ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` value
Linux   — XDG autostart entry at ``~/.config/autostart/imla.desktop``

All are user-level (no sudo / UAC prompt required).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("imla.startup")

SERVICE_ID = "com.imla.dictation"
DISPLAY_NAME = "ImlaDictation"


def _project_root() -> Path:
    # imla/startup.py -> imla-ai/
    return Path(__file__).resolve().parent.parent


def _is_linux() -> bool:
    return sys.platform.startswith("linux") or "bsd" in sys.platform


def is_enabled() -> bool:
    if sys.platform == "darwin":
        return _macos_plist_path().exists()
    if sys.platform == "win32":
        return _windows_registry_value_exists()
    if _is_linux():
        return _linux_desktop_path().exists()
    return False


def enable() -> bool:
    try:
        if sys.platform == "darwin":
            _enable_macos()
        elif sys.platform == "win32":
            _enable_windows()
        elif _is_linux():
            _enable_linux()
        else:
            log.warning("Start-on-login not supported on %s", sys.platform)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to enable start-on-login: %s", e)
        return False


def disable() -> bool:
    try:
        if sys.platform == "darwin":
            _disable_macos()
        elif sys.platform == "win32":
            _disable_windows()
        elif _is_linux():
            _disable_linux()
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("Failed to disable start-on-login: %s", e)
        return False


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_ID}.plist"


def _enable_macos() -> None:
    plist_path = _macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    project_root = _project_root()

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVICE_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>imla.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    plist_path.write_text(content, encoding="utf-8")
    log.info("LaunchAgent written: %s", plist_path)


def _disable_macos() -> None:
    plist_path = _macos_plist_path()
    if plist_path.exists():
        plist_path.unlink()
        log.info("LaunchAgent removed: %s", plist_path)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _windows_run_key():
    import winreg  # type: ignore[import-not-found]

    return (
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
    )


def _windows_command() -> str:
    python = sys.executable
    project_root = _project_root()
    return f'"{python}" -m imla.main'


def _windows_registry_value_exists() -> bool:
    import winreg  # type: ignore[import-not-found]

    hive, subkey = _windows_run_key()
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
    except OSError:
        return False
    try:
        try:
            winreg.QueryValueEx(key, DISPLAY_NAME)
            return True
        except FileNotFoundError:
            return False
    finally:
        winreg.CloseKey(key)


def _enable_windows() -> None:
    import winreg  # type: ignore[import-not-found]

    hive, subkey = _windows_run_key()
    cmd = _windows_command()
    key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, DISPLAY_NAME, 0, winreg.REG_SZ, cmd)
    finally:
        winreg.CloseKey(key)
    log.info("Run-on-login registry value set: %s", DISPLAY_NAME)


def _disable_windows() -> None:
    import winreg  # type: ignore[import-not-found]

    hive, subkey = _windows_run_key()
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return
    try:
        try:
            winreg.DeleteValue(key, DISPLAY_NAME)
        except FileNotFoundError:
            pass
    finally:
        winreg.CloseKey(key)
    log.info("Run-on-login registry value removed: %s", DISPLAY_NAME)


# ---------------------------------------------------------------------------
# Linux (XDG autostart — honoured by GNOME, KDE, XFCE, Cinnamon, MATE, …)
# ---------------------------------------------------------------------------


def _linux_desktop_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "autostart" / "imla.desktop"


def _desktop_quote(arg: str) -> str:
    """Quote one argument for a .desktop ``Exec=`` line (XDG spec: double
    quotes, with ``"`` `` ` `` ``$`` ``\\`` backslash-escaped; ``%`` doubled)."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


def _enable_linux() -> None:
    path = _linux_desktop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(
        _desktop_quote(part) for part in (sys.executable, "-m", "imla.main")
    )
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Imla\n"
        "Comment=Push-to-talk voice dictation\n"
        f"Exec={exec_line}\n"
        f"Path={_project_root()}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    path.write_text(content, encoding="utf-8")
    log.info("Autostart entry written: %s", path)


def _disable_linux() -> None:
    path = _linux_desktop_path()
    if path.exists():
        path.unlink()
        log.info("Autostart entry removed: %s", path)
