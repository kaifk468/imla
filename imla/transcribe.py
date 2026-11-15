"""Local speech-to-text via faster-whisper.

vad_filter is explicitly disabled because Silero VAD prematurely drops
quiet mic signals and wireless-audio streams.

The model name is loaded lazily on first use and can be swapped at
runtime via :func:`set_model_name`. The next call to :func:`transcribe`
will instantiate the new model.

Accuracy knobs
--------------
* ``beam_size`` — wider beam search is more accurate than greedy decoding
  (``1``) at a modest CPU cost. Configurable via :func:`set_decode_options`.
* ``vocabulary`` — technical terms are passed to Whisper as an
  ``initial_prompt`` so it spells them correctly instead of guessing
  ("PyTorch", not "pie torch").
* ``cpu_threads`` — explicit thread count so CTranslate2 uses the machine's
  cores instead of its small default.
"""

from __future__ import annotations

import logging
import os
import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import httpx
import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("imla.transcribe")

DEFAULT_MODEL = "small.en"
DEFAULT_BEAM_SIZE = 3
# Whisper's prompt window is ~224 tokens; keep the vocabulary hint well under it.
MAX_VOCAB_TERMS = 60
# ...and by characters too: code identifiers/annotations cost many tokens each.
# Whisper keeps only the LAST ~223 prompt tokens, so stay safely below that.
MAX_VOCAB_CHARS = 420

_model: Optional[WhisperModel] = None
_model_name: str = DEFAULT_MODEL
_beam_size: int = DEFAULT_BEAM_SIZE
_cpu_threads: int = 0  # 0 = auto (see _resolve_threads)
_lock = threading.Lock()


def _resolve_threads() -> int:
    if _cpu_threads > 0:
        return _cpu_threads
    # CTranslate2's own default is only 4; use more of the machine, but cap
    # it so a many-core desktop doesn't thrash.
    return min(8, os.cpu_count() or 4)


def set_model_name(name: str) -> None:
    """Change the model used for the next transcription.

    The new model loads lazily on the next :func:`transcribe` call."""
    global _model_name, _model
    if name and name != _model_name:
        with _lock:
            _model_name = name
            _model = None
        log.info("Whisper model will switch to %r on next dictation.", name)


def set_decode_options(
    beam_size: Optional[int] = None,
    cpu_threads: Optional[int] = None,
) -> None:
    """Update decoding options. A thread-count change reloads the model."""
    global _beam_size, _cpu_threads, _model
    if beam_size is not None and beam_size >= 1:
        _beam_size = int(beam_size)
    if cpu_threads is not None and cpu_threads >= 0 and cpu_threads != _cpu_threads:
        with _lock:
            _cpu_threads = int(cpu_threads)
            _model = None


def get_model_name() -> str:
    return _model_name


def build_initial_prompt(vocab: Optional[Iterable[str]]) -> Optional[str]:
    """Turn a vocabulary list into a Whisper ``initial_prompt`` (or ``None``)."""
    if not vocab:
        return None
    seen = set()
    terms: List[str] = []
    for term in vocab:
        term = (term or "").strip()
        key = term.lower()
        if term and key not in seen:
            if sum(len(t) + 2 for t in terms) + len(term) > MAX_VOCAB_CHARS:
                break
            seen.add(key)
            terms.append(term)
        if len(terms) >= MAX_VOCAB_TERMS:
            break
    if not terms:
        return None
    return "Technical terms: " + ", ".join(terms) + "."


def get_model() -> WhisperModel:
    """Lazily load and cache the current whisper model."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                threads = _resolve_threads()
                log.info(
                    "Loading faster-whisper model (%s, cpu, int8, %d threads, "
                    "beam %d)...",
                    _model_name, threads, _beam_size,
                )
                _model = WhisperModel(
                    _model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=threads,
                )
                log.info("Model loaded.")
    return _model


def preload_async() -> None:
    """Load the current model in a background thread.

    Without this the model loads (and may download) during the first
    dictation after launch or after a model switch, adding seconds of
    latency right when the user is waiting for text."""

    def _load() -> None:
        try:
            get_model()
        except Exception:  # noqa: BLE001
            log.exception("Whisper preload failed; will retry on first use.")

    threading.Thread(target=_load, daemon=True, name="imla-whisper-preload").start()


def _transcribe_local(audio: np.ndarray, vocab: Optional[Iterable[str]] = None) -> str:
    """Decode with the local faster-whisper model."""
    model = get_model()
    segments, info = model.transcribe(
        audio,
        language="en",
        beam_size=_beam_size,
        vad_filter=False,
        # Each utterance is independent; carrying text across segments makes
        # Whisper repeat itself or drop words.
        condition_on_previous_text=False,
        initial_prompt=build_initial_prompt(vocab),
    )
    parts = [seg.text for seg in segments]
    text = " ".join(parts).strip()
    log.debug(
        "transcribe: lang=%s prob=%.2f text=%r",
        info.language, info.language_probability, text,
    )
    return text


# ---------------------------------------------------------------------------
# Cloud (Groq) Whisper with automatic local fallback
# ---------------------------------------------------------------------------

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_CLOUD_MODEL = "whisper-large-v3-turbo"
SAMPLE_RATE = 16000
MIN_CLOUD_SECONDS = 0.4
SILENCE_PEAK = 1e-4
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_S = 300.0
DEFAULT_RETRY_AFTER_S = 60.0
MAX_RETRY_AFTER_S = 3600.0

_VALID_ENGINES = ("auto", "cloud", "local")
_engine: str = "local"
_cloud_model: str = DEFAULT_CLOUD_MODEL

_breaker_lock = threading.Lock()
_consecutive_failures = 0
_breaker_until = 0.0  # time.monotonic() deadline; 0 = closed


class CloudSTTError(Exception):
    """Cloud transcription failed; the caller should fall back to local."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.reason = reason or message


@dataclass
class TranscribeResult:
    text: str
    engine: str  # "groq" | "local"
    latency_s: float
    fallback_reason: Optional[str] = None


def set_engine(engine: str = "auto", cloud_model: Optional[str] = None) -> None:
    """Choose "auto" (cloud, local fallback), "cloud" or "local" (never send audio)."""
    global _engine, _cloud_model
    _engine = engine if engine in _VALID_ENGINES else "auto"
    if cloud_model:
        _cloud_model = cloud_model


def get_engine() -> str:
    return _engine


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_until


def reset_cloud_breaker() -> None:
    global _consecutive_failures, _breaker_until
    with _breaker_lock:
        _consecutive_failures = 0
        _breaker_until = 0.0


def _record_cloud_success() -> None:
    global _consecutive_failures
    with _breaker_lock:
        _consecutive_failures = 0


def _record_cloud_failure(err: CloudSTTError) -> None:
    global _consecutive_failures, _breaker_until
    with _breaker_lock:
        if err.status_code == 429:
            wait = err.retry_after if err.retry_after else DEFAULT_RETRY_AFTER_S
            wait = max(0.0, min(float(wait), MAX_RETRY_AFTER_S))
            _breaker_until = time.monotonic() + wait
            return
        _consecutive_failures += 1
        if _consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            _breaker_until = time.monotonic() + BREAKER_COOLDOWN_S
            _consecutive_failures = 0


def encode_wav(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """float32 mono in [-1, 1] -> 16-bit PCM WAV bytes."""
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def transcribe_groq(
    audio: np.ndarray,
    vocab: Optional[Iterable[str]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> str:
    """Transcribe via Groq's Whisper endpoint. Raises :class:`CloudSTTError`."""
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        try:
            from .keychain import get_api_key

            key = get_api_key()
        except Exception:  # noqa: BLE001
            key = None
    if not key:
        raise CloudSTTError("no API key", reason="no API key")

    clip_s = len(audio) / float(SAMPLE_RATE)
    total = timeout_s if timeout_s is not None else min(20.0, 6.0 + 0.2 * clip_s)
    data = {
        "model": model or _cloud_model,
        "language": "en",
        "temperature": "0",
        "response_format": "json",
    }
    prompt = build_initial_prompt(vocab)
    if prompt is not None:
        data["prompt"] = prompt
    wav = encode_wav(audio, SAMPLE_RATE)

    try:
        with httpx.Client(timeout=httpx.Timeout(total, connect=3.0)) as client:
            resp = client.post(
                GROQ_STT_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data=data,
            )
    except httpx.TimeoutException as e:
        raise CloudSTTError(f"timeout: {type(e).__name__}", reason="timeout") from e
    except httpx.TransportError as e:
        raise CloudSTTError(f"offline: {type(e).__name__}", reason="offline") from e
    except httpx.HTTPError as e:
        raise CloudSTTError(f"http error: {type(e).__name__}", reason="offline") from e

    status = resp.status_code
    if status >= 400:
        retry_after = None
        if status == 429:
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        raise CloudSTTError(
            f"http {status}", status_code=status, retry_after=retry_after,
            reason=f"http {status}",
        )
    try:
        text = resp.json()["text"]
        if not isinstance(text, str):
            raise TypeError("text is not a string")
    except Exception as e:  # noqa: BLE001
        raise CloudSTTError("malformed response", reason="malformed response") from e
    return text.strip()


def _record_usage(clip_seconds: float) -> None:
    try:
        from .usage import record_audio_seconds

        record_audio_seconds(max(10.0, clip_seconds))
    except Exception:  # noqa: BLE001
        pass


def transcribe_detailed(
    audio: np.ndarray,
    vocab: Optional[Iterable[str]] = None,
    api_key: Optional[str] = None,
) -> TranscribeResult:
    """Transcribe with the configured engine, falling back to local on any cloud failure."""
    if audio is None or len(audio) == 0:
        return TranscribeResult("", "local", 0.0)
    t0 = time.monotonic()
    clip_s = len(audio) / float(SAMPLE_RATE)
    reason: Optional[str] = None

    if _engine != "local":
        peak = float(np.max(np.abs(audio)))
        if _breaker_open():
            reason = "cloud paused (circuit breaker)"
        elif clip_s < MIN_CLOUD_SECONDS:
            reason = "clip too short for cloud"
        elif peak < SILENCE_PEAK:
            reason = "silent clip"
        else:
            try:
                text = transcribe_groq(audio, vocab, api_key=api_key)
            except CloudSTTError as e:
                _record_cloud_failure(e)
                reason = e.reason
                log.warning("Cloud STT failed (%s); falling back to local.", reason)
            else:
                _record_cloud_success()
                if text:
                    _record_usage(clip_s)
                    lat = time.monotonic() - t0
                    log.info("STT engine=groq latency=%.2fs", lat)
                    return TranscribeResult(text, "groq", lat)
                reason = "empty cloud result"
                log.warning("Cloud STT returned empty text; falling back to local.")

    text = _transcribe_local(audio, vocab)
    lat = time.monotonic() - t0
    log.info("STT engine=local latency=%.2fs fallback=%s", lat, reason)
    return TranscribeResult(text, "local", lat, reason)


def transcribe(audio: np.ndarray, vocab: Optional[Iterable[str]] = None) -> str:
    """Transcribe a 16 kHz mono float32 audio buffer. Returns stripped text."""
    return transcribe_detailed(audio, vocab).text


def test_cloud(api_key: Optional[str] = None) -> Tuple[bool, str, float]:
    """Send 1 s of faint noise to the cloud model. Never raises."""
    t0 = time.monotonic()
    try:
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(SAMPLE_RATE) * 0.01).astype(np.float32)
        transcribe_groq(noise, None, api_key=api_key)
        lat = time.monotonic() - t0
        return True, f"Cloud OK ({_cloud_model}, {lat:.2f}s)", lat
    except CloudSTTError as e:
        lat = time.monotonic() - t0
        reason = e.reason
        if e.status_code == 401:
            reason = "http 401 (invalid API key)"
        return False, f"Cloud test failed: {reason}", lat
    except Exception as e:  # noqa: BLE001
        lat = time.monotonic() - t0
        return False, f"Cloud test failed: {type(e).__name__}", lat


test_cloud.__test__ = False  # not a pytest test
