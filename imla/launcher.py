"""How Imla re-launches itself (source checkout or frozen build)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def gui_python() -> str:
    """Interpreter to use for a windowless child (pythonw.exe on Windows)."""
    if sys.platform == "win32" and not is_frozen():
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def launch_command(*extra_args: str, windowed: bool = True) -> list:
    if is_frozen():
        return [sys.executable, *extra_args]
    py = gui_python() if windowed else sys.executable
    return [py, "-m", "imla.main", *extra_args]


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
