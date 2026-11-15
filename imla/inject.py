"""Safe text injection at the active system cursor.

Strategy: backup clipboard → put transcript on clipboard → simulate Cmd/Ctrl+V
via pynput → wait briefly → restore original clipboard.

Clipboard backends: ``pbcopy``/``pbpaste`` (macOS), ``win32clipboard``
(Windows), ``wl-copy``/``wl-paste`` or ``xclip``/``xsel`` (Linux).
On Wayland the paste keystroke is sent with ``wtype`` or ``ydotool`` when
available, since pynput can only reach XWayland windows there.

We deliberately avoid raw CGEvent posting and missing C constants like
kVK_Command. pynput handles modifier routing per-platform.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from pynput.keyboard import Controller, Key

from ._platform import is_wayland_session

log = logging.getLogger("imla.inject")

_kb = Controller()


def is_trusted(prompt: bool = False) -> bool:
    """True if this process may post synthetic keystrokes.

    On macOS, simulated key events are silently discarded unless the running
    app / terminal is granted Accessibility. When ``prompt=True``, macOS
    registers the responsible app in System Settings → Privacy & Security →
    Accessibility and shows the native permission dialog.
    """
    if sys.platform != "darwin":
        return True
    try:
        if prompt:
            from HIServices import AXIsProcessTrustedWithOptions  # type: ignore[import-not-found]

            return bool(
                AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
            )
        from ApplicationServices import AXIsProcessTrusted  # type: ignore[import-not-found]

        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        log.debug("AXIsProcessTrusted check unavailable", exc_info=True)
        return True


def accessibility_hint() -> str:
    """Human-readable target the user must enable in System Settings → Accessibility."""
    import os

    exe = os.path.realpath(sys.executable)
    term = os.environ.get("TERM_PROGRAM") or os.environ.get("__CFBundleIdentifier")
    if term:
        return f"your terminal/IDE app ({term}) — or {exe}"
    return f"your terminal/IDE app (e.g. Terminal / Antigravity / iTerm) or {exe}"


def _read_clipboard() -> Optional[str]:
    """Return the current textual clipboard contents, or None if unavailable."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["pbpaste", "-Prefer", "ascii"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            ).stdout
            return out
        except Exception:  # noqa: BLE001
            log.warning("pbpaste failed; clipboard backup unavailable.", exc_info=True)
            return None

    if sys.platform == "win32":
        try:
            import win32clipboard  # type: ignore[import-not-found]

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return data
            finally:
                win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            log.debug("win32 read failed", exc_info=True)
            return None
        return None

    # Linux / BSD: external clipboard tools (see _linux_clipboard_cmd).
    cmd = _linux_clipboard_cmd(read=True)
    if cmd is None:
        _warn_no_clipboard_tool()
        return None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1.0, check=False,
        )
    except Exception:  # noqa: BLE001
        log.debug("clipboard read via %s failed", cmd[0], exc_info=True)
        return None
    # Non-zero usually means "clipboard empty" — nothing to restore.
    return proc.stdout if proc.returncode == 0 else None


def _write_clipboard(text: str) -> bool:
    """Write text to the system clipboard. Returns True on success."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                timeout=5.0,
                check=True,
            )
            return True
        except Exception:  # noqa: BLE001
            # Never fall through to the tkinter fallback here: creating a Tk
            # root off the main thread aborts the process on modern macOS
            # (Tk 8.6 calls -[NSApplication macOSVersion], which no longer
            # exists). Report failure instead.
            log.warning("pbcopy failed; clipboard not updated.", exc_info=True)
            return False

    if sys.platform == "win32":
        try:
            import win32clipboard  # type: ignore[import-not-found]

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:  # noqa: BLE001
            log.debug("win32 write failed", exc_info=True)
            return False

    # Linux / BSD: external clipboard tools. (A Tk-based fallback is not
    # viable here: X11 drops the clipboard as soon as the owning Tk window
    # is destroyed, and the HUD already owns a Tk interpreter.)
    cmd = _linux_clipboard_cmd(read=False)
    if cmd is None:
        _warn_no_clipboard_tool()
        return False
    try:
        # wl-copy / xclip / xsel fork a background process that keeps
        # serving the selection; send its stdio to DEVNULL so we don't
        # block waiting on inherited pipes.
        subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=True,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("%s failed; clipboard not updated.", cmd[0], exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Linux helpers
# ---------------------------------------------------------------------------

_warned_no_clipboard = False


def _linux_clipboard_cmd(read: bool) -> Optional[List[str]]:
    """Pick a clipboard CLI: wl-clipboard on Wayland, else xclip / xsel."""
    candidates: List[List[str]] = []
    if is_wayland_session():
        candidates.append(["wl-paste", "--no-newline"] if read else ["wl-copy"])
    candidates.append(
        ["xclip", "-selection", "clipboard", "-o"]
        if read
        else ["xclip", "-selection", "clipboard", "-i"]
    )
    candidates.append(
        ["xsel", "--clipboard", "--output"] if read else ["xsel", "--clipboard", "--input"]
    )
    for cmd in candidates:
        if shutil.which(cmd[0]):
            return cmd
    return None


def _warn_no_clipboard_tool() -> None:
    global _warned_no_clipboard
    if _warned_no_clipboard:
        return
    _warned_no_clipboard = True
    hint = "wl-clipboard" if is_wayland_session() else "xclip (or xsel)"
    log.error(
        "No clipboard tool found — install %s, e.g. `sudo apt install %s`.",
        hint,
        "wl-clipboard" if is_wayland_session() else "xclip",
    )


def _linux_paste_wayland() -> bool:
    """Send Ctrl+V to the focused *native* Wayland window.

    pynput can only synthesize keys into XWayland windows, so try the
    Wayland-native tools first:
      * ``wtype``   — wlroots compositors (Sway, Hyprland, …)
      * ``ydotool`` — any compositor; needs the ``ydotoold`` daemon running
    Returns True if one of them succeeded."""
    attempts = [
        ["wtype", "-M", "ctrl", "v", "-m", "ctrl"],
        # ydotool ≥ 1.0 syntax: <evdev keycode>:<1=down|0=up>; 29=LCtrl, 47=V
        ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
    ]
    for cmd in attempts:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=2.0,
                check=False,
            )
        except Exception:  # noqa: BLE001
            log.debug("%s failed", cmd[0], exc_info=True)
            continue
        if r.returncode == 0:
            return True
        log.debug(
            "%s exited %d: %s", cmd[0], r.returncode,
            (r.stderr or b"").decode("utf-8", "replace").strip(),
        )
    return False


def inject_text(text: str) -> bool:
    """Paste ``text`` at the current cursor position via clipboard paste.

    Backs up and restores the user's clipboard. Returns True only if the paste
    was actually dispatched; False if the clipboard could not be written or the
    process lacks permission to post keystrokes."""
    if not text:
        return False

    if not is_trusted(prompt=True):
        log.error(
            "Not injecting: no Accessibility permission, so macOS would discard "
            "the paste. Enable %s under System Settings → Privacy & "
            "Security → Accessibility. "
            "Transcript left on clipboard — press Cmd+V to paste it.",
            accessibility_hint(),
        )
        # Still stage the text so the dictation isn't lost; skip the restore
        # below so it survives on the clipboard for a manual paste.
        _write_clipboard(text)
        return False

    original = _read_clipboard()
    if not _write_clipboard(text):
        log.warning("Could not write transcript to clipboard; skipping injection.")
        return False

    modifier = Key.cmd if sys.platform == "darwin" else Key.ctrl
    pasted_natively = False
    if is_wayland_session():
        pasted_natively = _linux_paste_wayland()
        if not pasted_natively:
            log.warning(
                "Wayland: neither wtype nor ydotool could send Ctrl+V; falling "
                "back to pynput, which only reaches XWayland apps. If nothing "
                "was pasted, the transcript is on the clipboard."
            )
    if not pasted_natively:
        try:
            # Linux Terminal support: GNOME Terminal, Konsole, etc. use Ctrl+Shift+V
            use_shift = False
            if sys.platform.startswith("linux") and shutil.which("xdotool"):
                try:
                    # Get active window ID and its class
                    res = subprocess.run(
                        ["xdotool", "getactivewindow"],
                        capture_output=True,
                        text=True,
                        timeout=0.5,
                        check=False,
                    )
                    if res.returncode == 0:
                        win_id = res.stdout.strip()
                        res_class = subprocess.run(
                            ["xprop", "-id", win_id, "WM_CLASS"],
                            capture_output=True,
                            text=True,
                            timeout=0.5,
                            check=False,
                        )
                        if res_class.returncode == 0:
                            win_class = res_class.stdout.lower()
                            # Pure terminals usually have these classes.
                            # We exclude JetBrains (IntelliJ) because its terminal handles Ctrl+V.
                            term_classes = ["terminal", "xterm", "konsole", "alacritty", "kitty", "terminator"]
                            if any(t in win_class for t in term_classes) and "jetbrains" not in win_class:
                                use_shift = True
                                log.debug("Terminal detected (class: %s); using Ctrl+Shift+V", win_class)
                except Exception:
                    log.debug("Failed to detect active window class", exc_info=True)

            with _kb.pressed(modifier):
                if use_shift:
                    with _kb.pressed(Key.shift):
                        _kb.tap("v")
                else:
                    _kb.tap("v")
        except Exception:  # noqa: BLE001
            log.exception("Simulated paste failed.")
            return False

    # Give the receiving app time to consume the paste event before we overwrite
    # the clipboard back. 80ms was too tight for Electron apps (Slack, VS Code),
    # which would paste the restored contents or nothing at all.
    time.sleep(0.35)

    if original is not None:
        _write_clipboard(original)
    return True
