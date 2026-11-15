# Imla

A fast, cross-platform push-to-talk voice dictation desktop app with local speech recognition, live streaming preview, and 3-tier transcript cleanup.

Hold **Right Option / Right Alt** to speak, and release to automatically transcribe, clean up, and paste the text at your active cursor without stealing window focus.

> **Continuing this project in a new session or on another machine?** Read [`context.md`](context.md): it records what was built and why, decisions, known issues, verification status and next steps.

---

## Quick Start: One-Click App (run the installer once)

Do this **one time** per computer. After it, Imla is searchable like any other app, you never need a terminal or the project folder path again, and you can start, open Settings and quit with the mouse.

**0. Prerequisite (once):** install `uv` and create the environment from the project folder (see the platform sections below for the `uv` install command and Linux/macOS system packages):

```bash
uv sync --python 3.12
```

**1. Create your launchers (once):**

| Platform | Command (run from the project folder) | What you get |
| :--- | :--- | :--- |
| **Windows 10/11** | `powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1` | **Imla** and **Imla Settings** on the Desktop and in the Start Menu. Add `-StartOnLogin` to launch at sign-in. |
| **Ubuntu / Linux** | `bash scripts/install_linux.sh` | **Imla** and **Imla Settings** in the app menu and on the Desktop. Add `--start-on-login` to launch at login. |
| **macOS** | `bash scripts/install_macos.sh` | `Imla.app` and `Imla Settings.app` in `~/Applications` (Spotlight, Launchpad, Dock). Add `--start-on-login` to launch at login. |

Uninstall: `scripts\uninstall_windows.ps1`, or `bash scripts/install_linux.sh --uninstall`, or `bash scripts/install_macos.sh --uninstall`.

**2. Start (one click):**
- **Windows**: press the Windows key, type `Imla`, press Enter (or double-click the Desktop icon).
- **Ubuntu**: press Super, type `Imla`, press Enter.
- **macOS**: press Cmd+Space, type `Imla`, press Enter. Drag it to the Dock to pin it.

A notification **"Imla is ready"** appears after a few seconds and an icon shows in the system tray / menu bar. Hold **Right Alt / Right Option**, speak, release.

**3. Open Settings (one click):**
- Search for **Imla Settings** the same way, **or** right-click the tray icon and choose **Settings…**.
- Close the Settings window with the **X** (it asks if there are unsaved changes). This does *not* stop Imla.

**4. Quit (one click), any of these:**
- Right-click the Imla tray icon (on Windows it may be under the **^** arrow) and choose **Quit**.
- **Any platform, even without a tray menu:** open Imla Settings and click **Quit Imla** (bottom left).
- **Ubuntu:** search for **Quit Imla** (the Linux installer creates it), or run `pkill -f imla.main`. On some Ubuntu setups the tray icon has no menu and a click just opens Settings; use the options above.
- Terminal: `uv run python -m imla.main --quit`.

Starting Imla twice is safe: the second copy exits and the first keeps running.

**Notes**
- The launchers point at this project folder and its `.venv`; do not move or delete the folder (re-run the installer if you do).
- **Ubuntu must use an X11 session** ("Ubuntu on Xorg", gear icon at the login screen). Wayland blocks the global hotkey and pasting into other apps unless you follow the Wayland setup below.
- **macOS** asks for Microphone, Accessibility and Input Monitoring permissions on first run (see the macOS section). If it says the app is from an unidentified developer, right-click it and choose **Open** once.
- The Windows installer has been run on Windows; the Linux and macOS installers were written to match it but have only been syntax-checked so far. Please report any problem.
- Logs are written to `~/.config/imla/imla.log` (Settings → General → **Open log file**).

---

## Current Status

| Platform | Status | Implementation Details |
| :--- | :--- | :--- |
| **macOS** (Apple Silicon & Intel) | **Ready & Verified** | Native `PyObjC` (`NSWindow` / `NSView`) non-activating floating HUD, native `NSStatusItem` menu bar icon, `pbcopy`/`pbpaste` clipboard preservation, `AXIsProcessTrusted` Accessibility prompt, and `LaunchAgent` login item. |
| **Windows** (10 / 11) | **Supported** | Non-activating `Tkinter` + `Win32` (`WS_EX_NOACTIVATE`) floating HUD, `pystray` system tray, `win32clipboard` (`CF_UNICODETEXT`) paste, `Right Alt` + `AltGr` international layout support, and `HKCU\...\Run` login registry integration. |
| **Linux** (X11 & Wayland) | **Supported** | `Tkinter` floating HUD with compositor-safe withdraw/deiconify, `pystray` tray (with headless fallback), `xclip`/`xsel` (X11) and `wl-clipboard` + `wtype`/`ydotool` (Wayland) injection, optional `evdev` global hotkey listener for Wayland, and XDG `~/.config/autostart/imla.desktop`. |

### Core Features

- **Speech Pipeline with Cloud + Local Fallback**:
  - **Live Provisional Preview (`Vosk`)**: Streams low-latency partial transcripts in the bottom half of the expanded HUD pill while you hold the hotkey (auto-downloaded and cached at `~/.cache/imla/models/vosk-model-small-en-us-0.15`).
  - **Final Transcription, engine `auto` (default)**: sends the recording to **Groq cloud Whisper (`whisper-large-v3-turbo`)** (about 0.5–1 s) and automatically **falls back to local `faster-whisper`** (CPU, `int8`) if there is no key, no network, a timeout or a rate limit. After repeated cloud failures the cloud is skipped for a few minutes so an outage adds no delay. Choose **Local only** in Settings → Transcription if you do not want audio to leave your computer. In `auto`/`cloud` mode your recorded audio and transcript text are sent to Groq.
  - **Technical vocabulary**: terms from Settings → **Vocabulary** and your dictionary are passed to Whisper and the cleanup model so words like `PyTorch`, `Kubernetes`, `gRPC` come out right.
- **Built for software engineers, tuned for Java / Spring Boot**: ships with editable vocabulary packs in `imla/vocab/` (about 210 Java/Spring terms, 110 general software terms, and about 240 `spoken => Written` replacements). Examples: "at transactional" → `@Transactional`, "j p a repository" → `JpaRepository`, "null pointer exception" → `NullPointerException`, "pom dot xml" → `pom.xml`, "j unit" → `JUnit`, "eighty eighty" → `8080`. The cleanup model is told you are a Java/Spring Boot engineer so it keeps class names, annotations, packages and file names exactly. Turn packs on/off in **Settings → Vocabulary**; your own terms and dictionary always take priority. To add words, edit the `.txt` files in `imla/vocab/` (spelling hints one per line, replacements as `spoken => Written`) or use the Settings tabs.
- **Grammar fixing** (Settings → Cleanup → *Fix grammar mistakes*): the cleanup model corrects grammar and tense while a retention guard prevents it from dropping or rewriting your words.
- **Free-tier protection**: cleanup rotates `openai/gpt-oss-20b` → `gpt-oss-120b` → `qwen/qwen3.8-27b` on rate limits, and **Settings → Cleanup → Usage today** shows tokens and audio seconds used against the daily Groq limits.
- **3-Tier Transcript Cleanup**:
  - **Tier 1 — Groq Cloud (`openai/gpt-oss-20b` by default; pick any model in Settings with *Refresh*)**: Ultra-fast punctuation, capitalization, grammar and filler-word cleanup with a word-count hallucination guard.
  - **Tier 2 — Local Ollama (`llama3.2:3b` at `http://localhost:11434`)**: Automatic local LLM fallback if offline or if no Groq key is configured.
  - **Tier 3 — Deterministic Rule Engine**: Instant zero-dependency offline fallback that strips filler words (`um`, `uh`, `like`, `you know`), maps spoken punctuation (`comma`, `period`, `question mark`, `open/close quote`), and fixes sentence casing.
- **Personal Dictionary**: Case-insensitive, word-boundary-safe replacements applied after cleanup (e.g., `"pie torch" → "PyTorch"`, `"kubernetes" → "K8s"`).
- **Hardware Audio Resilience**:
  - Opens microphones at their native sample rate and channel count, downmixes stereo to mono, and resamples to 16 kHz via polyphase filtering (`scipy.signal.resample_poly`).
  - **Dead Wireless Dongle / Silence Detection**: Detects 1.5 seconds of continuous 100% digital silence (`peak < 1e-6`) or PortAudio disconnect errors, reinitializes PortAudio, and automatically switches to a built-in fallback microphone.
- **System Tray & Settings Dashboard**:
  - Menu bar / system tray icon (`🎙` / `🔇`) with live status, **Quick Mute**, **Settings…**, and **Quit**.
  - Multi-tab Tkinter Settings Dashboard (`General`, `Transcription`, `Cleanup`, `Dictionary`, `Vocabulary`) with engine/model pickers, **Test cloud**, usage meters and log shortcuts, launched in an isolated child process with dirty-state tracking, close guard, live hot-reload, and OS Keychain storage (`keyring`).

---

## Setup & Commands by Platform

### 1. macOS

#### Install `uv` (if not already installed)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

#### Sync environment & run
```bash
uv sync --python 3.12
uv run python -m imla.main
```
*(Or invoke directly via `~/.local/bin/uv run python -m imla.main`.)*

#### Required macOS Permissions
macOS requires three permissions for global push-to-talk and synthetic `Cmd+V` pasting. **Important:** When you run Imla inside a terminal or IDE (such as **Antigravity**, **Terminal.app**, **iTerm2**, or **VS Code**), macOS checks permissions for **that host terminal/IDE application**, not the underlying `python` binary:
1. **Accessibility** *(required to paste text at the cursor)*:
   - On first run (or first dictation), Imla triggers the native macOS Accessibility prompt.
   - Open **System Settings → Privacy & Security → Accessibility** and toggle **ON** your terminal/IDE app (e.g., **Antigravity** or **Terminal**).
   - If you use **Start on Login** (which runs via `LaunchAgent` outside a terminal), also add the Python binary shown in the startup log (`~/.local/share/uv/python/.../bin/python3.12`).
2. **Input Monitoring** *(required to detect the global Right Option hotkey)*:
   - Open **System Settings → Privacy & Security → Input Monitoring** and toggle **ON** your terminal/IDE app.
3. **Microphone**:
   - Accept the macOS microphone prompt when first holding the hotkey.

---

### 2. Windows (10 / 11)

#### Install `uv` (PowerShell)
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

#### Sync environment & run
```powershell
uv sync --python 3.12
uv run python -m imla.main
```

#### Windows Notes
- **Hotkey**: Defaults to **Right Alt** (`Key.alt_r`). On international/European layouts where Right Alt is mapped to **AltGr** (`Key.alt_gr`), Imla recognizes both press and release automatically.
- **Administrator Windows**: Due to Windows UIPI (User Interface Privilege Isolation), a non-elevated Imla process cannot send `Ctrl+V` into windows running as Administrator (e.g., an elevated PowerShell prompt) unless Imla is also run as Administrator.

---

### 3. Linux (X11 & Wayland)

#### Install `uv`
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

#### Option A: Linux with X11
Install PortAudio, Tkinter, and an X11 clipboard utility (`xclip` or `xsel`):

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y libportaudio2 python3-tk xclip xdotool

# Fedora
sudo dnf install -y portaudio python3-tkinter xclip xdotool

# Arch Linux
sudo pacman -S portaudio tk xclip xdotool
```

Then sync and run:
```bash
uv sync --python 3.12
uv run python -m imla.main
```

#### Option B: Linux with Wayland (Ubuntu, Fedora, Sway, Hyprland, KDE)
Wayland isolates global key events and synthetic keystrokes between windows. For full system-wide push-to-talk and cursor injection under Wayland:

1. **Install system packages** (`wl-clipboard` for clipboard access, plus `wtype` for wlroots compositors like Sway/Hyprland or `ydotool` for GNOME/KDE):
   ```bash
   # Ubuntu / Debian
   sudo apt update && sudo apt install -y libportaudio2 python3-tk wl-clipboard wtype ydotool xdotool

   # Fedora
   sudo dnf install -y portaudio python3-tkinter wl-clipboard wtype ydotool xdotool
   ```
2. **Grant `/dev/input` read access** so the `evdev` hotkey backend can detect the push-to-talk key globally across native Wayland windows:
   ```bash
   sudo usermod -aG input $USER
   # Log out and log back in (or reboot) for the group change to take effect.
   ```
3. **Sync with the `wayland` extra and run**:
   ```bash
   uv sync --python 3.12 --extra wayland
   uv run python -m imla.main
   ```
   *(You can also force a specific hotkey backend via `LIKHARI_HOTKEY_BACKEND=evdev` or `LIKHARI_HOTKEY_BACKEND=pynput`.)*

---

## CLI Options

```bash
# Run Imla (tray + HUD + global hotkey listener)
uv run python -m imla.main

# List all detected audio input devices and their native sample rates
uv run python -m imla.main --list-devices

# Lock recording to a specific microphone (by device index or name substring)
uv run python -m imla.main --device 0
uv run python -m imla.main --device "MacBook Pro Microphone"

# Ask the running Imla to quit (also used by the "Quit Imla" launcher/button)
uv run python -m imla.main --quit

# Open the Settings dashboard standalone
uv run python -m imla.main --settings

# Use a custom config file path
uv run python -m imla.main --config /path/to/config.json
```

---

## Configuration & API Keys

### Groq API Key (Tier 1 Cleanup)
Imla never writes your API key to `config.json`. It resolves `GROQ_API_KEY` in the following order:
1. **OS Keychain** (`keyring` service `Imla`, account `groq_api_key`) — managed via the **Settings → Cleanup** tab (**Save** / **Test Connection**).
   - **macOS**: Keychain Access
   - **Windows**: Windows Credential Manager
   - **Linux**: Secret Service (GNOME Keyring / KWallet)
2. **Environment / `.env` fallback**: Create a `.env` file in the project root:
   ```ini
   GROQ_API_KEY=gsk_your_api_key_here
   ```

### Config File (`~/.config/imla/config.json`)
You normally never edit this by hand; use the Settings window. On first run the Groq key from `.env` is copied into the OS keychain, so the one-click launchers work from any folder. Settings are persisted atomically to `~/.config/imla/config.json` across all platforms (`engine` is `auto`, `cloud` or `local`; `model` is the local fallback):
```json
{
  "hotkey": "Key.alt_r",
  "pre_roll_ms": 0,
  "input_device": null,
  "overlay_enabled": true,
  "start_on_login": false,
  "tail_ms": 250,
  "transcription": {
    "engine": "auto",
    "cloud_model": "whisper-large-v3-turbo",
    "model": "small.en",
    "beam_size": 3,
    "cpu_threads": 0,
    "preview_enabled": true
  },
  "format": {
    "tier": "auto",
    "groq_model": "openai/gpt-oss-20b",
    "ollama_model": "llama3.2:3b",
    "fix_grammar": true
  },
  "vocabulary": ["PyTorch", "Kubernetes"],
  "dictionary": [
    ["kubernetes", "K8S"],
    ["pie torch", "PyTorch"],
    ["next js", "Next.js"],
    ["grpc", "gRPC"]
  ],
  "stats": {
    "total_dictations": 0,
    "total_words": 0
  }
}
```

---

## Running Tests

```bash
# Run the full unit test suite (103 tests; test_provisional_text_truncation is a known pre-existing failure)
uv run python -m unittest discover -s tests -v

# Benchmark local vs cloud transcription on your own clips or microphone
uv run python tests/bench_stt.py --engine both --record 8

# Run the 3-tier formatting & length-guard smoke test
uv run python tests/format_smoke.py

# Run the live HUD + Vosk streaming preview animation smoke test
uv run python tests/hud_smoke.py
```

---

## Project Layout

- `imla/main.py` — Application entrypoint, state orchestration, live config watcher, and CLI.
- `imla/audio.py` — Resilient PortAudio capture, stereo-to-mono downmix, 16 kHz polyphase resampling, silence detection, and automatic fallback mic switching.
- `imla/preview.py` — Asynchronous `Vosk` streaming recogniser with bounded frame queue (`maxsize=8`), rate-limited callbacks (`<=15 Hz`), and automatic model download/caching.
- `imla/transcribe.py` — Lazy-loaded, hot-swappable `faster-whisper` (`int8` CPU) transcription wrapper.
- `imla/format.py` — 3-tier transcript cleanup (`Groq` → `Ollama` → offline `Rule` engine) with `1.5x` word-count hallucination guard.
- `imla/dictionary.py` — Case-insensitive regex word-boundary personal dictionary replacements.
- `imla/hotkey.py` — Global push-to-talk listener (`pynput` across macOS/Windows/X11 + `evdev` on Linux Wayland).
- `imla/inject.py` — Clipboard backup, paste simulation, and restoration across macOS, Windows, X11, and Wayland.
- `imla/hud.py` — Non-activating floating pill HUD (`PyObjC` on macOS, `Tkinter` on Windows & Linux).
- `imla/tray.py` — System tray icon and menu (`NSStatusItem` on macOS, `pystray` on Windows & Linux with headless fallback).
- `imla/settings.py` — Four-tab `Tkinter` Settings dashboard with dirty tracking, close guard, and Vosk model downloader.
- `imla/keychain.py` — OS keychain wrapper (`keyring` + `.env` fallback) and Groq API connection tester.
- `imla/startup.py` — Launch-on-login manager (`LaunchAgent` plist on macOS, `Run` registry key on Windows, XDG `.desktop` entry on Linux).
- `imla/config.py` — Atomic config loader/saver with key-stripping defence-in-depth.
- `imla/usage.py` — Daily token / audio-second counters for the Groq free tier.
- `imla/launcher.py`, `imla/single_instance.py`, `imla/applog.py` — Relaunch command for the Settings process, one-copy-at-a-time guard, and rotating log file.
- `scripts/install_windows.ps1`, `install_linux.sh`, `install_macos.sh` — One-time installers that make Imla searchable and pin-able (see Quick Start).
