"""Daily API usage counters (Groq free-tier budgeting).

Persisted as JSON at ``~/.config/imla/usage.json``. The day boundary is the
UTC date (matching Groq's daily quota reset). Every function swallows I/O
errors: usage tracking must never break a dictation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("imla.usage")

USAGE_PATH = Path.home() / ".config" / "imla" / "usage.json"

DAILY_TOKEN_LIMIT = 200_000
DAILY_AUDIO_SECONDS_LIMIT = 28_800
DAILY_AUDIO_REQUEST_LIMIT = 2_000
DAILY_CHAT_REQUEST_LIMIT = 1_000

_WARN_RATIO = 0.8
_lock = threading.Lock()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _empty(date: str) -> dict:
    return {
        "date": date,
        "tokens": {},
        "requests": {},
        "audio_seconds": 0.0,
        "audio_requests": 0,
    }


def _load() -> dict:
    """Load today's counters (fresh if file missing/corrupt/stale)."""
    today = _today_str()
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or raw.get("date") != today:
            return _empty(today)
        data = _empty(today)
        data["tokens"] = {
            str(k): int(v) for k, v in (raw.get("tokens") or {}).items()
        }
        data["requests"] = {
            str(k): int(v) for k, v in (raw.get("requests") or {}).items()
        }
        data["audio_seconds"] = float(raw.get("audio_seconds", 0.0))
        data["audio_requests"] = int(raw.get("audio_requests", 0))
        return data
    except FileNotFoundError:
        return _empty(today)
    except Exception as e:  # noqa: BLE001
        log.debug("usage load failed: %s", e)
        return _empty(today)


def _save(data: dict) -> None:
    path = Path(USAGE_PATH)
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def record_tokens(model: str, tokens: int) -> None:
    try:
        with _lock:
            data = _load()
            data["tokens"][model] = data["tokens"].get(model, 0) + int(tokens)
            data["requests"][model] = data["requests"].get(model, 0) + 1
            _save(data)
    except Exception as e:  # noqa: BLE001
        log.debug("record_tokens failed: %s", e)


def record_audio_seconds(seconds: float) -> None:
    try:
        with _lock:
            data = _load()
            data["audio_seconds"] = data["audio_seconds"] + float(seconds)
            data["audio_requests"] += 1
            _save(data)
    except Exception as e:  # noqa: BLE001
        log.debug("record_audio_seconds failed: %s", e)


def get_today() -> dict:
    try:
        with _lock:
            return _load()
    except Exception as e:  # noqa: BLE001
        log.debug("get_today failed: %s", e)
        return _empty(_today_str())


def summary_lines() -> list[str]:
    """Human-readable usage lines for the Settings panel."""
    d = get_today()
    lines: list[str] = []
    warnings: list[str] = []

    def pct(used: float, limit: float) -> int:
        return int(used * 100 / limit) if limit else 0

    for model in sorted(d["tokens"]):
        used = d["tokens"][model]
        p = pct(used, DAILY_TOKEN_LIMIT)
        lines.append(
            f"{model}: {used:,} / {DAILY_TOKEN_LIMIT:,} tokens ({p}%)"
        )
        if used >= _WARN_RATIO * DAILY_TOKEN_LIMIT:
            warnings.append(f"⚠ {model} is at {p}% of its daily token limit")
        reqs = d["requests"].get(model, 0)
        if reqs >= _WARN_RATIO * DAILY_CHAT_REQUEST_LIMIT:
            warnings.append(
                f"⚠ {model} is at {pct(reqs, DAILY_CHAT_REQUEST_LIMIT)}% of "
                "its daily request limit"
            )

    secs = d["audio_seconds"]
    if secs > 0 or d["audio_requests"] > 0:
        p = pct(secs, DAILY_AUDIO_SECONDS_LIMIT)
        lines.append(
            f"Cloud Whisper: {int(secs):,} s / "
            f"{DAILY_AUDIO_SECONDS_LIMIT:,} s ({p}%)"
        )
        if secs >= _WARN_RATIO * DAILY_AUDIO_SECONDS_LIMIT:
            warnings.append(f"⚠ Cloud Whisper is at {p}% of its daily audio limit")
        if d["audio_requests"] >= _WARN_RATIO * DAILY_AUDIO_REQUEST_LIMIT:
            warnings.append(
                "⚠ Cloud Whisper is at "
                f"{pct(d['audio_requests'], DAILY_AUDIO_REQUEST_LIMIT)}% of "
                "its daily request limit"
            )

    if not lines:
        lines.append("No cloud usage recorded today.")
    return lines + warnings
