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
        "input_device": None,        # None = auto; else int index or name substring
        "overlay_enabled": True,
        "start_on_login": False,
        "transcription": {
            "model": "base",         # tiny | base | small | medium
            "preview_enabled": True,
        },
        "format": {
            "tier": "auto",          # auto | groq | ollama | rule
            "groq_model": "llama-3.3-70b-versatile",
            "ollama_model": "llama3.2:3b",
        },
        "dictionary": [
            ["kubernetes", "K8s"],
            ["pie torch", "PyTorch"],
            ["next js", "Next.js"],
            ["grpc", "gRPC"],
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


def get_dictionary(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return the dictionary as a list of ``(find, replace)`` tuples.

    Invalid entries are silently dropped."""
    out: List[Tuple[str, str]] = []
    for entry in cfg.get("dictionary", []):
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            find, replace = entry
            if isinstance(find, str) and isinstance(replace, str) and find:
                out.append((find, replace))
    return out
