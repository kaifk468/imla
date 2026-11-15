"""File logging for a windowless (pythonw.exe) app."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Optional

LOG_DIR = Path.home() / ".config" / "imla"
LOG_PATH = LOG_DIR / "imla.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
_MARK = "_imla_handler"


def setup_logging(level: int = logging.INFO, log_dir: Optional[Path] = None) -> Path:
    """Configure root logging (rotating file + console if available).

    Idempotent. Returns the log file path.
    """
    # pythonw.exe: no stdout/stderr. Give stray print() a sink.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
        has_console = False
    else:
        has_console = True
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115

    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    path = directory / "imla.log"

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, _MARK, False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        setattr(fh, _MARK, True)
        root.addHandler(fh)
    except OSError:
        pass
    if has_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        setattr(sh, _MARK, True)
        root.addHandler(sh)
    return path


def install_excepthook() -> None:
    """Log uncaught exceptions (main thread and worker threads)."""
    log = logging.getLogger("imla.crash")

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "Uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
