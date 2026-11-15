"""Built-in developer vocabulary packs (Java / Spring Boot first).

Each pack is two plain-text files in ``imla/vocab/``:

* ``<pack>.txt``                 spelling hints, one per line, most important first
* ``replacements_<pack>.txt``    ``spoken => Written`` pairs for the dictionary

Lines starting with ``#`` are comments. Packs are switched on/off through the
``vocab_packs`` config section; the user's own vocabulary and dictionary always
take priority over pack content.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

log = logging.getLogger("imla.vocabpacks")

VOCAB_DIR = Path(__file__).resolve().parent / "vocab"

# Priority order: Java/Spring first, then general software.
PACK_ORDER = ["java_spring", "software_general"]
# replacements_* file for each pack
_REPLACEMENT_FILES = {
    "java_spring": "replacements_java_spring.txt",
    "software_general": "replacements_general.txt",
}
PACK_LABELS = {
    "java_spring": "Java & Spring Boot",
    "software_general": "General software engineering",
}


def default_pack_flags() -> Dict[str, bool]:
    return {name: True for name in PACK_ORDER}


def _read_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Vocabulary pack file %s unreadable: %s", path, e)
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


@lru_cache(maxsize=None)
def pack_terms(name: str) -> Tuple[str, ...]:
    """Spelling-hint terms of one pack, in file order, de-duplicated."""
    seen = set()
    out: List[str] = []
    for term in _read_lines(VOCAB_DIR / f"{name}.txt"):
        if term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return tuple(out)


@lru_cache(maxsize=None)
def pack_replacements(name: str) -> Tuple[Tuple[str, str], ...]:
    """``(spoken, written)`` pairs of one pack, longest spoken form first so
    a specific phrase ("at spring boot test") wins over a general one."""
    fname = _REPLACEMENT_FILES.get(name)
    if not fname:
        return ()
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for line in _read_lines(VOCAB_DIR / fname):
        if "=>" not in line:
            continue
        spoken, written = (p.strip() for p in line.split("=>", 1))
        if spoken and written and "\\" not in written and spoken.lower() not in seen:
            seen.add(spoken.lower())
            pairs.append((spoken, written))
    pairs.sort(key=lambda p: -len(p[0]))
    return tuple(pairs)


def enabled_packs(cfg: dict) -> List[str]:
    flags = cfg.get("vocab_packs") if isinstance(cfg, dict) else None
    if not isinstance(flags, dict):
        flags = {}
    return [n for n in PACK_ORDER if flags.get(n, True)]


def enabled_terms(cfg: dict) -> List[str]:
    out: List[str] = []
    for name in enabled_packs(cfg):
        out.extend(pack_terms(name))
    return out


def enabled_replacements(cfg: dict) -> List[Tuple[str, str]]:
    """Replacements of all enabled packs; Java pack entries win ties."""
    out: List[Tuple[str, str]] = []
    seen = set()
    merged: List[Tuple[str, str]] = []
    for name in enabled_packs(cfg):
        merged.extend(pack_replacements(name))
    merged.sort(key=lambda p: -len(p[0]))  # stable: earlier pack first on ties
    for spoken, written in merged:
        if spoken.lower() not in seen:
            seen.add(spoken.lower())
            out.append((spoken, written))
    return out
