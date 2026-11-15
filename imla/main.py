"""Imla — orchestration loop.

Wires:
  * Recorder           — with auto-recovery + silence detection
  * StreamingPreview   — Vosk live overlay (optional)
  * Transcribe         — faster-whisper (model selectable from config)
  * Format             — 3-tier cleanup (Groq → Ollama → rule)
  * Dictionary         — personal replacements applied post-format
  * HUD                — floating pill (expanded when preview is active)
  * Tray               — background menu bar / taskbar icon
  * Settings           — Tkinter dashboard, run as a separate process
                         (``--settings``) so Tk always owns its main thread;
                         the running app live-reloads config.json on save.
  * Startup            — launch-on-login (LaunchAgent / Run registry /
                         XDG autostart)

CLI:
  ``--list-devices`` — print available input devices and exit.
  ``--device ID|NAME`` — lock the recorder to a specific input.
  ``--settings`` — open only the Settings dashboard.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv  # noqa: E402

# Logging first: under pythonw.exe (desktop shortcut) stdout/stderr are None,
# and the heavy imports below may print or warn.
from . import applog  # noqa: E402

applog.setup_logging()
applog.install_excepthook()

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

load_dotenv()

from .audio import Recorder
from .config import (
    CONFIG_PATH,
    get_effective_dictionary,
    get_vocabulary,
    load_config,
    save_config,
)
from .dictionary import apply_dictionary, compile_dictionary
from .format import clean_transcript, log_format_setup
from .hotkey import PushToTalk
from .hud import HUD
from .inject import accessibility_hint, inject_text, is_trusted
from . import launcher, single_instance
from .keychain import get_api_key, import_env_key_if_missing, set_api_key
from .preview import StreamingPreview
from .startup import enable as startup_enable, disable as startup_disable, is_enabled as startup_is_enabled
from .transcribe import (
    preload_async,
    set_decode_options,
    set_engine,
    set_model_name,
    transcribe,
    transcribe_detailed,
)
from .tray import TrayController

log = logging.getLogger("imla")

# Project root (or the exe's folder when packaged).
_PROJECT_ROOT = launcher.project_root()


# ---------------------------------------------------------------------------
# Mutable application state shared across components.
# ---------------------------------------------------------------------------


class AppState:
    """Shared, locked state for cross-component communication."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.muted = False
        self.lock = threading.Lock()
        self._recorder: Optional[Recorder] = None

    def set_recorder(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def is_recording(self) -> bool:
        return self._recorder is not None and self._recorder._stream is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="imla",
        description="Imla push-to-talk voice dictation.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Lock the recorder to a specific input device (index or name substring).",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices and exit.",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Open the Settings dashboard directly.",
    )
    parser.add_argument(
        "--quit",
        action="store_true",
        help="Ask the running Imla to quit, then exit.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help=f"Override path to config.json (default: {CONFIG_PATH}).",
    )
    return parser.parse_args(argv)


def cmd_list_devices() -> int:
    print("Available input devices:")
    try:
        devices = sd.query_devices()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not query devices: {e})")
        return 1
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            sr = int(d.get("default_samplerate", 0))
            print(f"  #{i}: {d.get('name', '?')} ({sr} Hz)")
    print()
    try:
        default = sd.query_devices(kind="input")
        print(f"Default input: #{default.get('index')} — {default.get('name')}")
    except Exception:  # noqa: BLE001
        pass
    return 0


# ---------------------------------------------------------------------------
# Logging / startup
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    # Console (when there is one) + rotating file at ~/.config/imla/imla.log.
    applog.setup_logging()


def _select_input_device() -> None:
    try:
        dev = sd.query_devices(kind="input")
        idx = dev.get("index", "?")
        name = dev.get("name", "default input")
        sr = int(dev.get("default_samplerate", 16000))
        ch = int(dev.get("max_input_channels", 1))
        log.info(
            "Selected input device #%s: %s (%d Hz, %d ch max)",
            idx, name, sr, ch,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not query default input device: %s", e)


# ---------------------------------------------------------------------------
# Config → live apply
# ---------------------------------------------------------------------------


def apply_config(
    app: AppState,
    cfg: Dict[str, Any],
    recorder: Recorder,
    ptt: Optional[PushToTalk] = None,
) -> None:
    """Push config values into the live components (no restart)."""
    # Whisper model
    tcfg = cfg.get("transcription", {})
    set_engine(
        tcfg.get("engine", "auto"),
        tcfg.get("cloud_model", "whisper-large-v3-turbo"),
    )
    set_model_name(tcfg.get("model", "small.en"))
    set_decode_options(
        beam_size=tcfg.get("beam_size", 3),
        cpu_threads=tcfg.get("cpu_threads", 0),
    )
    # Load the model now (no-op if already loaded) so the next dictation
    # doesn't pay the load/download cost.
    preload_async()

    # Microphone lock
    dev = cfg.get("input_device")
    if isinstance(dev, int):
        recorder.lock_device(index=dev)
    elif isinstance(dev, str) and dev:
        recorder.lock_device(name_substring=dev)
    else:
        recorder.lock_device(index=None, name_substring=None)

    # Push-to-talk hotkey rebind
    if ptt is not None and "hotkey" in cfg:
        ptt.set_key(cfg["hotkey"])

    # Start-on-login
    want = bool(cfg.get("start_on_login", False))
    currently = startup_is_enabled()
    if want and not currently:
        startup_enable()
    elif not want and currently:
        startup_disable()

    # Keep the existing dict reference intact so closures holding ``app.config``
    # or ``config`` see updated values immediately.
    if isinstance(app.config, dict) and app.config is not cfg:
        app.config.clear()
        app.config.update(cfg)
    else:
        app.config = cfg


# ---------------------------------------------------------------------------
# Finalize (transcribe → format → inject) with metrics
# ---------------------------------------------------------------------------


def _run_finalize(
    audio,
    hud: Any,
    config: Any = None,
    api_key: Optional[str] = None,
    audio_duration_s: float = 0.0,
    release_monotonic: float = 0.0,
    inject_fn: Optional[Callable[[str], None]] = None,
) -> None:
    if callable(config):
        inject_fn = config
        config = None
    if config is None:
        config = {}
    if release_monotonic == 0.0:
        release_monotonic = time.monotonic()
    release_ts = release_monotonic
    try:
        log.info("Transcribing with Whisper...")
        t_w0 = time.monotonic()
        try:
            stt = transcribe_detailed(
                audio, vocab=get_vocabulary(config), api_key=api_key
            )
        except Exception:  # noqa: BLE001
            log.exception("Transcription failed.")
            return
        raw = stt.text
        t_w1 = time.monotonic()
        whisper_s = t_w1 - t_w0
        stt_label = stt.engine
        if stt.fallback_reason:
            stt_label += f", fallback: {stt.fallback_reason}"

        if not raw:
            log.info("(empty transcript)")
            return

        log.info("Raw transcript (%d words): %r", len(raw.split()), raw)

        # Format (3-tier)
        fmt_cfg = config.get("format", {})
        t_f0 = time.monotonic()
        try:
            result = clean_transcript(
                raw,
                groq_api_key=api_key,
                groq_model=fmt_cfg.get("groq_model", "openai/gpt-oss-20b"),
                ollama_model=fmt_cfg.get("ollama_model", "llama3.2:3b"),
                tier_override=fmt_cfg.get("tier", "auto"),
                dictionary=get_effective_dictionary(config),
                vocabulary=get_vocabulary(config),
                fix_grammar=bool(fmt_cfg.get("fix_grammar", True)),
                developer_focus=bool(fmt_cfg.get("developer_focus", True)),
                # The short-utterance LLM bypass saves quota but can't fix
                # grammar, so only use it when grammar fixing is off.
                skip_llm_for_short=not bool(fmt_cfg.get("fix_grammar", True)),
            )
        except Exception:  # noqa: BLE001
            log.exception("Formatting pipeline failed; injecting raw transcript.")
            cleaned = raw
            tier_label = "RAW"
        else:
            cleaned = result.text
            tier_label = result.tier.upper()
            log.info(
                "Cleaned (%s, %d → %d words): %r",
                tier_label, result.word_count_in, result.word_count_out, cleaned,
            )
            # Diagnostics: which raw words are absent from the output?
            out_words = {w.strip(".,!?;:\"'()").lower() for w in cleaned.split()}
            dropped = [
                w for w in raw.split()
                if w.strip(".,!?;:\"'()").lower() not in out_words
            ]
            if dropped:
                log.info("Words not present in cleaned text: %s", " ".join(dropped))
        t_f1 = time.monotonic()
        format_s = t_f1 - t_f0

        # Update stats (in-memory; flushed on Save)
        stats = config.setdefault("stats", {})
        stats["total_dictations"] = int(stats.get("total_dictations", 0)) + 1
        stats["total_words"] = int(stats.get("total_words", 0)) + len(cleaned.split())

        # Inject
        try:
            if inject_fn is not None:
                injected = inject_fn(cleaned)
            else:
                injected = inject_text(cleaned)
            # A custom inject_fn (tests, alternate backends) may return None;
            # only treat an explicit False as failure.
            if injected is False:
                log.warning("Transcript not injected; see the error above.")
            else:
                log.info("Injected transcript at cursor.")
        except Exception:  # noqa: BLE001
            log.exception("Injection failed.")
            return

        total_s = t_f1 - release_ts
        log.info(
            "[AUDIO] %.2fs | [STT:%s] %.2fs | [FORMAT] %.2fs (%s) | Total: %.2fs",
            audio_duration_s,
            stt_label,
            whisper_s,
            format_s,
            tier_label,
            total_s,
        )
    finally:
        try:
            hud.hide()
        except Exception:  # noqa: BLE001
            pass


_finalize = _run_finalize


# ---------------------------------------------------------------------------
# Silence / device fallback handling
# ---------------------------------------------------------------------------


def _make_silence_handler(
    recorder: Recorder,
    hud: HUD,
    app: AppState,
    config: Dict[str, Any],
) -> Callable[[], None]:
    """Return a one-shot callback for when the wireless dongle goes silent."""

    def handle() -> None:
        log.warning(
            "Detected 1.5s of continuous silence on the active input (%s) — "
            "treating as a dead wireless dongle. Auto-switching to fallback mic.",
            recorder.device_name or "current device",
        )
        fallback_idx = recorder.switch_to_fallback()
        if fallback_idx is not None:
            config["input_device"] = fallback_idx
            app.config["input_device"] = fallback_idx
            try:
                hud.set_status_text(f"⚠ Switched mic: {recorder.device_name}")
            except Exception:  # noqa: BLE001
                pass

    return handle


# ---------------------------------------------------------------------------
# Settings apply callback
# ---------------------------------------------------------------------------


def make_on_apply(
    app: AppState,
    recorder: Recorder,
    hud: Optional[HUD] = None,
    ptt: Optional[PushToTalk] = None,
) -> Callable[[Dict[str, Any], Optional[str]], None]:
    """Build the live-apply callback invoked from the Settings window."""

    def on_apply(cfg: Dict[str, Any], api_key: Optional[str]) -> None:
        # Persist API key into keychain (best-effort; we already do this in
        # SettingsWindow._save, but we re-apply here in case it changed).
        if api_key is not None:
            set_api_key(api_key or None)
        apply_config(app, cfg, recorder, ptt)
        log.info("Applied config from Settings dashboard.")

    return on_apply


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)

    _setup_logging()

    if args.list_devices:
        return cmd_list_devices()

    if args.quit:
        ok = single_instance.request_quit()
        print("Asked Imla to quit." if ok else "Could not write the quit request.")
        return 0 if ok else 1

    config_path = args.config or str(CONFIG_PATH)
    config_path_obj = Path(config_path)
    config = load_config(path=config_path_obj)

    # Only one dictation process may own the hotkey. The Settings window is a
    # separate process and is allowed alongside it.
    if not args.settings and not single_instance.acquire("imla"):
        log.info("Imla is already running; exiting this second instance.")
        return 0

    # A stale "quit" marker (left when nothing was running) must not kill us.
    single_instance.clear_quit_request()

    # Copy a .env key into the OS keychain so the app also works when it is
    # not started from the project folder (desktop shortcut, start on login).
    if import_env_key_if_missing():
        log.info("Imported the Groq API key from .env into the OS keychain.")
    api_key = get_api_key()

    # CLI --device overrides config
    if args.device:
        try:
            config["input_device"] = int(args.device)
        except ValueError:
            config["input_device"] = args.device

    if args.settings:
        # Standalone Settings process (also what the tray's "Settings…"
        # launches). SettingsWindow itself persists config.json, the
        # keychain entry and start-on-login; the running dictation process
        # notices the config.json change and applies it live.
        from .settings import SettingsWindow

        def _on_saved(_cfg: Dict[str, Any], _key: Optional[str]) -> None:
            log.info("Settings saved.")

        SettingsWindow(
            initial_config=config,
            on_apply=_on_saved,
            config_path=config_path_obj,
        ).open()
        return 0

    log.info("Imla starting up…")
    tcfg = config.get("transcription", {})
    log.info(
        "Speech engine: %s | cloud model: %s | local model: faster-whisper %s "
        "(cpu, int8, beam %s)",
        tcfg.get("engine", "auto"),
        tcfg.get("cloud_model", "whisper-large-v3-turbo"),
        tcfg.get("model", "small.en"), tcfg.get("beam_size", 3),
    )
    log.info("Log file: %s", applog.LOG_PATH)
    log_format_setup(
        config.get("format", {}),
        has_groq_key=bool(get_api_key()),
        vocabulary=get_vocabulary(config),
        developer_focus=bool(config.get("format", {}).get("developer_focus", True)),
    )

    if not is_trusted(prompt=True):
        log.warning(
            "No Accessibility permission — dictation will transcribe but the "
            "text will NOT appear at your cursor. Enable %s under System Settings "
            "→ Privacy & Security → Accessibility.",
            accessibility_hint(),
        )

    app = AppState(config)

    recorder = Recorder()
    app.set_recorder(recorder)

    if config.get("input_device") is None and args.device is None:
        _select_input_device()

    hud = HUD(rms_provider=recorder.current_rms_levels)
    preview = StreamingPreview(on_partial=hud.set_preview_text)

    state_lock = threading.Lock()
    transcribe_thread: Optional[threading.Thread] = None
    shutdown_event = threading.Event()

    def on_press() -> None:
        nonlocal transcribe_thread
        with state_lock:
            if app.muted:
                log.info("Muted — ignoring press.")
                return
            if transcribe_thread is not None and transcribe_thread.is_alive():
                log.info("Still transcribing previous utterance; ignoring press.")
                return
            try:
                native_sr, native_ch = recorder.start()
            except Exception:  # noqa: BLE001
                log.exception("Failed to start recorder.")
                return
            # Read live so a Settings change applies without restart.
            preview_enabled = bool(
                config.get("transcription", {}).get("preview_enabled", True)
            )
            preview_active = preview_enabled and preview.start(native_sr)
            if preview_active:
                recorder.set_preview_sink(preview.feed)
            # One-shot silence callback for this recording.
            recorder.set_silence_callback(
                _make_silence_handler(
                    recorder,
                    hud,
                    app,
                    config,
                )
            )
            log.info(
                "Recording… (native %d Hz, %d ch, %s). Release hotkey to stop.",
                native_sr, native_ch, recorder.device_name,
            )

        if config.get("overlay_enabled", True):
            hud.show_recording(preview_active=preview_active)

    def on_release() -> None:
        nonlocal transcribe_thread
        # Keep capturing briefly so the last syllable isn't clipped when the
        # key is released a hair early. Done outside the lock.
        tail_s = max(0, int(config.get("tail_ms", 250) or 0)) / 1000.0
        if tail_s and recorder.start_time and not app.muted:
            time.sleep(min(tail_s, 1.0))
        with state_lock:
            recorder.set_preview_sink(None)
            recorder.set_silence_callback(None)
            preview.stop()
            audio = recorder.stop()
            release_ts = time.monotonic()
            if audio is None or len(audio) == 0:
                log.info("No audio captured.")
                if transcribe_thread is None or not transcribe_thread.is_alive():
                    hud.hide()
                return

            audio_duration_s = (
                release_ts - recorder.start_time if recorder.start_time else 0.0
            )

            # Dead Wireless Dongle (100% digital silence) detection on captured audio
            if audio_duration_s >= 1.5 and float(np.max(np.abs(audio))) < 1e-6:
                log.warning(
                    "Captured audio is 100% digital silence (max amplitude == 0.000). "
                    "Microphone may be powered off or muted. Auto-switching to fallback mic."
                )
                fallback_idx = recorder.switch_to_fallback()
                if fallback_idx is not None:
                    config["input_device"] = fallback_idx
                    app.config["input_device"] = fallback_idx
                    try:
                        hud.set_status_text(f"⚠ Switched mic: {recorder.device_name}")
                    except Exception:  # noqa: BLE001
                        pass
                hud.hide()
                return

            transcribe_thread = threading.Thread(
                target=_run_finalize,
                args=(
                    audio,
                    hud,
                    config,
                    api_key,
                    audio_duration_s,
                    release_ts,
                ),
                daemon=True,
            )
            transcribe_thread.start()
        if config.get("overlay_enabled", True):
            hud.show_processing()

    initial_hotkey = config.get("hotkey", "Key.alt_r")
    ptt = PushToTalk(on_press=on_press, on_release=on_release, key=initial_hotkey)
    ptt.start()
    apply_config(app, config, recorder, ptt)

    # ------------------------------------------------------------------
    # Settings window + tray + shutdown coordination

    def request_quit() -> None:
        log.info("Quit requested.")
        shutdown_event.set()
        # Unblocks hud.run() on the main thread (AppKit pump on macOS, the
        # Tk thread join on Windows/Linux) so the finally-block below runs.
        hud.close()

    settings_proc: Optional[subprocess.Popen] = None

    def open_settings() -> None:
        """Launch the Settings dashboard as a child process.

        Tk must own the main thread of its process (on macOS creating it
        anywhere else aborts the app), and our main thread is busy with the
        HUD. The child saves config.json + keychain; the watcher below picks
        the changes up live."""
        nonlocal settings_proc
        if settings_proc is not None and settings_proc.poll() is None:
            log.info("Settings window is already open.")
            return
        # pythonw.exe on Windows (no console window); the exe itself when packaged.
        cmd = launcher.launch_command("--settings")
        if args.config:
            cmd += ["--config", args.config]
        settings_proc = subprocess.Popen(cmd, cwd=str(_PROJECT_ROOT))

    def _config_mtime() -> Optional[float]:
        try:
            return config_path_obj.stat().st_mtime
        except OSError:
            return None

    last_config_mtime = _config_mtime()

    def reload_config_if_changed() -> None:
        nonlocal last_config_mtime, api_key
        mtime = _config_mtime()
        if mtime is None or mtime == last_config_mtime:
            return
        last_config_mtime = mtime
        new_cfg = load_config(path=config_path_obj)
        if args.device:  # CLI override keeps precedence
            try:
                new_cfg["input_device"] = int(args.device)
            except ValueError:
                new_cfg["input_device"] = args.device
        # Keep in-session counters rather than the on-disk snapshot.
        new_cfg["stats"] = config.get("stats", new_cfg.get("stats", {}))
        with state_lock:
            # Mutate in place: on_press/on_release/finalize hold this dict.
            config.clear()
            config.update(new_cfg)
        api_key = get_api_key()
        apply_config(app, config, recorder, ptt)
        log.info("Reloaded settings from %s.", config_path_obj)

    def watch_config() -> None:
        while not shutdown_event.wait(1.0):
            try:
                if single_instance.quit_requested():
                    log.info("Quit requested from Settings / --quit.")
                    request_quit()
                    return
                reload_config_if_changed()
            except Exception:  # noqa: BLE001
                log.exception("Failed to reload settings.")

    threading.Thread(
        target=watch_config, daemon=True, name="imla-config-watch"
    ).start()

    def toggle_mute(new_value: bool) -> None:
        app.muted = new_value
        log.info("Muted: %s", new_value)
        try:
            tray.update()
        except Exception:  # noqa: BLE001
            pass

    tray = TrayController(
        on_settings=open_settings,
        on_quit=request_quit,
        on_toggle_mute=toggle_mute,
        is_muted=lambda: app.muted,
        status_text=lambda: (
            "● Recording" if app.is_recording() else
            ("Muted" if app.muted else "Ready")
        ),
    )
    tray.start()

    log.info(
        "Ready. Hold %s to record. Ctrl+C to quit.",
        str(ptt.key_spec).replace("Key.", ""),
    )
    # Visible feedback for shortcut launches (no console to look at).
    tray.notify(
        "Imla is ready",
        "Hold %s and speak. Right-click the tray icon for Settings or Quit."
        % str(ptt.key_spec).replace("Key.", "").replace("_", " ").title(),
    )

    try:
        # Block until shutdown — on macOS this is hud.run() (AppKit pump);
        # on Windows/Linux it waits on the HUD's Tk thread.
        hud.run()
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down.")
    finally:
        shutdown_event.set()
        ptt.stop()
        preview.stop()
        tray.stop()
        hud.close()
    return 0


if __name__ == "__main__":
    main()
