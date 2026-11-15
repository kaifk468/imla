"""Real-time streaming speech preview via Vosk.

Engine 1 of Imla's two-model architecture. This recognizer produces a
provisional live transcript that is rendered on the HUD overlay while the
user is holding the push-to-talk key. It is NEVER injected at the cursor
— Whisper (Engine 2) is the sole source of truth for the injected text.

The recognizer runs on a dedicated worker thread. The audio callback feeds
it via a bounded queue (``QUEUE_MAXSIZE``). If Vosk falls behind, frames
are dropped rather than queued, keeping latency low for the live preview.

If the Vosk package is unavailable on this platform (e.g. macOS arm64,
which has no supported wheel as of 0.3.45) or the model fails to download,
``is_ready()`` returns ``False`` and the HUD falls back to the small
collapsed pill — Whisper keeps working.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
import urllib.request
import zipfile
from math import gcd
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from scipy import signal

log = logging.getLogger("imla.preview")

VOSK_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = (
    f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
)
CACHE_DIR = Path.home() / ".cache" / "imla" / "models"

QUEUE_MAXSIZE = 8       # ~0.8 s of audio at ~0.1 s/frame
RATE_LIMIT_HZ = 15      # preview text updates per second
PREVIEW_SR = 16_000     # Vosk requires 16 kHz mono PCM int16
DOWNLOAD_TIMEOUT_S = 120


def is_model_cached() -> bool:
    """Return True if the Vosk model directory exists and has files."""
    model_dir = CACHE_DIR / VOSK_MODEL_NAME
    return model_dir.exists() and any(model_dir.iterdir())


def download_model(progress_cb: Optional[Callable[[int, int], None]] = None) -> Path:
    """Download (if needed) and extract the Vosk model, calling progress_cb(downloaded, total)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_dir = CACHE_DIR / VOSK_MODEL_NAME
    if model_dir.exists() and any(model_dir.iterdir()):
        if progress_cb:
            progress_cb(1, 1)
        return model_dir

    zip_path = CACHE_DIR / f"{VOSK_MODEL_NAME}.zip"
    if not zip_path.exists():
        log.info("Downloading Vosk model (~40 MB) to %s ...", zip_path)
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(float(DOWNLOAD_TIMEOUT_S))
        try:
            def reporthook(block_num: int, block_size: int, total_size: int) -> None:
                if progress_cb:
                    downloaded = block_num * block_size
                    if total_size > 0:
                        downloaded = min(downloaded, total_size)
                    progress_cb(downloaded, total_size)

            urllib.request.urlretrieve(VOSK_MODEL_URL, zip_path, reporthook=reporthook)
        finally:
            socket.setdefaulttimeout(old_timeout)
        log.info("Download complete.")

    log.info("Extracting Vosk model...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(CACHE_DIR)
    log.info("Extraction complete.")
    if progress_cb:
        progress_cb(1, 1)
    return model_dir


def _ensure_model() -> Path:
    """Download (if needed) and extract the Vosk model into the cache dir."""
    return download_model()


class StreamingPreview:
    """Continuous Vosk recognizer for live preview text only.

    Output is delivered via the ``on_partial`` callback, rate-limited to
    ``RATE_LIMIT_HZ`` updates per second.
    """

    def __init__(self, on_partial: Callable[[str], None]) -> None:
        self._on_partial = on_partial
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active = False
        self._native_sr = PREVIEW_SR
        self._last_partial = ""
        self._recognizer = None
        self._model_ready = threading.Event()
        self._model_unavailable = threading.Event()
        self._model_dir: Optional[Path] = None
        self._model = None
        self._finalized_text = ""
        # Check vosk availability up front so we don't waste bandwidth
        # downloading a model that can't be loaded.
        try:
            import vosk  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            log.warning(
                "Vosk package not installed on this platform; "
                "streaming preview disabled (Whisper still works)."
            )
            self._model_unavailable.set()
            return

        # Kick off model prep in the background so the first hotkey press
        # doesn't pay the download/extract cost.
        threading.Thread(target=self._prepare_model, daemon=True).start()

    # ------------------------------------------------------------------
    # Public state

    def is_ready(self) -> bool:
        """True once the Vosk model is downloaded, extracted, and loadable.

        If Vosk isn't installed on this platform or the model fails to
        download, this stays False and ``start()`` returns False."""
        return self._model_ready.is_set()

    def start(self, native_sr: int) -> bool:
        """Wait up to 30s for the model, then start the worker thread.

        Returns True if running, False if the model isn't available
        (in which case the HUD should fall back to the small pill)."""
        if self._model_unavailable.is_set():
            return False
        if not self._model_ready.wait(timeout=30.0):
            log.warning("Vosk model not ready within 30s; preview disabled.")
            return False

        try:
            import vosk  # type: ignore[import-not-found]

            if self._model is None:
                vosk.SetLogLevel(-1)
                self._model = vosk.Model(str(self._model_dir))
            self._recognizer = vosk.KaldiRecognizer(self._model, PREVIEW_SR)
            self._recognizer.SetWords(True)
        except Exception:
            log.exception("Failed to initialize Vosk recognizer.")
            self._model_unavailable.set()
            return False

        self._native_sr = native_sr
        self._stop.clear()
        self._finalized_text = ""
        self._last_partial = ""
        # Drain any stale frames from a previous session.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._active = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        log.info("Streaming preview started (native_sr=%d).", native_sr)
        return True

    def feed(self, chunk: np.ndarray) -> None:
        """Called from the audio callback (PortAudio thread).

        Downmixes to mono, resamples to 16 kHz, converts to int16 PCM,
        and pushes to the worker queue. Drops stale frames if the queue
        is full so latency never grows unbounded."""
        if not self._active or self._recognizer is None:
            return

        # Downmix to mono (float32).
        if chunk.ndim == 2 and chunk.shape[1] > 1:
            mono = np.mean(chunk, axis=1).astype(np.float32, copy=False)
        elif chunk.ndim == 2:
            mono = chunk[:, 0].astype(np.float32, copy=False)
        else:
            mono = chunk.astype(np.float32, copy=False)

        # Resample to 16 kHz.
        if self._native_sr != PREVIEW_SR:
            g = gcd(self._native_sr, PREVIEW_SR)
            up = PREVIEW_SR // g
            down = self._native_sr // g
            mono = signal.resample_poly(mono, up, down).astype(
                np.float32, copy=False
            )

        # Float32 → int16 PCM bytes.
        pcm = (
            (mono * 32767.0)
            .clip(-32768.0, 32767.0)
            .astype(np.int16)
            .tobytes()
        )

        # Drop stale frames if the worker is behind.
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except queue.Full:
                pass

    def stop(self) -> None:
        """Drain remaining audio and shut down the worker."""
        self._active = False
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # unblock the worker
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)
            self._worker = None
        self._recognizer = None

    # ------------------------------------------------------------------
    # Internals

    def _prepare_model(self) -> None:
        try:
            self._model_dir = _ensure_model()
            import vosk  # type: ignore[import-not-found]

            vosk.SetLogLevel(-1)
            self._model = vosk.Model(str(self._model_dir))
            self._model_ready.set()
            log.info("Vosk model ready and cached at %s", self._model_dir)
        except Exception:
            log.exception("Failed to prepare Vosk model; preview disabled.")
            self._model_unavailable.set()

    def _run(self) -> None:
        last_emit = 0.0
        rate_limit_s = 1.0 / RATE_LIMIT_HZ
        while not self._stop.is_set():
            try:
                frame = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if frame is None or self._recognizer is None:
                break

            accepted = self._recognizer.AcceptWaveform(frame)
            if accepted:
                result = json.loads(self._recognizer.Result())
                text = result.get("text", "").strip()
                if text:
                    self._finalized_text = (
                        f"{self._finalized_text} {text}".strip()
                        if self._finalized_text
                        else text
                    )
                    self._last_partial = self._finalized_text
            else:
                result = json.loads(self._recognizer.PartialResult())
                partial = result.get("partial", "").strip()
                if partial:
                    self._last_partial = (
                        f"{self._finalized_text} {partial}".strip()
                        if self._finalized_text
                        else partial
                    )
                else:
                    self._last_partial = self._finalized_text

            now = time.monotonic()
            if now - last_emit >= rate_limit_s:
                try:
                    self._on_partial(self._last_partial)
                except Exception:
                    log.exception("on_partial handler failed")
                last_emit = now

        if self._last_partial:
            try:
                self._on_partial(self._last_partial)
            except Exception:
                pass
