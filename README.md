# Likhari

> 🎥 **Video Walkthrough & Explanation:** [Watch on YouTube](https://youtu.be/nHz_IqXA1CU)

A fast, cross-platform push-to-talk voice dictation desktop app with local speech recognition, live streaming preview, and 3-tier transcript cleanup.

Hold **Right Option / Right Alt** to speak, and release to automatically transcribe, clean up, and paste the text at your active cursor without stealing window focus.

---

## Current Status

| Platform | Status | Implementation Details |
| :--- | :--- | :--- |
| **macOS** (Apple Silicon & Intel) | **Ready & Verified** | Native `PyObjC` (`NSWindow` / `NSView`) non-activating floating HUD, native `NSStatusItem` menu bar icon, `pbcopy`/`pbpaste` clipboard preservation, `AXIsProcessTrusted` Accessibility prompt, and `LaunchAgent` login item. |
| **Windows** (10 / 11) | **Supported** | Non-activating `Tkinter` + `Win32` (`WS_EX_NOACTIVATE`) floating HUD, `pystray` system tray, `win32clipboard` (`CF_UNICODETEXT`) paste, `Right Alt` + `AltGr` international layout support, and `HKCU\...\Run` login registry integration. |
| **Linux** (X11 & Wayland) | **Supported** | `Tkinter` floating HUD with compositor-safe withdraw/deiconify, `pystray` tray (with headless fallback), `xclip`/`xsel` (X11) and `wl-clipboard` + `wtype`/`ydotool` (Wayland) injection, optional `evdev` global hotkey listener for Wayland, and XDG `~/.config/autostart/likhari.desktop`. |

### Core Features

- **Two-Model Speech Pipeline**:
  - **Live Provisional Preview (`Vosk`)**: Streams low-latency partial transcripts in the bottom half of the expanded HUD pill while you hold the hotkey (auto-downloaded and cached at `~/.cache/likhari/models/vosk-model-small-en-us-0.15`).
  - **Final Transcription (`faster-whisper`)**: Runs locally on CPU (`int8`, `vad_filter=False` so quiet or wireless mics are never clipped). Supports live switching between `tiny`, `base`, `small`, and `medium` models.
- **3-Tier Transcript Cleanup**:
  - **Tier 1 — Groq Cloud (`qwen/qwen3.8-27b` by default)**: Ultra-fast punctuation, capitalization, and filler-word cleanup with a strict `1.5x` word-count hallucination guard.
  - **Tier 2 — Local Ollama (`llama3.2:3b` at `http://localhost:11434`)**: Automatic local LLM fallback if offline or if no Groq key is configured.
  - **Tier 3 — Deterministic Rule Engine**: Instant zero-dependency offline fallback that strips filler words (`um`, `uh`, `like`, `you know`), maps spoken punctuation (`comma`, `period`, `question mark`, `open/close quote`), and fixes sentence casing.
- **Personal Dictionary**: Case-insensitive, word-boundary-safe replacements applied after cleanup (e.g., `"pie torch" → "PyTorch"`, `"kubernetes" → "K8s"`).
- **Hardware Audio Resilience**:
  - Opens microphones at their native sample rate and channel count, downmixes stereo to mono, and resamples to 16 kHz via polyphase filtering (`scipy.signal.resample_poly`).
  - **Dead Wireless Dongle / Silence Detection**: Detects 1.5 seconds of continuous 100% digital silence (`peak < 1e-6`) or PortAudio disconnect errors, reinitializes PortAudio, and automatically switches to a built-in fallback microphone.
- **System Tray & Settings Dashboard**:
  - Menu bar / system tray icon (`🎙` / `🔇`) with live status, **Quick Mute**, **Settings…**, and **Quit**.
  - Multi-tab Tkinter Settings Dashboard (`General`, `Transcription`, `Cleanup`, `Dictionary`) launched in an isolated child process with dirty-state tracking, close guard, live hot-reload, and OS Keychain storage (`keyring`).

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
uv run python -m likhari.main
```
*(Or invoke directly via `~/.local/bin/uv run python -m likhari.main`.)*

#### Required macOS Permissions
macOS requires three permissions for global push-to-talk and synthetic `Cmd+V` pasting. **Important:** When you run Likhari inside a terminal or IDE (such as **Antigravity**, **Terminal.app**, **iTerm2**, or **VS Code**), macOS checks permissions for **that host terminal/IDE application**, not the underlying `python` binary:
1. **Accessibility** *(required to paste text at the cursor)*:
   - On first run (or first dictation), Likhari triggers the native macOS Accessibility prompt.
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
uv run python -m likhari.main
```

#### Windows Notes
- **Hotkey**: Defaults to **Right Alt** (`Key.alt_r`). On international/European layouts where Right Alt is mapped to **AltGr** (`Key.alt_gr`), Likhari recognizes both press and release automatically.
- **Administrator Windows**: Due to Windows UIPI (User Interface Privilege Isolation), a non-elevated Likhari process cannot send `Ctrl+V` into windows running as Administrator (e.g., an elevated PowerShell prompt) unless Likhari is also run as Administrator.

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
sudo apt update && sudo apt install -y libportaudio2 python3-tk xclip

# Fedora
sudo dnf install -y portaudio python3-tkinter xclip

# Arch Linux
sudo pacman -S portaudio tk xclip
```

Then sync and run:
```bash
uv sync --python 3.12
uv run python -m likhari.main
```

#### Option B: Linux with Wayland (Ubuntu, Fedora, Sway, Hyprland, KDE)
Wayland isolates global key events and synthetic keystrokes between windows. For full system-wide push-to-talk and cursor injection under Wayland:

1. **Install system packages** (`wl-clipboard` for clipboard access, plus `wtype` for wlroots compositors like Sway/Hyprland or `ydotool` for GNOME/KDE):
   ```bash
   # Ubuntu / Debian
   sudo apt update && sudo apt install -y libportaudio2 python3-tk wl-clipboard wtype ydotool

   # Fedora
   sudo dnf install -y portaudio python3-tkinter wl-clipboard wtype ydotool
   ```
2. **Grant `/dev/input` read access** so the `evdev` hotkey backend can detect the push-to-talk key globally across native Wayland windows:
   ```bash
   sudo usermod -aG input $USER
   # Log out and log back in (or reboot) for the group change to take effect.
   ```
3. **Sync with the `wayland` extra and run**:
   ```bash
   uv sync --python 3.12 --extra wayland
   uv run python -m likhari.main
   ```
   *(You can also force a specific hotkey backend via `LIKHARI_HOTKEY_BACKEND=evdev` or `LIKHARI_HOTKEY_BACKEND=pynput`.)*

---

## CLI Options

```bash
# Run Likhari (tray + HUD + global hotkey listener)
uv run python -m likhari.main

# List all detected audio input devices and their native sample rates
uv run python -m likhari.main --list-devices

# Lock recording to a specific microphone (by device index or name substring)
uv run python -m likhari.main --device 0
uv run python -m likhari.main --device "MacBook Pro Microphone"

# Open the Settings dashboard standalone
uv run python -m likhari.main --settings

# Use a custom config file path
uv run python -m likhari.main --config /path/to/config.json
```

---

## Configuration & API Keys

### Groq API Key (Tier 1 Cleanup)
Likhari never writes your API key to `config.json`. It resolves `GROQ_API_KEY` in the following order:
1. **OS Keychain** (`keyring` service `Likhari`, account `groq_api_key`) — managed via the **Settings → Cleanup** tab (**Save** / **Test Connection**).
   - **macOS**: Keychain Access
   - **Windows**: Windows Credential Manager
   - **Linux**: Secret Service (GNOME Keyring / KWallet)
2. **Environment / `.env` fallback**: Create a `.env` file in the project root:
   ```ini
   GROQ_API_KEY=gsk_your_api_key_here
   ```

### Config File (`~/.config/likhari/config.json`)
Settings are persisted atomically to `~/.config/likhari/config.json` across all platforms:
```json
{
  "hotkey": "Key.alt_r",
  "pre_roll_ms": 0,
  "input_device": null,
  "overlay_enabled": true,
  "start_on_login": false,
  "transcription": {
    "model": "base",
    "preview_enabled": true
  },
  "format": {
    "tier": "auto",
    "groq_model": "qwen/qwen3.8-27b",
    "ollama_model": "llama3.2:3b"
  },
  "dictionary": [
    ["kubernetes", "K8s"],
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
# Run the full unit test suite (19 tests)
uv run python -m unittest discover -s tests -v

# Run the 3-tier formatting & length-guard smoke test
uv run python tests/format_smoke.py

# Run the live HUD + Vosk streaming preview animation smoke test
uv run python tests/hud_smoke.py
```

---

## Project Layout

- `likhari/main.py` — Application entrypoint, state orchestration, live config watcher, and CLI.
- `likhari/audio.py` — Resilient PortAudio capture, stereo-to-mono downmix, 16 kHz polyphase resampling, silence detection, and automatic fallback mic switching.
- `likhari/preview.py` — Asynchronous `Vosk` streaming recognizer with bounded frame queue (`maxsize=8`), rate-limited callbacks (`<=15 Hz`), and automatic model download/caching.
- `likhari/transcribe.py` — Lazy-loaded, hot-swappable `faster-whisper` (`int8` CPU) transcription wrapper.
- `likhari/format.py` — 3-tier transcript cleanup (`Groq` → `Ollama` → offline `Rule` engine) with `1.5x` word-count hallucination guard.
- `likhari/dictionary.py` — Case-insensitive regex word-boundary personal dictionary replacements.
- `likhari/hotkey.py` — Global push-to-talk listener (`pynput` across macOS/Windows/X11 + `evdev` on Linux Wayland).
- `likhari/inject.py` — Clipboard backup, paste simulation, and restoration across macOS, Windows, X11, and Wayland.
- `likhari/hud.py` — Non-activating floating pill HUD (`PyObjC` on macOS, `Tkinter` on Windows & Linux).
- `likhari/tray.py` — System tray icon and menu (`NSStatusItem` on macOS, `pystray` on Windows & Linux with headless fallback).
- `likhari/settings.py` — Four-tab `Tkinter` Settings dashboard with dirty tracking, close guard, and Vosk model downloader.
- `likhari/keychain.py` — OS keychain wrapper (`keyring` + `.env` fallback) and Groq API connection tester.
- `likhari/startup.py` — Launch-on-login manager (`LaunchAgent` plist on macOS, `Run` registry key on Windows, XDG `.desktop` entry on Linux).
- `likhari/config.py` — Atomic config loader/saver with key-stripping defense-in-depth.
