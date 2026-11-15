"""OS keychain wrapper for the Groq API key.

Stores the key in the platform-native secure store via ``keyring``:
* macOS — Keychain
* Windows — Credential Manager
* Linux — Secret Service (KWallet, GNOME Keyring, etc.)

The key is NEVER written to ``config.json``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
import keyring
import keyring.errors

load_dotenv()  # default search (cwd / script dir upwards)


def _load_extra_env_files() -> None:
    """Also load .env from the project root and ~/.config/imla (no override)."""
    from pathlib import Path

    candidates = []
    try:
        from .launcher import project_root

        candidates.append(project_root() / ".env")
    except Exception:  # noqa: BLE001
        pass
    candidates.append(Path.home() / ".config" / "imla" / ".env")
    for p in candidates:
        try:
            if p.is_file():
                load_dotenv(p, override=False)
        except Exception:  # noqa: BLE001
            pass


_load_extra_env_files()

log = logging.getLogger("imla.keychain")

SERVICE_NAME = "Imla"
KEY_NAME = "groq_api_key"


def _get_keyring_key() -> Optional[str]:
    """Key from the OS keychain only (no env fallback); None on any failure."""
    try:
        key = keyring.get_password(SERVICE_NAME, KEY_NAME)
        return key if key else None
    except Exception as e:  # noqa: BLE001
        log.warning("Keyring unavailable: %s", type(e).__name__)
        return None


def import_env_key_if_missing() -> bool:
    """Copy GROQ_API_KEY from the environment (.env) into the keychain.

    Returns True only if a key was newly stored. Never raises, never logs
    the key.
    """
    try:
        if _get_keyring_key():
            return False
        env_key = (os.environ.get("GROQ_API_KEY") or "").strip()
        if not env_key:
            return False
        ok = set_api_key(env_key)
        if ok:
            log.info("Imported GROQ_API_KEY from environment into keychain")
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        log.warning("Key import failed: %s", type(e).__name__)
        return False


def get_api_key() -> Optional[str]:
    """Return the stored Groq API key, or ``None`` if not set.

    Checks the OS keychain first. If not found in keychain, falls back to
    the GROQ_API_KEY environment variable (e.g. from .env).
    """
    try:
        key = keyring.get_password(SERVICE_NAME, KEY_NAME)
        if key:
            return key
    except keyring.errors.KeyringError as e:
        log.warning("Keyring unavailable: %s", e)

    env_key = os.environ.get("GROQ_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return None


def set_api_key(key: Optional[str]) -> bool:
    """Store or clear the Groq API key. Returns True on success."""
    if not key:
        try:
            keyring.delete_password(SERVICE_NAME, KEY_NAME)
        except keyring.errors.PasswordDeleteError:
            pass
        except keyring.errors.KeyringError as e:
            log.warning("Keyring delete failed: %s", e)
            return False
        return True
    try:
        keyring.set_password(SERVICE_NAME, KEY_NAME, key)
        return True
    except keyring.errors.KeyringError as e:
        log.warning("Keyring set failed: %s", e)
        return False


def test_connection(api_key: str) -> tuple:
    """Verify the key by issuing a tiny Groq API call. Returns (ok, message)."""
    if not api_key:
        return False, "No API key provided."
    try:
        import httpx
    except ImportError:
        return False, "httpx not installed."
    try:
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=4.0,
        )
        if r.status_code == 200:
            return True, "Connection OK."
        if r.status_code == 401:
            return False, "Invalid API key (401 Unauthorized)."
        return False, f"Groq returned HTTP {r.status_code}."
    except httpx.HTTPError as e:
        return False, f"Network error: {e}"
