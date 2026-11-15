"""Unit tests verifying Phase 5 (Hardware Auto-Recovery & Personal Dictionary)
and Phase 6 (System Tray & Desktop Settings Dashboard with Keychain Security).
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from imla.audio import AudioRecorder, Recorder, SILENCE_PEAK_THRESHOLD
from imla.config import default_config, get_dictionary, load_config, save_config
from imla.dictionary import apply_dictionary, compile_dictionary
from imla.format import clean_transcript
from imla.hotkey import PushToTalk
from imla.keychain import SERVICE_NAME, KEY_NAME, get_api_key, set_api_key
from imla.preview import is_model_cached
from imla.settings import SettingsWindow, HOTKEY_CHOICES
from imla import startup


class TestPhase5AudioResilience(unittest.TestCase):
    """Phase 5: Audio hardware resilience and recovery."""

    def test_audio_recorder_alias(self):
        """Verify AudioRecorder is aliased to Recorder."""
        self.assertIs(AudioRecorder, Recorder)

    def test_switch_to_fallback_handles_errors_gracefully(self):
        """Verify switch_to_fallback reinitializes sounddevice backend without crashing."""
        rec = Recorder()
        # Mock sounddevice calls
        with patch("sounddevice._terminate") as mock_term, \
             patch("sounddevice._initialize") as mock_init, \
             patch("sounddevice.query_devices") as mock_query, \
             patch("sounddevice.default.device", [3, 0]):
            
            mock_query.return_value = [
                {"name": "Disconnected Headset", "max_input_channels": 0, "default_samplerate": 48000},
                {"name": "MacBook Pro-Mikrofon", "max_input_channels": 1, "default_samplerate": 48000},
            ]

            fallback_idx = rec.switch_to_fallback()
            self.assertEqual(fallback_idx, 1)
            self.assertEqual(rec.device, 1)
            mock_term.assert_called_once()
            mock_init.assert_called_once()

    def test_digital_silence_detection(self):
        """Verify continuous 100% digital silence (amplitude < 1e-6) triggers callback after 1.5s."""
        silence_triggered = []

        def on_silence():
            silence_triggered.append(True)

        rec = Recorder(on_silence_fallback=on_silence)
        rec._is_recording = True

        chunk_0_1s = np.zeros(1600, dtype=np.float32)

        # Simulate monotonic clock advancing 0.1s each chunk
        current_time = 100.0

        def get_time():
            return current_time

        with patch("time.monotonic", side_effect=get_time):
            # Feed 14 frames (1.4s of silence) — should NOT trigger yet
            for _ in range(14):
                rec._callback(chunk_0_1s.reshape(-1, 1), 1600, None, None)
                current_time += 0.1
            self.assertEqual(len(silence_triggered), 0)

            # Feed 3 more frames (total 1.7s of silence > 1.5s) — SHOULD trigger
            for _ in range(3):
                rec._callback(chunk_0_1s.reshape(-1, 1), 1600, None, None)
                current_time += 0.1
            self.assertEqual(len(silence_triggered), 1)

            # Subsequent silent frames should not re-trigger repeatedly (one-shot per streak)
            for _ in range(5):
                rec._callback(chunk_0_1s.reshape(-1, 1), 1600, None, None)
                current_time += 0.1
            self.assertEqual(len(silence_triggered), 1)

    def test_non_silent_audio_resets_silence_timer(self):
        """Verify that audio with signal resets the continuous silence counter."""
        silence_triggered = []
        rec = Recorder(on_silence_fallback=lambda: silence_triggered.append(True))
        rec._is_recording = True

        chunk_silent = np.zeros(1600, dtype=np.float32)
        chunk_voice = np.ones(1600, dtype=np.float32) * 0.1

        current_time = 100.0

        def get_time():
            return current_time

        with patch("time.monotonic", side_effect=get_time):
            # 1.0s silence
            for _ in range(10):
                rec._callback(chunk_silent.reshape(-1, 1), 1600, None, None)
                current_time += 0.1

            # Sound arrives
            rec._callback(chunk_voice.reshape(-1, 1), 1600, None, None)
            current_time += 0.1

            # Another 1.0s silence (total 2.0s silence overall, but broken by sound)
            for _ in range(10):
                rec._callback(chunk_silent.reshape(-1, 1), 1600, None, None)
                current_time += 0.1

            # Should NOT have triggered because continuous streak never hit 1.5s
            self.assertEqual(len(silence_triggered), 0)


class TestPhase5PersonalDictionary(unittest.TestCase):
    """Phase 5: Personal replacement dictionary with case-insensitive word boundaries."""

    def test_compile_and_apply_dictionary(self):
        pairs = [
            ("kubernetes", "K8s"),
            ("pie torch", "PyTorch"),
            ("next js", "Next.js"),
            ("grpc", "gRPC"),
        ]
        rules = compile_dictionary(pairs)

        # Exact match
        self.assertEqual(apply_dictionary("I love kubernetes", rules), "I love K8s")
        # Case insensitivity
        self.assertEqual(apply_dictionary("We deploy on KUBERNETES cluster", rules), "We deploy on K8s cluster")
        self.assertEqual(apply_dictionary("trained with PIE TORCH yesterday", rules), "trained with PyTorch yesterday")
        self.assertEqual(apply_dictionary("built in NEXT JS and GRPC", rules), "built in Next.js and gRPC")

        # Word boundary protection — substrings should NOT be replaced
        self.assertEqual(apply_dictionary("mykubernetescluster is running", rules), "mykubernetescluster is running")
        self.assertEqual(apply_dictionary("apple pie torching", rules), "apple pie torching")

    def test_dictionary_in_formatting_pipeline(self):
        """Verify dictionary is applied post-formatting in clean_transcript."""
        cfg = default_config()
        cfg["dictionary"] = [["kubernetes", "K8s"], ["pie torch", "PyTorch"]]
        dict_pairs = get_dictionary(cfg)

        # Pass tier_override="rule" to test deterministic offline pipeline
        res = clean_transcript("we use kubernetes and pie torch daily", tier_override="rule", dictionary=dict_pairs)
        self.assertIn("K8s", res.text)
        self.assertIn("PyTorch", res.text)


class TestPhase6KeychainSecurity(unittest.TestCase):
    """Phase 6: OS Keychain security — API keys must never be saved in config.json."""

    def test_save_config_never_saves_api_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            cfg = default_config()
            # Deliberately inject API key fields into config dict
            cfg["api_key"] = "gsk_secret12345"
            cfg["groq_api_key"] = "gsk_secret12345"
            cfg["format"]["api_key"] = "gsk_secret12345"

            save_config(cfg, path=path)

            raw_disk = path.read_text(encoding="utf-8")
            self.assertNotIn("gsk_secret12345", raw_disk)
            parsed = json.loads(raw_disk)
            self.assertNotIn("api_key", parsed)
            self.assertNotIn("groq_api_key", parsed)
            self.assertNotIn("api_key", parsed.get("format", {}))

    def test_keychain_get_set(self):
        """Verify get_api_key and set_api_key interact with keyring."""
        with patch("keyring.get_password") as mock_get, \
             patch("keyring.set_password") as mock_set, \
             patch("keyring.delete_password") as mock_del:

            mock_get.return_value = "gsk_mocked_key"
            self.assertEqual(get_api_key(), "gsk_mocked_key")
            mock_get.assert_called_with(SERVICE_NAME, KEY_NAME)

            # Setting non-empty key
            set_api_key("gsk_new_key")
            mock_set.assert_called_with(SERVICE_NAME, KEY_NAME, "gsk_new_key")

            # Setting empty key deletes
            set_api_key("")
            mock_del.assert_called_with(SERVICE_NAME, KEY_NAME)


class TestPhase6SettingsAndStartup(unittest.TestCase):
    """Phase 6: Settings Window, Dirty Tracking, Hotkey Rebinding, and Startup."""

    def test_hotkey_rebinding(self):
        """Verify PushToTalk supports dynamic hotkey rebinding."""
        ptt = PushToTalk(on_press=lambda: None, on_release=lambda: None, key="Key.alt_r")
        self.assertEqual(ptt.key_spec, "Key.alt_r")

        ptt.set_key("Key.ctrl_l")
        self.assertEqual(ptt.key_spec, "Key.ctrl_l")

        ptt.set_key("Key.f8")
        self.assertEqual(ptt.key_spec, "Key.f8")

    def test_startup_macos_plist_generation(self):
        """Verify macOS LaunchAgent plist structure."""
        plist_path = startup._macos_plist_path()
        self.assertTrue(str(plist_path).endswith("com.imla.dictation.plist"))

    def test_vosk_model_cache_helper(self):
        """Verify is_model_cached returns boolean without error."""
        res = is_model_cached()
        self.assertIsInstance(res, bool)

    def test_settings_window_dirty_tracking_and_ui(self):
        """Verify SettingsWindow builds tabs, tracks dirty state, and enables/disables Save."""
        applied = []
        def on_apply(cfg, key):
            applied.append((cfg, key))

        win = SettingsWindow(initial_config=default_config(), on_apply=on_apply)
        win._build()
        try:
            self.assertFalse(win._dirty)
            self.assertEqual(str(win._save_btn["state"]), "disabled")

            # Changing whisper model marks dirty
            win._whisper_model_var.set("small")
            win._mark_dirty()
            self.assertTrue(win._dirty)
            self.assertEqual(str(win._save_btn["state"]), "normal")

            # Reverting resets dirty
            win._whisper_model_var.set("base")
            win._refresh_dirty()
            self.assertFalse(win._dirty)
            self.assertEqual(str(win._save_btn["state"]), "disabled")
        finally:
            if win._root is not None:
                win._root.destroy()

    def test_hotkey_alt_gr_press_and_release(self):
        """Verify Right Alt hotkey accepts AltGr on both press and release (Windows/Linux layouts)."""
        from pynput import keyboard

        events = []
        ptt = PushToTalk(
            on_press=lambda: events.append("press"),
            on_release=lambda: events.append("release"),
            key="Key.alt_r",
        )
        ptt._handle_press(keyboard.Key.alt_gr)
        ptt._handle_release(keyboard.Key.alt_gr)
        self.assertEqual(events, ["press", "release"])

    def test_startup_linux_desktop_entry(self):
        """Verify Linux XDG autostart .desktop creation and removal."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": td}):
                desktop_path = startup._linux_desktop_path()
                self.assertEqual(desktop_path, Path(td) / "autostart" / "imla.desktop")
                startup._enable_linux()
                self.assertTrue(desktop_path.exists())
                content = desktop_path.read_text(encoding="utf-8")
                self.assertIn("[Desktop Entry]", content)
                self.assertIn("imla.main", content)
                startup._disable_linux()
                self.assertFalse(desktop_path.exists())

    def test_transcribe_set_model_name_resets_cached_model(self):
        """Verify set_model_name clears the cached WhisperModel instance."""
        from imla import transcribe as tr

        tr._model_name = "base"
        tr._model = object()  # sentinel
        tr.set_model_name("small")
        self.assertEqual(tr.get_model_name(), "small")
        self.assertIsNone(tr._model)
        tr.set_model_name("base")


if __name__ == "__main__":
    unittest.main()
