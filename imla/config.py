"""Configuration loader for Imla.

Config file lives at ``~/.config/imla/config.json`` (XDG-style, same
path on macOS, Linux, and Windows — Windows tolerates ``~/.config``).

The Groq API key is *never* stored here — it lives in the OS keychain via
``keyring``. ``config.json`` only holds a boolean ``has_keyring_key`` flag
to indicate whether a key is present.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger("imla.config")

CONFIG_DIR = Path.home() / ".config" / "imla"
CONFIG_PATH = CONFIG_DIR / "config.json"


def default_config() -> Dict[str, Any]:
    """Return the default configuration as a fresh dict."""
    return {
        "hotkey": "Key.alt_r",
        "pre_roll_ms": 0,
        "tail_ms": 250,              # keep recording briefly after key release
        "input_device": None,        # None = auto; else int index or name substring
        "overlay_enabled": True,
        "start_on_login": False,
        "transcription": {
            # auto = Groq cloud Whisper first, local faster-whisper fallback
            # cloud = cloud only | local = never send audio off the machine
            "engine": "auto",
            "cloud_model": "whisper-large-v3-turbo",
            # Local (fallback) model:
            # tiny | base | small | small.en | medium | distil-large-v3 ...
            "model": "small.en",
            "preview_enabled": True,
            "beam_size": 3,          # 1 = greedy (fast, least accurate)
            "cpu_threads": 0,        # 0 = auto
        },
        "format": {
            "tier": "auto",          # auto | groq | ollama | rule
            "groq_model": "openai/gpt-oss-20b",
            "ollama_model": "llama3.2:3b",
            "fix_grammar": True,     # minimal-edit grammar correction (LLM tiers)
            # Tell the cleanup LLM the speaker is a (Java/Spring Boot) engineer.
            "developer_focus": True,
        },
        # Built-in developer vocabulary packs (see imla/vocab/). The user's own
        # vocabulary and dictionary always take priority over these.
        "vocab_packs": {"java_spring": True, "software_general": True},
        # Terms that need correct spelling but no find/replace pair. They are
        # fed to Whisper and to the cleanup LLM as spelling hints.
        "vocabulary": [],
        "dictionary": [
            ["kubernetes", "Kubernetes"],
            ["pie torch", "PyTorch"],
            ["pie test", "pytest"],
            ["next js", "Next.js"],
            ["node js", "Node.js"],
            ["react js", "React"],
            ["grpc", "gRPC"],
            ["g rpc", "gRPC"],
            ["get hub", "GitHub"],
            ["o auth", "OAuth"],
            ["docker compose", "Docker Compose"],
            ["type script", "TypeScript"],
            ["java script", "JavaScript"],
            ["postgres sql", "PostgreSQL"],
        ],
        "stats": {
            "total_dictations": 0,
            "total_words": 0,
        },
    }


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Load the config file, returning defaults if missing or invalid."""
    cfg = default_config()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                on_disk = json.load(f)
            # Merge: defaults first, then on-disk overrides.
            _deep_merge(cfg, on_disk)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Config at %s unreadable (%s); using defaults.", path, e)
    return cfg


def save_config(cfg: Dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Write the config to disk atomically. API keys are strictly excluded."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Defense-in-depth: ensure API keys are never written to config.json
    clean_cfg = copy.deepcopy(cfg)
    clean_cfg.pop("api_key", None)
    clean_cfg.pop("groq_api_key", None)
    if "format" in clean_cfg and isinstance(clean_cfg["format"], dict):
        clean_cfg["format"].pop("api_key", None)
        clean_cfg["format"].pop("groq_api_key", None)
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(clean_cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        log.error("Failed to write config: %s", e)
        raise


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """In-place merge of ``overlay`` into ``base`` (dict-of-dicts merged)."""
    for k, v in overlay.items():
        if (
            k in base
            and isinstance(base[k], dict)
            and isinstance(v, dict)
        ):
            _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


def get_vocabulary(cfg: Dict[str, Any]) -> List[str]:
    """Return spelling-hint terms, highest priority first: the user's own
    ``vocabulary``, the Java/Spring pack, the user's dictionary targets, then
    the other enabled packs. De-duplicated, in order."""
    from . import vocabpacks

    terms: List[str] = []
    seen = set()
    candidates = [t for t in cfg.get("vocabulary", []) if isinstance(t, str)]
    packs = vocabpacks.enabled_packs(cfg)
    for name in packs[:1]:
        candidates += list(vocabpacks.pack_terms(name))
    candidates += [replace for _, replace in get_dictionary(cfg)]
    for name in packs[1:]:
        candidates += list(vocabpacks.pack_terms(name))
    for term in candidates:
        term = term.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def get_effective_dictionary(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """User dictionary first, then the enabled built-in pack replacements.

    Use this for dictation; use :func:`get_dictionary` for the editable list
    shown in Settings (so pack entries are never copied into config.json)."""
    from . import vocabpacks

    user = get_dictionary(cfg)
    seen = {find.lower() for find, _ in user}
    extra = [(f, r) for f, r in vocabpacks.enabled_replacements(cfg)
             if f.lower() not in seen]
    return user + extra


def get_dictionary(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return the user's dictionary as a list of ``(find, replace)`` tuples.

    Invalid entries are silently dropped."""
    out: List[Tuple[str, str]] = []
    for entry in cfg.get("dictionary", []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            find, replace = entry
            if isinstance(find, str) and isinstance(replace, str) and find:
                out.append((find, replace))
    return out
