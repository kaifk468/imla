# Imla: project context (read this first in a new session or on a new machine)

This file is the persistent memory of the project. It records what exists, why it was built that way, what was changed, and what is still open, so work can continue from a fresh `git clone` without the original chat. **Keep it updated whenever you change behaviour** (see "How to maintain this file" at the bottom).

Last updated: 2026-11-15, after commit `6bf3e98`.

## 1. What Imla is

Push-to-talk voice dictation desktop app. Hold **Right Alt / Right Option**, speak, release: the audio is transcribed, cleaned up (punctuation, filler words, grammar) and pasted at the cursor. Python 3.12, managed with `uv`. Platforms: Windows (primary, tested), Ubuntu/Linux and macOS (supported, installers untested).

Pipeline per dictation (`imla/main.py::_run_finalize`):

1. `audio.py` records at the mic's native rate, resamples to 16 kHz mono. A tail (`tail_ms`, default 250 ms) keeps recording after key release so the last word is not clipped.
2. `preview.py` (Vosk) shows a live provisional text in the HUD while the key is held.
3. `transcribe.py::transcribe_detailed` → Groq cloud Whisper (`whisper-large-v3-turbo`) first, local `faster-whisper` as fallback (engine `auto`).
4. `format.py::clean_transcript` → tier 1 Groq chat model, tier 2 local Ollama, tier 3 regex rules. Dictionary replacements run before and after.
5. `inject.py` pastes the text at the cursor.

The log line to look at when debugging speed/accuracy: `[AUDIO] … | [STT:<engine>] … | [FORMAT] … | Total` in `~/.config/imla/imla.log`.

## 2. Module map

| File | Role |
| :--- | :--- |
| `imla/main.py` | Entry point, CLI (`--settings`, `--list-devices`, `--device`, `--config`), state wiring, config hot-reload, tray + HUD. |
| `imla/transcribe.py` | Engines: `transcribe_groq`, local faster-whisper, `transcribe_detailed` (returns engine, latency, fallback reason), circuit breaker, `test_cloud`, `build_initial_prompt` (vocabulary hint). |
| `imla/format.py` | Cleanup tiers, `SYSTEM_PROMPT`, `build_system_prompt`, retention guard + length guard, 429 model rotation (`GROQ_FALLBACK_MODELS`), `list_groq_chat_models`. |
| `imla/config.py` | `default_config()`, load/save (atomic), `get_vocabulary()`. Config lives at `~/.config/imla/config.json`. |
| `imla/usage.py` | Daily token / audio-second counters in `~/.config/imla/usage.json`. |
| `imla/settings.py` | Tkinter Settings window (tabs: General, Transcription, Cleanup, Dictionary, Vocabulary). Runs as a **separate process** (`--settings`). |
| `imla/tray.py` | System tray (pystray; `NSStatusItem` on macOS). Menu: status, Quick Mute, Settings…, Quit. `notify()` shows the "Imla is ready" toast. |
| `imla/keychain.py` | API key from OS keychain, else `.env`; `import_env_key_if_missing()` copies `.env` key into the keychain on first run. |
| `imla/startup.py` | Start-on-login (Windows Run key, macOS LaunchAgent `com.imla.dictation`, Linux XDG autostart). |
| `imla/launcher.py` | Command used to relaunch Imla/Settings (`pythonw.exe` on Windows, frozen-aware). |
| `imla/single_instance.py` | Named mutex / lock so only one dictation process owns the hotkey. |
| `imla/applog.py` | Rotating file log `~/.config/imla/imla.log`, excepthook. |
| `imla/hud.py`, `audio.py`, `hotkey.py`, `inject.py`, `preview.py`, `dictionary.py` | HUD overlay, capture, global hotkey, paste, Vosk preview, personal dictionary. |
| `scripts/install_windows.ps1`, `install_linux.sh`, `install_macos.sh`, `uninstall_windows.ps1` | One-time installers that create searchable launchers (Imla + Imla Settings). |
| `tests/` | `unittest` suite plus `bench_stt.py` (local vs cloud benchmark) and `format_smoke.py`. |

## 3. What was done and why (history)

Origin: the app was slow (12–15 s per dictation) and missed words, especially technical ones. Diagnosis from the app log: a large local Whisper model on CPU (9–22 s), no vocabulary hint, a cleanup step that deleted words, clipping at key release, and a wasted 404 call to a dead Groq model.

**Phase A: accuracy and speed**
- Local model default `small.en`, `beam_size=3`, `initial_prompt` built from vocabulary, `condition_on_previous_text=False`, model preloaded at startup.
- Cleanup prompt rewritten to fix grammar without dropping words; `_retention_ok` rejects LLM output that loses ≥30% of content words; the regex tier no longer strips "like"/"actually".
- Groq models that return 404 are remembered and skipped. Default chat model changed from `llama-3.3-70b-versatile` (not available to this account) to `openai/gpt-oss-20b`.
- Larger default technical dictionary; `vocabulary` config list feeds both Whisper and the cleanup prompt.

**Phase B: cloud Whisper with local fallback**
- Engine `auto` (default) | `cloud` | `local`. Cloud skipped for clips <0.4 s or silent buffers. Breaker: 3 consecutive failures pause cloud for 300 s; a 429 honours `retry-after`.
- Measured with a synthetic 9.5 s clip: cloud STT 0.58 s + cleanup 0.53 s = 1.1 s total (versus 6–8 s local). With an invalid key it fell back to local in 2.2 s and still returned text.

**Phase C: free-tier protection**
- Chat 429 rotates `openai/gpt-oss-20b` → `openai/gpt-oss-120b` → `qwen/qwen3.8-27b`; exhausted models are skipped until retry-after or next UTC midnight. Usage is shown in Settings → Cleanup.
- Groq free-tier limits seen in the account console (re-check, they change): Whisper turbo 20 RPM / 2K RPD / 28.8K audio-seconds per day; chat models 30 RPM / 1K RPD / 200K tokens per day each. Tokens per day is the real bottleneck (~150 dictations/day per model).

**Phase D: desktop app feel**
- Installer scripts (Windows/Linux/macOS), single-instance guard, file log, key import from `.env` to keychain, tray "Imla is ready" notification, Settings UI for all the options above, README Quick Start section.

## 4. Decisions to respect (and why)

- **Default engine is `auto`**: audio goes to Groq unless the user picks "Local only" in Settings → Transcription. This is a privacy trade-off the owner accepted for speed; keep the note in the UI/README.
- **Short-utterance LLM bypass** (≤6 words) only applies when the grammar toggle is **off**; otherwise short sentences would skip grammar fixes.
- **No PyInstaller exe** yet. Launchers depend on the project folder and its `.venv`.
- **Settings is a separate process**; the running app picks up `config.json` changes within about a second (`reload_config_if_changed`).
- **Secrets**: the Groq key lives in `.env` (gitignored) or the OS keychain. Never write it to config, logs or this file.
- **Never commit or push unless the owner asks.**

## 5. Resume on a new machine (git clone)

```bash
git clone <repo-url> imla && cd imla
uv sync --python 3.12          # add --extra wayland on Linux Wayland
# put the key in .env:  GROQ_API_KEY=gsk_...   (or paste it in Settings → Cleanup)
uv run python -m imla.main     # run once from the terminal to see logs
bash scripts/install_linux.sh  # or install_macos.sh / scripts\install_windows.ps1
```

Linux needs system packages (`libportaudio2 python3-tk xclip xdotool`; see README). The first local-model use downloads Whisper weights to `~/.cache/huggingface`; Vosk downloads on first preview.

Verify the setup:

```bash
PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests   # expect 103 tests, 1 known error
uv run python tests/bench_stt.py --engine groq --record 8            # needs the key and a mic
```

## 6. Known issues and gotchas

- `tests/test_preview_and_hud.py::test_provisional_text_truncation` fails (`_fit_text_ns` missing from `hud.py`). It failed before any of the work above; expected until fixed.
- `uv run` can rewrite `uv.lock` on Windows (pyproject `required-environments` now lists Windows too). Do not use `git stash` around it; check `git diff uv.lock` instead.
- `tests/format_smoke.py` prints a non-ASCII arrow; on a Windows console set `PYTHONIOENCODING=utf-8`.
- On Windows the tray icon may sit under the **^** overflow arrow.
- `run.sh` is an old Linux script with a hardcoded path from another machine; superseded by `scripts/install_linux.sh`.
- Saved configs from older versions may still name `llama-3.3-70b-versatile` (one harmless 404 per session) or a big local model such as `medium`; change in Settings.
- Ubuntu on Wayland blocks the global hotkey and pasting; use an Xorg session or the Wayland setup in the README.

## 7. Status of verification

| Area | State |
| :--- | :--- |
| Unit tests | 102 of 103 pass (see known issue above). |
| Cloud STT + cleanup live call | Verified on Windows with a synthetic clip. Not benchmarked with real microphone speech. |
| Windows launchers, tray Quit, ready toast | Verified by the owner on Windows. |
| Start-on-login | Registry command only string-tested; not rebooted. |
| Linux and macOS installers | Written, `bash -n` syntax-checked only. Never run. macOS permission persistence untested. |
| Settings window layout | Seen working by the owner; no formal review of small screens. |

## 8. Open work (suggested next steps)

1. Run `install_linux.sh` on Ubuntu and `install_macos.sh` on a Mac; fix whatever breaks and update section 7.
2. Add the owner's technical-term list to Settings → Vocabulary (or defaults in `config.py`).
3. Measure real-microphone accuracy: `tests/bench_stt.py --engine both --record 8`.
4. Instant "Starting…" feedback before heavy imports (small splash window); "already running" notice on a second launch.
5. Optional PyInstaller build / installer; app icon (`assets/imla.ico`, `.icns`, `.png` are looked up by the installers but not present).
6. Pre-roll audio capture (`pre_roll_ms` exists in config but is 0 / unused).
7. Fix the `_fit_text_ns` HUD test.

## 9. How to maintain this file

After any meaningful change: update sections 2 (module map), 3 (history, one short bullet with the reason), 7 (what is verified) and 8 (remove finished items), then bump "Last updated". Record decisions and gotchas that the code cannot show by itself; do not paste code, secrets, API keys or personal data.
