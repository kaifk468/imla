"""Tkinter Settings Dashboard for Imla.

Five tabs: General, Transcription, Cleanup, Vocabulary, Dictionary.
Footer: stats, dirty badge, Cancel, Save.

Dirty tracking
--------------
On open we snapshot the working config + keychain state. Save is enabled
only when at least one field has changed. Save writes config.json +
keyring, then triggers a live apply via the host callback.

Close guard
-----------
If the user tries to close (X button, Escape) while dirty, a modal asks:
Save / Discard / Cancel.
"""

from __future__ import annotations

import copy
import logging
import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv()

# Ensure Tcl/Tk libraries are discovered in portable / virtualenv Python environments
for tcl_sub in ("tcl8.6", "tcl8.5", "tcl"):
    cand = os.path.join(sys.base_prefix, "lib", tcl_sub)
    if os.path.isdir(cand) and "TCL_LIBRARY" not in os.environ:
        os.environ["TCL_LIBRARY"] = cand
        break

for tk_sub in ("tk8.6", "tk8.5", "tk"):
    cand = os.path.join(sys.base_prefix, "lib", tk_sub)
    if os.path.isdir(cand) and "TK_LIBRARY" not in os.environ:
        os.environ["TK_LIBRARY"] = cand
        break

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

import sounddevice as sd

from .config import (
    CONFIG_PATH,
    default_config,
    get_dictionary,
    load_config,
    save_config,
)
from .keychain import get_api_key, set_api_key, test_connection as groq_test

log = logging.getLogger("imla.settings")

DEFAULT_SIZE = "640x740"
MIN_SIZE = "560x640"

HOTKEY_CHOICES = [
    ("Right Alt / Option", "Key.alt_r"),
    ("Left Alt / Option", "Key.alt_l"),
    ("Right Control", "Key.ctrl_r"),
    ("Left Control", "Key.ctrl_l"),
    ("Right Command", "Key.cmd_r"),
    ("Right Shift", "Key.shift_r"),
    ("F8", "Key.f8"),
    ("F1", "Key.f1"),
    ("F2", "Key.f2"),
]

WHISPER_MODELS = [
    "tiny", "base", "small", "small.en", "medium", "distil-large-v3",
]
TIER_CHOICES = ["auto", "groq", "ollama", "rule"]
ENGINE_CHOICES = [
    ("Auto \u2014 cloud first, local fallback", "auto"),
    ("Cloud only", "cloud"),
    ("Local only (audio never leaves this PC)", "local"),
]
CLOUD_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]
NO_USAGE_TEXT = "No cloud usage recorded today."
FREE_TIER_NOTE = (
    "Groq free tier: about 200K tokens/day per cleanup model and "
    "8 hours/day of cloud Whisper audio."
)
VOCAB_HELP = (
    "Technical terms, names and acronyms. Helps speech recognition and "
    "cleanup spell them correctly."
)


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """Parse ``value`` as an int clamped to [lo, hi]; ``default`` if invalid."""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def parse_vocabulary(text: str) -> List[str]:
    """Split text into terms: one per line, stripped, empties dropped,
    case-insensitively de-duplicated (order preserved)."""
    out: List[str] = []
    seen = set()
    for line in text.splitlines():
        term = line.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Fill keys the UI manages with defaults (older configs lack them) so
    the dirty baseline matches what the widgets will collect."""
    d = default_config()
    tr = cfg.setdefault("transcription", {})
    for k in ("engine", "cloud_model", "beam_size", "cpu_threads"):
        tr.setdefault(k, d["transcription"][k])
    fmt = cfg.setdefault("format", {})
    fmt.setdefault("fix_grammar", d["format"]["fix_grammar"])
    cfg.setdefault("tail_ms", d["tail_ms"])
    vocab = cfg.get("vocabulary")
    cfg["vocabulary"] = parse_vocabulary(
        "\n".join(t for t in vocab if isinstance(t, str))
    ) if isinstance(vocab, list) else []
    return cfg


class SettingsWindow:
    """Open the settings window. Blocking; intended to be launched from
    the main thread (Tk mainloop requirement)."""

    def __init__(
        self,
        *,
        initial_config: Dict[str, Any],
        on_apply: Callable[[Dict[str, Any], Optional[str]], None],
        config_path: Optional[ Any ] = None,
    ) -> None:
        """
        Args:
            initial_config: the working config (already merged with current
                config.json). The settings window will edit a deep copy and
                pass it back via ``on_apply`` on Save.
            on_apply: ``on_apply(new_config, new_api_key_or_none)``. The
                key is passed here (not stored in config.json) so callers
                can route it directly to the keychain.
            config_path: optional override path for config.json.
        """
        self._on_apply = on_apply
        self._config_path = config_path or CONFIG_PATH
        self._working: Dict[str, Any] = _normalize_config(copy.deepcopy(initial_config))
        self._initial: Dict[str, Any] = _normalize_config(copy.deepcopy(initial_config))
        self._initial_key: Optional[str] = get_api_key()
        self._pending_key: Optional[str] = self._initial_key  # current textbox value
        self._dirty = False
        self._saved_label_after_id: Optional[str] = None
        self._root: Optional[tk.Tk] = None
        self._save_btn: Optional[ttk.Button] = None
        self._cancel_btn: Optional[ttk.Button] = None
        self._dirty_label: Optional[ttk.Label] = None
        self._saved_label: Optional[ttk.Label] = None
        self._api_key_visible = False

        self._mic_choices: List[Tuple[str, Optional[int]]] = []  # (label, index-or-None)
        self._dict_tree: Optional[ttk.Treeview] = None
        self._vosk_download_btn: Optional[ttk.Button] = None
        self._vosk_progress: Optional[ttk.Progressbar] = None
        self._vocab_text: Optional[tk.Text] = None
        self._usage_text: Optional[tk.Text] = None
        self._test_cloud_btn: Optional[ttk.Button] = None
        self._groq_refresh_btn: Optional[ttk.Button] = None
        self._groq_box: Optional[ttk.Combobox] = None

    # ------------------------------------------------------------------
    # Public entry point

    def open(self) -> None:
        self._build()
        assert self._root is not None
        self._refresh_dirty()
        self._root.mainloop()

    # ------------------------------------------------------------------
    # Build / layout

    def _build(self) -> None:
        root = tk.Tk()
        root.title("Imla Settings")
        root.geometry(DEFAULT_SIZE)
        root.minsize(560, 640)
        root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._root = root

        # Widget variables (populated now that root exists)
        self._hotkey_var = tk.StringVar(master=root)
        self._pre_roll_var = tk.IntVar(master=root)
        self._mic_var = tk.StringVar(master=root)
        self._overlay_var = tk.BooleanVar(master=root)
        self._start_on_login_var = tk.BooleanVar(master=root)
        self._whisper_model_var = tk.StringVar(master=root)
        self._whisper_badge_var = tk.StringVar(master=root, value="")
        self._preview_var = tk.BooleanVar(master=root)
        self._preview_status_var = tk.StringVar(master=root, value="Model: checking…")
        self._tier_var = tk.StringVar(master=root)
        self._groq_model_var = tk.StringVar(master=root)
        self._engine_var = tk.StringVar(master=root)
        self._cloud_model_var = tk.StringVar(master=root)
        self._beam_var = tk.StringVar(master=root)
        self._threads_var = tk.StringVar(master=root)
        self._fix_grammar_var = tk.BooleanVar(master=root)
        self._tail_var = tk.StringVar(master=root)
        self._cloud_status_var = tk.StringVar(master=root, value="")
        self._groq_status_var = tk.StringVar(master=root, value="")
        self._general_status_var = tk.StringVar(master=root, value="")
        self._ollama_model_var = tk.StringVar(master=root)
        self._api_key_var = tk.StringVar(master=root)
        self._test_result_var = tk.StringVar(master=root, value="")
        self._dict_find_var = tk.StringVar(master=root)
        self._dict_replace_var = tk.StringVar(master=root)
        self._dict_status_var = tk.StringVar(master=root, value="")

        # Cmd+S / Ctrl+S keyboard shortcut
        root.bind(
            "<Command-s>" if _is_macos() else "<Control-s>",
            lambda e: self._save(),
        )
        root.bind("<Escape>", lambda e: self._on_close_request())

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self._build_general_tab(notebook)
        self._build_transcription_tab(notebook)
        self._build_cleanup_tab(notebook)
        self._build_vocabulary_tab(notebook)
        self._build_dictionary_tab(notebook)

        self._build_footer(root)

        self._load_into_widgets()

    def _build_general_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=12)
        notebook.add(frame, text="General")

        # Hotkey
        ttk.Label(frame, text="Push-to-talk hotkey").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        hotkey_box = ttk.Combobox(
            frame, textvariable=self._hotkey_var, state="readonly",
            values=[label for label, _ in HOTKEY_CHOICES], width=24,
        )
        hotkey_box.grid(row=0, column=1, sticky="w", pady=(0, 6))
        hotkey_box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())

        # Pre-roll
        ttk.Label(frame, text="Pre-roll buffer (ms)").grid(
            row=1, column=0, sticky="w", pady=6
        )
        slider = ttk.Scale(
            frame, from_=0, to=1000, orient="horizontal",
            variable=self._pre_roll_var, length=240,
            command=lambda v: self._mark_dirty(),
        )
        slider.grid(row=1, column=1, sticky="we", pady=6)
        self._pre_roll_value_label = ttk.Label(frame, text="0 ms", width=8)
        self._pre_roll_value_label.grid(row=1, column=2, sticky="w")
        self._pre_roll_var.trace_add(
            "write", lambda *_: self._pre_roll_value_label.configure(
                text=f"{int(self._pre_roll_var.get())} ms"
            )
        )

        # Microphone
        ttk.Label(frame, text="Active input microphone").grid(
            row=2, column=0, sticky="w", pady=6
        )
        self._mic_choices = self._list_input_devices()
        mic_box = ttk.Combobox(
            frame, textvariable=self._mic_var, state="readonly",
            values=[label for label, _ in self._mic_choices], width=36,
        )
        mic_box.grid(row=2, column=1, sticky="we", pady=6)
        mic_box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())

        # Tail (keep recording after key release)
        ttk.Label(frame, text="Keep recording after key release (ms)").grid(
            row=3, column=0, sticky="w", pady=6
        )
        ttk.Spinbox(
            frame, from_=0, to=1000, increment=50, width=8,
            textvariable=self._tail_var,
        ).grid(row=3, column=1, sticky="w", pady=6)
        self._tail_var.trace_add("write", lambda *_: self._mark_dirty())

        # Overlay toggle
        ttk.Checkbutton(
            frame, text="Show floating overlay while recording",
            variable=self._overlay_var, command=self._mark_dirty,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=6)

        # Start on login
        ttk.Checkbutton(
            frame, text="Start Imla when I log in",
            variable=self._start_on_login_var, command=self._mark_dirty,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=6)

        # Files
        ttk.Separator(frame, orient="horizontal").grid(
            row=6, column=0, columnspan=3, sticky="we", pady=10
        )
        files = ttk.Frame(frame)
        files.grid(row=7, column=0, columnspan=3, sticky="w")
        ttk.Button(files, text="Open log file", command=self._open_log_file).pack(
            side="left"
        )
        ttk.Button(
            files, text="Open config folder", command=self._open_config_folder
        ).pack(side="left", padx=(6, 0))
        ttk.Label(frame, textvariable=self._general_status_var, foreground="#555").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        frame.columnconfigure(1, weight=1)

    def _build_transcription_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=12)
        notebook.add(frame, text="Transcription")

        # Engine
        ttk.Label(frame, text="Speech engine").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        engine_box = ttk.Combobox(
            frame, textvariable=self._engine_var, state="readonly",
            values=[label for label, _ in ENGINE_CHOICES], width=38,
        )
        engine_box.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 6))
        engine_box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Label(
            frame,
            text="Auto/Cloud send your recorded audio to Groq. "
                 "Local keeps audio on this PC but is slower.",
            foreground="#555", wraplength=520, justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # Cloud model + test
        ttk.Label(frame, text="Cloud model").grid(
            row=2, column=0, sticky="w", pady=6
        )
        cloud_box = ttk.Combobox(
            frame, textvariable=self._cloud_model_var, state="readonly",
            values=CLOUD_MODELS, width=24,
        )
        cloud_box.grid(row=2, column=1, sticky="w", pady=6)
        cloud_box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        self._cloud_box = cloud_box
        self._test_cloud_btn = ttk.Button(
            frame, text="Test cloud", command=self._test_cloud
        )
        self._test_cloud_btn.grid(row=2, column=2, sticky="w", padx=(8, 0))
        ttk.Label(frame, textvariable=self._cloud_status_var, wraplength=540).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )

        ttk.Separator(frame, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="we", pady=8
        )

        ttk.Label(frame, text="Local model (fallback)").grid(
            row=5, column=0, sticky="w", pady=(0, 6)
        )
        model_box = ttk.Combobox(
            frame, textvariable=self._whisper_model_var, state="readonly",
            values=WHISPER_MODELS, width=18,
        )
        model_box.grid(row=5, column=1, sticky="w", pady=(0, 6))
        model_box.bind(
            "<<ComboboxSelected>>",
            lambda e: (self._mark_dirty(), self._refresh_transcription_badges()),
        )

        # Cache badge
        ttk.Label(frame, textvariable=self._whisper_badge_var).grid(
            row=5, column=2, sticky="w", padx=(8, 0)
        )

        ttk.Label(
            frame,
            text="Model will load on next dictation.",
            foreground="#555",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Beam size (1-5)").grid(
            row=7, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            frame, from_=1, to=5, width=6, textvariable=self._beam_var
        ).grid(row=7, column=1, sticky="w", pady=4)
        self._beam_var.trace_add("write", lambda *_: self._mark_dirty())

        ttk.Label(frame, text="CPU threads (0 = auto)").grid(
            row=8, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            frame, from_=0, to=16, width=6, textvariable=self._threads_var
        ).grid(row=8, column=1, sticky="w", pady=4)
        self._threads_var.trace_add("write", lambda *_: self._mark_dirty())

        ttk.Separator(frame, orient="horizontal").grid(
            row=9, column=0, columnspan=3, sticky="we", pady=8
        )

        # Live preview
        ttk.Checkbutton(
            frame, text="Live streaming preview (Vosk)",
            variable=self._preview_var, command=self._mark_dirty,
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=6)

        vosk_frame = ttk.Frame(frame)
        vosk_frame.grid(row=11, column=0, columnspan=3, sticky="we", pady=(2, 4))

        self._vosk_download_btn = ttk.Button(
            vosk_frame,
            text="Download Model (~40MB)",
            command=self._start_vosk_download,
        )
        self._vosk_download_btn.pack(side="left", padx=(0, 8))

        self._vosk_progress = ttk.Progressbar(
            vosk_frame, orient="horizontal", length=180, mode="determinate"
        )
        self._vosk_progress.pack(side="left", fill="x", expand=True, padx=(0, 8))

        ttk.Label(frame, textvariable=self._preview_status_var).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )

        frame.columnconfigure(1, weight=1)
        self._refresh_transcription_badges()

    def _build_cleanup_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=12)
        notebook.add(frame, text="Cleanup")

        # Tier selector
        ttk.Label(frame, text="Formatting tier").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        tier_box = ttk.Combobox(
            frame, textvariable=self._tier_var, state="readonly",
            values=TIER_CHOICES, width=18,
        )
        tier_box.grid(row=0, column=1, sticky="w", pady=(0, 6))
        tier_box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())

        # Groq model name
        ttk.Label(frame, text="Groq model").grid(
            row=1, column=0, sticky="w", pady=6
        )
        groq_box = ttk.Combobox(frame, textvariable=self._groq_model_var, width=32)
        groq_box.grid(row=1, column=1, sticky="we", pady=6)
        self._groq_box = groq_box
        self._groq_model_var.trace_add("write", lambda *_: self._mark_dirty())
        self._groq_refresh_btn = ttk.Button(
            frame, text="Refresh", width=9, command=self._refresh_groq_models
        )
        self._groq_refresh_btn.grid(row=1, column=2, sticky="w", padx=(6, 0))
        ttk.Label(frame, textvariable=self._groq_status_var, foreground="#555").grid(
            row=2, column=1, columnspan=2, sticky="w"
        )

        # Ollama model name
        ttk.Label(frame, text="Ollama model").grid(
            row=3, column=0, sticky="w", pady=6
        )
        ollama_entry = ttk.Entry(frame, textvariable=self._ollama_model_var, width=32)
        ollama_entry.grid(row=3, column=1, sticky="we", pady=6)
        self._ollama_model_var.trace_add("write", lambda *_: self._mark_dirty())

        ttk.Checkbutton(
            frame, text="Fix grammar mistakes",
            variable=self._fix_grammar_var, command=self._mark_dirty,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=6)

        ttk.Separator(frame, orient="horizontal").grid(
            row=5, column=0, columnspan=3, sticky="we", pady=8
        )

        # API key
        ttk.Label(frame, text="Groq API key (stored in OS keychain)").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        key_frame = ttk.Frame(frame)
        key_frame.grid(row=7, column=0, columnspan=3, sticky="we")
        self._api_key_entry = ttk.Entry(
            key_frame, textvariable=self._api_key_var, width=44, show="\u2022",
        )
        self._api_key_entry.pack(side="left")
        self._api_key_var.trace_add("write", lambda *_: self._mark_dirty())
        self._show_btn = ttk.Button(
            key_frame, text="Show", width=8, command=self._toggle_key_visibility,
        )
        self._show_btn.pack(side="left", padx=(6, 0))
        self._test_btn = ttk.Button(
            key_frame, text="Test Connection", command=self._test_connection,
        )
        self._test_btn.pack(side="left", padx=(6, 0))

        ttk.Label(frame, textvariable=self._test_result_var).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        ttk.Separator(frame, orient="horizontal").grid(
            row=9, column=0, columnspan=3, sticky="we", pady=8
        )

        # Usage today
        usage_head = ttk.Frame(frame)
        usage_head.grid(row=10, column=0, columnspan=3, sticky="we")
        ttk.Label(usage_head, text="Usage today").pack(side="left")
        ttk.Button(
            usage_head, text="Refresh", width=9, command=self._refresh_usage
        ).pack(side="left", padx=(8, 0))
        self._usage_text = tk.Text(
            frame, height=4, width=60, wrap="word", relief="flat",
            state="disabled", background=ttk.Style().lookup("TFrame", "background") or "#f0f0f0",
        )
        self._usage_text.grid(row=11, column=0, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Label(
            frame, text=FREE_TIER_NOTE, foreground="#555", wraplength=540,
            justify="left",
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))

        frame.columnconfigure(1, weight=1)

    def _build_vocabulary_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=12)
        notebook.add(frame, text="Vocabulary")

        ttk.Label(
            frame, text=VOCAB_HELP, foreground="#555", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            frame, text="One term per line.", foreground="#555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._vocab_text = tk.Text(frame, height=14, width=50, wrap="none", undo=True)
        self._vocab_text.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._vocab_text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self._vocab_text.configure(yscrollcommand=scroll.set)
        self._vocab_text.bind("<KeyRelease>", lambda e: self._mark_dirty())
        self._vocab_text.bind("<<Modified>>", self._on_vocab_modified)

        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

    def _on_vocab_modified(self, _event=None) -> None:
        if self._vocab_text is None:
            return
        if self._vocab_text.edit_modified():
            self._vocab_text.edit_modified(False)
            self._mark_dirty()

    def _set_vocab_text(self, terms: List[str]) -> None:
        if self._vocab_text is None:
            return
        self._vocab_text.delete("1.0", "end")
        self._vocab_text.insert("1.0", "\n".join(terms))
        self._vocab_text.edit_modified(False)

    def _get_vocab_terms(self) -> List[str]:
        if self._vocab_text is None:
            return list(self._working.get("vocabulary", []))
        return parse_vocabulary(self._vocab_text.get("1.0", "end"))

    def _build_dictionary_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=12)
        notebook.add(frame, text="Dictionary")

        ttk.Label(
            frame,
            text="Case-insensitive, word-boundary replacements applied after formatting.",
            foreground="#555",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        cols = ("find", "replace")
        self._dict_tree = ttk.Treeview(
            frame, columns=cols, show="headings", height=10, selectmode="browse",
        )
        self._dict_tree.heading("find", text="Find (spoken)")
        self._dict_tree.heading("replace", text="Replace with")
        self._dict_tree.column("find", width=200)
        self._dict_tree.column("replace", width=200)
        self._dict_tree.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self._dict_tree.bind("<<TreeviewSelect>>", self._on_dict_select)

        # Inputs + buttons
        input_frame = ttk.Frame(frame)
        input_frame.grid(row=2, column=0, columnspan=2, sticky="we", pady=(8, 0))
        ttk.Label(input_frame, text="Find:").grid(row=0, column=0, sticky="w")
        ttk.Entry(input_frame, textvariable=self._dict_find_var, width=24).grid(
            row=0, column=1, sticky="w", padx=(4, 12)
        )
        ttk.Label(input_frame, text="Replace:").grid(row=0, column=2, sticky="w")
        ttk.Entry(input_frame, textvariable=self._dict_replace_var, width=24).grid(
            row=0, column=3, sticky="w", padx=(4, 12)
        )

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=2, sticky="we", pady=(8, 0))
        ttk.Button(btn_frame, text="Add", command=self._dict_add, width=10).pack(
            side="left"
        )
        ttk.Button(btn_frame, text="Update", command=self._dict_update, width=10).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(btn_frame, text="Delete", command=self._dict_delete, width=10).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(btn_frame, text="Clear", command=self._dict_clear, width=10).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(frame, textvariable=self._dict_status_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

    def _build_footer(self, root: tk.Tk) -> None:
        footer = ttk.Frame(root, padding=(12, 8))
        footer.pack(fill="x", side="bottom")

        stats = self._working.get("stats", {})
        dicts = stats.get("total_dictations", 0)
        words = stats.get("total_words", 0)
        self._stats_var = tk.StringVar(value=f"Dictations: {dicts} | Words: {words}")
        ttk.Label(footer, textvariable=self._stats_var).pack(side="left")

        self._dirty_label = ttk.Label(footer, text="", foreground="#A66")
        self._dirty_label.pack(side="left", padx=(12, 0))

        self._saved_label = ttk.Label(footer, text="", foreground="#3a7")
        self._saved_label.pack(side="left", padx=(8, 0))

        self._cancel_btn = ttk.Button(
            footer, text="Cancel", command=self._cancel, width=10, state="disabled",
        )
        self._cancel_btn.pack(side="right")

        self._save_btn = ttk.Button(
            footer, text="Save", command=self._save, width=10, state="disabled",
        )
        self._save_btn.pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------
    # Widget loading / dirty tracking

    def _load_into_widgets(self) -> None:
        cfg = self._working
        # General
        hk = cfg.get("hotkey", "Key.alt_r")
        hk_label = next(
            (label for label, key in HOTKEY_CHOICES if key == hk),
            HOTKEY_CHOICES[0][0],
        )
        self._hotkey_var.set(hk_label)
        self._pre_roll_var.set(int(cfg.get("pre_roll_ms", 0)))
        self._tail_var.set(str(_clamp_int(cfg.get("tail_ms", 250), 0, 1000, 250)))
        # Mic: match by device index stored in config
        self._current_device_index = cfg.get("input_device")
        self._refresh_mic_combobox()
        self._overlay_var.set(bool(cfg.get("overlay_enabled", True)))
        try:
            from . import startup
            self._start_on_login_var.set(startup.is_enabled())
        except Exception:  # noqa: BLE001
            self._start_on_login_var.set(bool(cfg.get("start_on_login", False)))

        # Transcription
        tr = cfg.get("transcription", {})
        self._whisper_model_var.set(tr.get("model", "base"))
        self._preview_var.set(bool(tr.get("preview_enabled", True)))
        engine = tr.get("engine", "auto")
        self._engine_var.set(
            next((l for l, v in ENGINE_CHOICES if v == engine), engine)
        )
        cloud_model = tr.get("cloud_model", CLOUD_MODELS[0])
        self._cloud_model_var.set(cloud_model)
        if cloud_model not in CLOUD_MODELS and getattr(self, "_cloud_box", None):
            self._cloud_box.configure(values=CLOUD_MODELS + [cloud_model])
        self._beam_var.set(str(_clamp_int(tr.get("beam_size", 3), 1, 5, 3)))
        self._threads_var.set(str(_clamp_int(tr.get("cpu_threads", 0), 0, 16, 0)))

        # Cleanup
        fmt = cfg.get("format", {})
        self._tier_var.set(fmt.get("tier", "auto"))
        self._groq_model_var.set(fmt.get("groq_model", "openai/gpt-oss-20b"))
        self._ollama_model_var.set(fmt.get("ollama_model", "llama3.2:3b"))
        self._fix_grammar_var.set(bool(fmt.get("fix_grammar", True)))

        # Vocabulary
        self._set_vocab_text(
            [t for t in cfg.get("vocabulary", []) if isinstance(t, str)]
        )

        # API key from keychain (never from config)
        if self._initial_key:
            self._api_key_var.set(self._initial_key)

        # Dictionary
        self._refresh_dict_tree()

        # Refresh badges
        self._refresh_transcription_badges()
        self._refresh_usage()

        # Now compute dirty baseline (must be after all vars loaded).
        self._refresh_dirty()

    def _refresh_mic_combobox(self) -> None:
        if not self._mic_choices:
            self._mic_choices = self._list_input_devices()
        # Find a label matching the saved index, else "(System default)".
        saved_idx = self._current_device_index
        match_label = "(System default)"
        for label, idx in self._mic_choices:
            if idx == saved_idx:
                match_label = label
                break
        self._mic_var.set(match_label)

    def _refresh_dict_tree(self) -> None:
        if self._dict_tree is None:
            return
        for iid in self._dict_tree.get_children():
            self._dict_tree.delete(iid)
        for find, replace in get_dictionary(self._working):
            self._dict_tree.insert("", "end", values=(find, replace))

    def _refresh_transcription_badges(self) -> None:
        # Whisper cache status — check HuggingFace cache dir.
        try:
            from pathlib import Path

            cache = Path.home() / ".cache" / "huggingface" / "hub"
            model_name = (self._whisper_model_var.get() or "base").lower()
            approx_sizes = {
                "tiny": "75MB",
                "base": "145MB",
                "small": "480MB",
                "small.en": "480MB",
                "medium": "1.5GB",
                "distil-large-v3": "1.5GB",
            }
            approx = approx_sizes.get(model_name, "")
            matches = (
                list(cache.glob(f"models--*faster-whisper-{model_name}"))
                if cache.exists()
                else []
            )
            if matches:
                total_bytes = 0
                for p in matches:
                    for f in p.rglob("*"):
                        if f.is_file() and not f.is_symlink():
                            total_bytes += f.stat().st_size
                if total_bytes > 1_000_000:
                    mb = round(total_bytes / (1024 * 1024))
                    self._whisper_badge_var.set(f"● Cached ({mb}MB)")
                else:
                    self._whisper_badge_var.set(
                        f"● Cached ({approx})" if approx else "● Cached"
                    )
            else:
                self._whisper_badge_var.set("○ Needs Download")
        except Exception:  # noqa: BLE001
            self._whisper_badge_var.set("")

        # Vosk preview status
        try:
            from .preview import is_model_cached

            cached = is_model_cached()
            if cached:
                if self._vosk_download_btn is not None:
                    self._vosk_download_btn.configure(
                        state="disabled", text="Downloaded"
                    )
                if self._vosk_progress is not None:
                    self._vosk_progress["value"] = 100
                self._preview_status_var.set("Cached (~40MB) — Ready")
            else:
                if self._vosk_download_btn is not None:
                    self._vosk_download_btn.configure(
                        state="normal", text="Download Model (~40MB)"
                    )
                if self._vosk_progress is not None:
                    self._vosk_progress["value"] = 0
                self._preview_status_var.set("Needs Download (~40MB)")
        except Exception:  # noqa: BLE001
            self._preview_status_var.set("Model: unknown")

    def _start_vosk_download(self) -> None:
        if self._vosk_download_btn is not None:
            self._vosk_download_btn.configure(state="disabled", text="Downloading…")
        if self._vosk_progress is not None:
            self._vosk_progress["value"] = 0
        self._preview_status_var.set("Downloading Vosk model (~40MB)…")

        def _worker() -> None:
            try:
                from .preview import download_model

                def _progress_cb(downloaded: int, total: int) -> None:
                    if total > 0 and self._root is not None:
                        pct = int((downloaded / total) * 100)
                        self._root.after(0, lambda p=pct: self._on_vosk_progress(p))

                download_model(progress_cb=_progress_cb)
                if self._root is not None:
                    self._root.after(0, self._on_vosk_download_success)
            except Exception as exc:  # noqa: BLE001
                err_msg = str(exc)
                if self._root is not None:
                    self._root.after(0, lambda: self._on_vosk_download_error(err_msg))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _on_vosk_progress(self, percent: int) -> None:
        if self._vosk_progress is not None:
            self._vosk_progress["value"] = percent
        self._preview_status_var.set(f"Downloading Vosk model… {percent}%")

    def _on_vosk_download_success(self) -> None:
        if self._vosk_download_btn is not None:
            self._vosk_download_btn.configure(state="disabled", text="Downloaded")
        if self._vosk_progress is not None:
            self._vosk_progress["value"] = 100
        self._preview_status_var.set("Cached (~40MB) — Ready")

    def _on_vosk_download_error(self, err: str) -> None:
        if self._vosk_download_btn is not None:
            self._vosk_download_btn.configure(state="normal", text="Download Model (~40MB)")
        self._preview_status_var.set(f"Download failed: {err}")

    # ------------------------------------------------------------------
    # Dirty tracking

    def _collect_current(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """Build a (config, api_key) snapshot of the current widget state."""
        cfg = copy.deepcopy(self._working)
        # General
        hk_label = self._hotkey_var.get()
        hk_key = next(
            (key for label, key in HOTKEY_CHOICES if label == hk_label),
            HOTKEY_CHOICES[0][1],
        )
        cfg["hotkey"] = hk_key
        cfg["pre_roll_ms"] = int(self._pre_roll_var.get())
        # Mic: index from combobox label
        mic_label = self._mic_var.get()
        cfg["input_device"] = next(
            (idx for label, idx in self._mic_choices if label == mic_label),
            None,
        )
        cfg["tail_ms"] = _clamp_int(
            self._tail_var.get(), 0, 1000, int(self._initial.get("tail_ms", 250))
        )
        cfg["overlay_enabled"] = bool(self._overlay_var.get())
        cfg["start_on_login"] = bool(self._start_on_login_var.get())
        # Transcription
        cfg.setdefault("transcription", {})
        cfg["transcription"]["model"] = self._whisper_model_var.get()
        cfg["transcription"]["preview_enabled"] = bool(self._preview_var.get())
        engine_label = self._engine_var.get()
        cfg["transcription"]["engine"] = next(
            (v for l, v in ENGINE_CHOICES if l == engine_label), engine_label or "auto"
        )
        cfg["transcription"]["cloud_model"] = self._cloud_model_var.get() or CLOUD_MODELS[0]
        cfg["transcription"]["beam_size"] = _clamp_int(self._beam_var.get(), 1, 5, 3)
        cfg["transcription"]["cpu_threads"] = _clamp_int(self._threads_var.get(), 0, 16, 0)
        # Cleanup
        cfg.setdefault("format", {})
        cfg["format"]["tier"] = self._tier_var.get()
        cfg["format"]["groq_model"] = self._groq_model_var.get()
        cfg["format"]["ollama_model"] = self._ollama_model_var.get()
        cfg["format"]["fix_grammar"] = bool(self._fix_grammar_var.get())
        cfg["vocabulary"] = self._get_vocab_terms()
        # API key — return what's currently in the textbox.
        key = self._api_key_var.get().strip() or None
        return cfg, key

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._refresh_dirty()

    def _refresh_dirty(self) -> None:
        cfg, key = self._collect_current()
        key_dirty = (key or None) != (self._initial_key or None)
        is_clean = (
            cfg == self._initial
            and not key_dirty
        )
        self._dirty = not is_clean
        if self._save_btn is not None:
            self._save_btn.configure(
                state=("normal" if not is_clean else "disabled")
            )
        if self._cancel_btn is not None:
            self._cancel_btn.configure(
                state=("normal" if not is_clean else "disabled")
            )
        if self._dirty_label is not None:
            self._dirty_label.configure(
                text=("● Unsaved changes" if not is_clean else "")
            )

    # ------------------------------------------------------------------
    # Actions

    def _save(self) -> None:
        cfg, key = self._collect_current()
        # Persist config (without the key).
        try:
            save_config(cfg, path=self._config_path)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to save config: %s", e)
            messagebox.showerror("Save failed", f"Could not write config:\n{e}")
            return
        # Persist key into keychain.
        if not set_api_key(key):
            messagebox.showwarning(
                "Keychain warning",
                "Could not write the API key to the OS keychain.\n"
                "Your settings were saved, but the key may not persist.",
            )
        # Update start-on-login integration.
        try:
            from . import startup
            if cfg.get("start_on_login"):
                startup.enable()
            else:
                startup.disable()
        except Exception:  # noqa: BLE001
            log.exception("Failed to update start-on-login state.")
        # Live apply via callback.
        try:
            self._on_apply(cfg, key)
        except Exception:  # noqa: BLE001
            log.exception("Live apply callback failed.")
        # Update baseline + UI.
        self._working = copy.deepcopy(cfg)
        self._initial = copy.deepcopy(cfg)
        self._initial_key = key
        self._dirty = False
        self._refresh_dirty()
        self._flash_saved_label()

    def _cancel(self) -> None:
        self._working = copy.deepcopy(self._initial)
        self._pending_key = self._initial_key
        self._api_key_var.set(self._initial_key or "")
        self._load_into_widgets()
        if self._root is not None:
            self._root.destroy()

    def _on_close_request(self) -> None:
        cfg, key = self._collect_current()
        key_dirty = (key or None) != (self._initial_key or None)
        is_clean = (cfg == self._initial and not key_dirty)
        if is_clean:
            self._cancel()
            return
        choice = _prompt_close_guard(self._root)
        if choice == "save":
            self._save()
            if self._root is not None:
                self._root.destroy()
        elif choice == "discard":
            self._cancel()
        else:
            # cancel — do nothing
            pass

    def _flash_saved_label(self) -> None:
        if self._saved_label is None:
            return
        self._saved_label.configure(text="✓ Saved")
        if self._saved_label_after_id is not None:
            try:
                self._root.after_cancel(self._saved_label_after_id)
            except Exception:  # noqa: BLE001
                pass
        self._saved_label_after_id = self._root.after(
            2000, lambda: self._saved_label.configure(text="")
        )

    # ------------------------------------------------------------------
    # API key visibility / test

    def _toggle_key_visibility(self) -> None:
        self._api_key_visible = not self._api_key_visible
        self._api_key_entry.configure(show=("" if self._api_key_visible else "•"))
        self._show_btn.configure(text=("Hide" if self._api_key_visible else "Show"))

    def _test_connection(self) -> None:
        key = self._api_key_var.get().strip()
        if not key:
            self._test_result_var.set("No API key to test.")
            return
        self._test_result_var.set("Testing…")
        if self._root is not None:
            self._root.update_idletasks()
        ok, message = groq_test(key)
        self._test_result_var.set(("✓ " if ok else "✗ ") + message)

    # ------------------------------------------------------------------
    # Cloud test / model list / usage / files

    def _after(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the Tk thread (safe from worker threads)."""
        try:
            if self._root is not None:
                self._root.after(0, fn)
        except Exception:  # noqa: BLE001
            pass

    def _test_cloud(self) -> None:
        if self._test_cloud_btn is not None:
            self._test_cloud_btn.configure(state="disabled")
        self._cloud_status_var.set("Testing cloud transcription\u2026")

        def _run() -> None:
            result = self._test_cloud_worker()
            self._after(lambda: self._on_test_cloud_done(result))

        threading.Thread(target=_run, daemon=True).start()

    def _test_cloud_worker(self) -> Tuple[bool, str]:
        """Blocking call (run off the Tk thread). Never raises."""
        try:
            from .transcribe import test_cloud

            ok, message, latency = test_cloud(api_key=None)
            if latency:
                message = f"{message} ({float(latency):.1f}s)"
            return bool(ok), str(message)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloud test failed: %s", exc)
            return False, f"Cloud test unavailable: {exc}"

    def _on_test_cloud_done(self, result: Tuple[bool, str]) -> None:
        ok, message = result
        self._cloud_status_var.set(("\u2713 " if ok else "\u2717 ") + message)
        if self._test_cloud_btn is not None:
            self._test_cloud_btn.configure(state="normal")

    def _refresh_groq_models(self) -> None:
        if self._groq_refresh_btn is not None:
            self._groq_refresh_btn.configure(state="disabled")
        self._groq_status_var.set("Fetching models\u2026")

        def _run() -> None:
            models = self._fetch_groq_models_worker()
            self._after(lambda: self._on_groq_models(models))

        threading.Thread(target=_run, daemon=True).start()

    def _fetch_groq_models_worker(self) -> List[str]:
        """Blocking call (run off the Tk thread). Never raises."""
        try:
            from .format import list_groq_chat_models

            return [str(m) for m in (list_groq_chat_models(api_key=None) or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not list Groq models: %s", exc)
            return []

    def _on_groq_models(self, models: List[str]) -> None:
        if self._groq_refresh_btn is not None:
            self._groq_refresh_btn.configure(state="normal")
        if not models:
            self._groq_status_var.set(
                "Could not fetch models (check key / internet)"
            )
            return
        current = self._groq_model_var.get()
        values = list(models)
        if current and current not in values:
            values.insert(0, current)
        if self._groq_box is not None:
            self._groq_box.configure(values=values)
        self._groq_status_var.set(f"{len(models)} models available.")

    def _refresh_usage(self) -> None:
        try:
            from .usage import summary_lines

            lines = [str(l) for l in (summary_lines() or [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("Usage summary unavailable: %s", exc)
            lines = []
        self._set_usage_lines(lines)

    def _set_usage_lines(self, lines: List[str]) -> None:
        if self._usage_text is None:
            return
        text = "\n".join(lines) if lines else NO_USAGE_TEXT
        self._usage_text.configure(state="normal")
        self._usage_text.delete("1.0", "end")
        self._usage_text.insert("1.0", text)
        self._usage_text.configure(state="disabled")

    def _config_dir(self) -> str:
        return os.path.dirname(os.path.abspath(str(self._config_path)))

    def _open_path(self, path: str) -> None:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        else:
            import subprocess

            subprocess.Popen(["open" if _is_macos() else "xdg-open", path])

    def _open_log_file(self) -> None:
        path = os.path.join(self._config_dir(), "imla.log")
        if not os.path.exists(path):
            self._general_status_var.set("No log file yet (nothing logged).")
            return
        try:
            self._open_path(path)
            self._general_status_var.set("")
        except Exception as exc:  # noqa: BLE001
            self._general_status_var.set(f"Could not open log file: {exc}")

    def _open_config_folder(self) -> None:
        path = self._config_dir()
        try:
            os.makedirs(path, exist_ok=True)
            self._open_path(path)
            self._general_status_var.set("")
        except Exception as exc:  # noqa: BLE001
            self._general_status_var.set(f"Could not open config folder: {exc}")

    # ------------------------------------------------------------------
    # Dictionary handlers

    def _on_dict_select(self, _event) -> None:
        if self._dict_tree is None:
            return
        sel = self._dict_tree.selection()
        if not sel:
            return
        values = self._dict_tree.item(sel[0], "values")
        if values:
            self._dict_find_var.set(values[0])
            self._dict_replace_var.set(values[1])

    def _dict_add(self) -> None:
        find = self._dict_find_var.get().strip()
        replace = self._dict_replace_var.get()
        if not find:
            self._dict_status_var.set("Find cannot be empty.")
            return
        d = self._working.setdefault("dictionary", [])
        # Reject duplicate "find"
        for entry in d:
            if isinstance(entry, (list, tuple)) and entry and entry[0].lower() == find.lower():
                self._dict_status_var.set(f"'{find}' already exists.")
                return
        d.append([find, replace])
        self._refresh_dict_tree()
        self._mark_dirty()
        self._dict_status_var.set(f"Added: {find} → {replace}")

    def _dict_update(self) -> None:
        if self._dict_tree is None:
            return
        sel = self._dict_tree.selection()
        if not sel:
            self._dict_status_var.set("Select a row to update.")
            return
        find = self._dict_find_var.get().strip()
        replace = self._dict_replace_var.get()
        if not find:
            self._dict_status_var.set("Find cannot be empty.")
            return
        iid = sel[0]
        old_values = self._dict_tree.item(iid, "values")
        old_find = old_values[0] if old_values else None
        d = self._working.setdefault("dictionary", [])
        for i, entry in enumerate(d):
            if isinstance(entry, (list, tuple)) and entry and entry[0] == old_find:
                d[i] = [find, replace]
                break
        self._refresh_dict_tree()
        self._mark_dirty()
        self._dict_status_var.set(f"Updated: {old_find} → {find}")

    def _dict_delete(self) -> None:
        if self._dict_tree is None:
            return
        sel = self._dict_tree.selection()
        if not sel:
            self._dict_status_var.set("Select a row to delete.")
            return
        iid = sel[0]
        values = self._dict_tree.item(iid, "values")
        if not values:
            return
        find = values[0]
        d = self._working.setdefault("dictionary", [])
        self._working["dictionary"] = [
            entry for entry in d
            if not (isinstance(entry, (list, tuple)) and entry and entry[0] == find)
        ]
        self._refresh_dict_tree()
        self._mark_dirty()
        self._dict_status_var.set(f"Deleted: {find}")

    def _dict_clear(self) -> None:
        if not messagebox.askyesno(
            "Clear dictionary", "Remove all entries?", parent=self._root
        ):
            return
        self._working["dictionary"] = []
        self._refresh_dict_tree()
        self._mark_dirty()
        self._dict_status_var.set("Cleared.")

    # ------------------------------------------------------------------
    # Static helpers

    @staticmethod
    def _list_input_devices() -> List[Tuple[str, Optional[int]]]:
        """Return [(label, index)] for input devices + a 'System default' entry."""
        out: List[Tuple[str, Optional[int]]] = [("(System default)", None)]
        try:
            devices = sd.query_devices()
        except Exception:  # noqa: BLE001
            return out
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                name = d.get("name", f"Device {i}")
                sr = int(d.get("default_samplerate", 0))
                out.append((f"#{i}: {name} ({sr} Hz)", i))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_macos() -> bool:
    import sys
    return sys.platform == "darwin"


def _prompt_close_guard(parent: Optional[tk.Tk]) -> str:
    """Modal dialog asking Save / Discard / Cancel. Returns one of those."""
    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    win.title("Unsaved changes")
    win.transient(parent)
    win.resizable(False, False)
    ttk.Label(
        win,
        text="You have unsaved changes. What would you like to do?",
        padding=(16, 12),
    ).pack()
    result = tk.StringVar(value="cancel")

    btns = ttk.Frame(win, padding=(16, 0, 16, 12))
    btns.pack(fill="x")

    def pick(choice: str) -> None:
        result.set(choice)
        win.destroy()

    ttk.Button(btns, text="Cancel", command=lambda: pick("cancel"), width=10).pack(
        side="right"
    )
    ttk.Button(btns, text="Discard", command=lambda: pick("discard"), width=10).pack(
        side="right", padx=(0, 6)
    )
    ttk.Button(btns, text="Save", command=lambda: pick("save"), width=10).pack(
        side="right", padx=(0, 6)
    )

    # Center on parent
    if parent is not None:
        parent.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        win.update_idletasks()
        win.geometry(
            f"+{px - win.winfo_width() // 2}+{py - win.winfo_height() // 2}"
        )

    win.grab_set()
    win.wait_window()
    return result.get()


def open_settings_window(
    *,
    initial_config: Dict[str, Any],
    on_apply: Callable[[Dict[str, Any], Optional[str]], None],
) -> threading.Thread:
    """Convenience function: open the settings window on its own thread."""
    win = SettingsWindow(initial_config=initial_config, on_apply=on_apply)
    t = threading.Thread(target=win.open, daemon=True, name="imla-settings")
    t.start()
    return t
