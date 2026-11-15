"""Local speech-to-text via faster-whisper.

vad_filter is explicitly disabled because Silero VAD prematurely drops
quiet mic signals and wireless-audio streams.

The model name is loaded lazily on first use and can be swapped at
runtime via :func:`set_model_name`. The next call to :func:`transcribe`
will instantiate the new model.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("imla.transcribe")

_model: Optional[WhisperModel] = None
_model_name: str = "base"
_lock = threading.Lock()


def set_model_name(name: str) -> None:
    """Change the model used for the next transcription.

    The new model loads lazily on the next :func:`transcribe` call."""
    global _model_name, _model
    if name and name != _model_name:
        with _lock:
            _model_name = name
            _model = None
        log.info("Whisper model will switch to %r on next dictation.", name)


def get_model_name() -> str:
    return _model_name


def get_model() -> WhisperModel:
    """Lazily load and cache the current whisper model."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                log.info(
                    "Loading faster-whisper model (%s, cpu, int8)...",
                    _model_name,
                )
                _model = WhisperModel(
                    _model_name,
                    device="cpu",
                    compute_type="int8",
                )
                log.info("Model loaded.")
    return _model


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a 16 kHz mono float32 audio buffer. Returns stripped text."""
    if audio is None or len(audio) == 0:
        return ""
    model = get_model()
    segments, info = model.transcribe(
        audio,
        language="en",
        beam_size=1,
        vad_filter=False,
    )
    parts = [seg.text for seg in segments]
    text = " ".join(parts).strip()
    log.debug(
        "transcribe: lang=%s prob=%.2f text=%r",
        info.language, info.language_probability, text,
    )
    return text
