"""Resilient audio capture with stereo downmixing and 16 kHz resampling.

Many wireless microphones (DJI Mic, Rode, headsets) report 2 stereo channels.
Opening with channels=1 silently drops channel 2. We always open at the
device's native channel count, downmix to mono after capture, and resample
to 16 kHz float32 for whisper.

Resilience
----------
* PortAudio exceptions during ``start()`` (e.g. macOS AUHAL ``-10851``,
  Windows ``-9986``) trigger a re-initialization of the audio backend and
  a fallback to a built-in microphone so the app keeps running.
* 100% digital silence for >=1.5 s (a wireless dongle with the headset
  turned off) is detected via per-chunk peak amplitude; a callback fires
  so the host app can alert and switch to a fallback mic.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np
import sounddevice as sd
from scipy import signal

log = logging.getLogger("imla.audio")

# Whisper expects 16 kHz mono float32.
TARGET_SR = 16_000

# Below this peak amplitude the chunk is considered "silent".
SILENCE_PEAK_THRESHOLD = 1e-6
# Continuous silence this long triggers the fallback callback.
SILENCE_DURATION_S = 1.5

_PreferredBuiltinNames = (
    "macbook",
    "built-in",
    "built in",
    "internal",
    "system default",
)


class Recorder:
    """Push-to-talk recorder with auto-recovery and silence detection."""

    def __init__(
        self,
        on_silence_fallback: Optional[Callable[[], None]] = None,
        silence_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self._stream: Optional[sd.InputStream] = None
        self._buffers: List[np.ndarray] = []
        self._lock = threading.Lock()
        self._native_sr: int = TARGET_SR
        self._native_channels: int = 1
        self._device_name: str = ""
        self._device_index: Optional[int] = None
        # Optional live-preview consumer (e.g. StreamingPreview).
        self._preview_sink: Optional[Callable[[np.ndarray], None]] = None
        # Wall-clock timestamp of the most recent successful start().
        self.start_time: Optional[float] = None
        # Silence detection.
        self._silence_started_at: Optional[float] = None
        self._silence_callback: Optional[Callable[[], None]] = (
            on_silence_fallback or silence_callback
        )
        # Device lock (from CLI / config).
        self._device_lock: Optional[Tuple[Optional[int], Optional[str]]] = None

    # ------------------------------------------------------------------
    # Public API

    @property
    def native_sample_rate(self) -> int:
        return self._native_sr

    @property
    def native_sr(self) -> int:
        return self._native_sr

    @property
    def native_channels(self) -> int:
        return self._native_channels

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def device_index(self) -> Optional[int]:
        if self._device_lock is not None and self._device_lock[0] is not None:
            return self._device_lock[0]
        return self._device_index

    @property
    def device(self) -> Optional[int]:
        return self.device_index

    def set_preview_sink(
        self, sink: Optional[Callable[[np.ndarray], None]]
    ) -> None:
        self._preview_sink = sink

    def set_silence_callback(
        self, callback: Optional[Callable[[], None]]
    ) -> None:
        """Install a one-shot callback fired when 100% silence persists
        for >= ``SILENCE_DURATION_S`` seconds (a dead wireless dongle)."""
        self._silence_callback = callback
        self._silence_started_at = None

    def lock_device(
        self,
        index: Optional[int] = None,
        name_substring: Optional[str] = None,
    ) -> None:
        """Restrict subsequent ``start()`` calls to a specific device.

        Pass ``index`` (preferred) or ``name_substring`` (case-insensitive
        match against the device's name). Pass both as ``None`` to clear."""
        self._device_lock = (index, name_substring)

    def start(self) -> Tuple[int, int]:
        """Open the input stream at its native rate & channel count.

        Returns ``(native_sample_rate, native_channels)``.

        If the requested device fails to open (headset unplugged, AUHAL
        error, etc.), transparently retries with a fallback mic so the
        app never crashes."""
        # Reset silence tracker.
        self._silence_started_at = None

        with self._lock:
            self._buffers = []

        # Choose device: locked > configured > default.
        target_idx, target_name = (None, None)
        if self._device_lock is not None:
            target_idx, target_name = self._device_lock
        if target_idx is None and target_name is None:
            try:
                dev = sd.query_devices(kind="input")
                target_idx = dev.get("index")
            except Exception as e:  # noqa: BLE001
                log.warning("query_devices failed: %s", e)

        try:
            return self._open_stream(target_idx, target_name)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "Initial input device failed (%s); attempting fallback.",
                e,
            )
            return self._open_with_fallback(e)

    def switch_to_fallback(self) -> Optional[int]:
        """Switch device lock to the best available fallback microphone (e.g. built-in).
        Returns the new device index."""
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:  # noqa: BLE001
            log.warning("PortAudio re-init failed: %s", e)

        devices = sd.query_devices()
        inputs = [
            (i, d) for i, d in enumerate(devices)
            if d.get("max_input_channels", 0) > 0
        ]
        if not inputs:
            return None

        fallback_idx = None
        for idx, d in inputs:
            name = d.get("name", "").lower()
            if any(tag in name for tag in _PreferredBuiltinNames):
                fallback_idx = idx
                break
        if fallback_idx is None:
            try:
                default = sd.query_devices(kind="input")
                fallback_idx = default.get("index")
            except Exception:  # noqa: BLE001
                pass
        if fallback_idx is None and inputs:
            fallback_idx = inputs[0][0]

        if fallback_idx is not None:
            self._device_index = fallback_idx
            self.lock_device(index=fallback_idx)
            log.info(
                "Switched recorder to fallback device #%s: %s",
                fallback_idx,
                devices[fallback_idx].get("name", "?"),
            )
        return fallback_idx

    def stop(self) -> Optional[np.ndarray]:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None

        with self._lock:
            if not self._buffers:
                return None
            data = np.concatenate(self._buffers, axis=0)
            self._buffers = []

        # Downmix to mono if multi-channel.
        if data.ndim == 2 and data.shape[1] > 1:
            data = np.mean(data, axis=1).astype(np.float32, copy=False)
        elif data.ndim == 2:
            data = data[:, 0].astype(np.float32, copy=False)
        else:
            data = data.astype(np.float32, copy=False)

        # Resample to 16 kHz using polyphase filtering.
        if self._native_sr != TARGET_SR:
            from math import gcd

            g = gcd(self._native_sr, TARGET_SR)
            up = TARGET_SR // g
            down = self._native_sr // g
            data = signal.resample_poly(data, up, down).astype(
                np.float32, copy=False
            )

        return data

    def current_rms_levels(self, n: int = 5) -> List[float]:
        """Return RMS levels (0..1, oldest first) for the most recent n
        audio chunks. Safe to call from any thread."""
        with self._lock:
            bufs = list(self._buffers[-n:])
        out: List[float] = [0.0] * (n - len(bufs))
        for b in bufs:
            if b.size == 0:
                out.append(0.0)
                continue
            mono = b if b.ndim == 1 else np.mean(b, axis=1)
            rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float64)))))
            out.append(min(1.0, rms * 6.0))
        return out

    # ------------------------------------------------------------------
    # Internals

    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            # Non-fatal overflow/underflow. We don't surface; the user can
            # see device problems via the silence / disconnect fallback.
            pass
        chunk = np.array(indata, copy=True)
        with self._lock:
            self._buffers.append(chunk)

        # Silence detection — cheap peak check per chunk.
        if indata.size > 0:
            try:
                peak = float(np.max(np.abs(indata)))
            except (ValueError, TypeError):
                peak = 0.0
            if peak < SILENCE_PEAK_THRESHOLD:
                if self._silence_started_at is None:
                    self._silence_started_at = time.monotonic()
                elif (
                    self._silence_callback is not None
                    and time.monotonic() - self._silence_started_at
                    >= SILENCE_DURATION_S
                ):
                    cb = self._silence_callback
                    self._silence_callback = None  # one-shot
                    self._silence_started_at = None
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        log.exception("Silence callback raised.")
            else:
                self._silence_started_at = None

        # Fan-out to the preview consumer.
        sink = self._preview_sink
        if sink is not None:
            try:
                sink(chunk)
            except Exception:  # noqa: BLE001
                log.exception("Preview sink raised; disabling.")
                self._preview_sink = None

    # ------------------------------------------------------------------
    # Stream open / fallback

    def _resolve_device(
        self,
        index: Optional[int],
        name_substring: Optional[str],
    ) -> Tuple[int, dict]:
        """Resolve a (possibly locked) device index + info dict."""
        devices = sd.query_devices()
        if index is not None and 0 <= index < len(devices):
            d = devices[index]
            if d.get("max_input_channels", 0) > 0:
                return index, d
            log.warning(
                "Locked device #%s is not an input; falling through.",
                index,
            )
        if name_substring:
            needle = name_substring.lower()
            for i, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    name = d.get("name", "").lower()
                    if needle in name:
                        return i, d
            log.warning(
                "No input device matched name %r; falling through.",
                name_substring,
            )
        # Default input.
        default = sd.query_devices(kind="input")
        return default.get("index", 0), default

    def _open_stream(
        self,
        index: Optional[int],
        name_substring: Optional[str],
    ) -> Tuple[int, int]:
        idx, dev = self._resolve_device(index, name_substring)
        native_sr = int(dev.get("default_samplerate", TARGET_SR))
        max_in = int(dev.get("max_input_channels", 1))
        channels = max(1, 2 if max_in >= 2 else max_in)

        self._native_sr = native_sr
        self._native_channels = channels
        self._device_index = idx
        self._device_name = dev.get("name", "")

        self._stream = sd.InputStream(
            device=idx,
            samplerate=native_sr,
            channels=channels,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self.start_time = time.monotonic()
        log.info(
            "Opened input #%s: %s (%d Hz, %d ch)",
            idx, self._device_name, native_sr, channels,
        )
        return native_sr, channels

    def _open_with_fallback(self, original_error: Exception) -> Tuple[int, int]:
        """Re-initialize PortAudio and try built-in / first-available
        input devices in priority order."""
        # Tear down any half-open state.
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

        # Re-initialize PortAudio so we pick up newly-connected devices.
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:  # noqa: BLE001
            log.warning("PortAudio re-init failed: %s", e)

        devices = sd.query_devices()
        inputs = [
            (i, d) for i, d in enumerate(devices)
            if d.get("max_input_channels", 0) > 0
        ]
        if not inputs:
            raise RuntimeError(
                f"No input devices available after fallback (was: {original_error})"
            ) from original_error

        # Priority: built-in > default input > first available.
        priority_order: List[Tuple[int, dict]] = []
        seen = set()

        def push(idx: int, d: dict) -> None:
            if idx not in seen:
                priority_order.append((idx, d))
                seen.add(idx)

        for idx, d in inputs:
            name = d.get("name", "").lower()
            if any(tag in name for tag in _PreferredBuiltinNames):
                push(idx, d)
        # Default input
        try:
            default = sd.query_devices(kind="input")
            push(default.get("index", 0), default)
        except Exception:  # noqa: BLE001
            pass
        # Anything else
        for idx, d in inputs:
            push(idx, d)

        for idx, d in priority_order:
            try:
                return self._open_stream(idx, None)
            except sd.PortAudioError as e:
                log.warning("Fallback device #%s (%s) failed: %s", idx, d.get("name"), e)
                continue

        raise RuntimeError(
            f"All fallback devices failed (last: {original_error})"
        ) from original_error


AudioRecorder = Recorder
