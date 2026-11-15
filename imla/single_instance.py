"""Single-instance guard. Never raises."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("imla.single_instance")

ERROR_ALREADY_EXISTS = 183

_handles: dict = {}


def _lock_dir() -> Path:
    return Path.home() / ".config" / "imla"


def acquire(name: str = "imla") -> bool:
    """True if we are the only instance; the lock is held until process exit."""
    if name in _handles:
        return True
    try:
        if sys.platform == "win32":
            return _acquire_windows(name)
        return _acquire_posix(name)
    except Exception as e:  # noqa: BLE001
        log.warning("Single-instance check failed (%s); continuing", e)
        return True


def _acquire_windows(name: str) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateMutexW(None, False, f"Local\\{name}-single-instance")
    err = ctypes.get_last_error()
    if not handle:
        log.warning("CreateMutexW failed (error %s); continuing", err)
        return True
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return False
    _handles[name] = handle
    return True


def _acquire_posix(name: str) -> bool:
    import fcntl

    d = _lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / f"{name}.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _handles[name] = f
    return True


def release(name: str = "imla") -> None:
    h = _handles.pop(name, None)
    if h is None:
        return
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(h))
        else:
            h.close()
    except Exception:  # noqa: BLE001
        pass
