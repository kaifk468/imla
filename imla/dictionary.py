"""Personal replacement dictionary — case-insensitive, word-boundary aware.

Applied AFTER the formatting pipeline so it can correct both raw Whisper
output and the cleanup tier's output. The dictionary lives in
``config.json`` and is editable from the Settings UI.

Replacements are applied in order. Each pair uses a word-boundary regex
(so ``"k8s"`` won't match inside ``"kubernetes"``), and matches are
case-insensitive — the replacement string is used verbatim regardless of
the input casing.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Tuple

log = logging.getLogger("imla.dictionary")

_Pattern = Tuple[re.Pattern, str]


def compile_dictionary(pairs: Iterable[Tuple[str, str]]) -> list:
    """Pre-compile regex patterns for each (find, replace) pair."""
    out = []
    for find, replace in pairs:
        if not find:
            continue
        try:
            pattern = re.compile(r"\b" + re.escape(find) + r"\b", re.IGNORECASE)
        except re.error as e:
            log.warning("Skipping invalid dictionary entry %r: %s", find, e)
            continue
        out.append((pattern, replace))
    return out


def apply_dictionary(text: str, compiled: list) -> str:
    """Apply all compiled replacements in order."""
    for pattern, replacement in compiled:
        text = pattern.sub(replacement, text)
    return text
