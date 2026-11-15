"""Tests for the new Settings window fields (engine, vocabulary, etc.)."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imla.config import default_config  # noqa: E402
from imla import settings as settings_mod  # noqa: E402
from imla.settings import SettingsWindow, parse_vocabulary  # noqa: E402


def _old_style_config():
    cfg = default_config()
    for k in ("engine", "cloud_model", "beam_size", "cpu_threads"):
        cfg["transcription"].pop(k)
    cfg["format"].pop("fix_grammar")
    cfg.pop("tail_ms")
    cfg.pop("vocabulary")
    return cfg


class SettingsUITests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.cfg_path = Path(self._td.name) / "config.json"
        p = patch.object(settings_mod, "get_api_key", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        for name in ("set_api_key",):
            q = patch.object(settings_mod, name, return_value=True)
            q.start()
            self.addCleanup(q.stop)
        self.applied = []

    def make(self, cfg=None):
        win = SettingsWindow(
            initial_config=cfg if cfg is not None else default_config(),
            on_apply=lambda c, k: self.applied.append((c, k)),
            config_path=self.cfg_path,
        )
        try:
            win._build()
        except Exception as exc:  # noqa: BLE001
            if win._root is not None:
                win._root.destroy()
            if "display" in str(exc).lower() or "tcl" in type(exc).__name__.lower():
                raise unittest.SkipTest(f"Tk unavailable: {exc}")
            raise
        self.addCleanup(self._destroy, win)
        return win

    @staticmethod
    def _destroy(win):
        try:
            if win._root is not None:
                win._root.destroy()
        except Exception:  # noqa: BLE001
            pass

    # -- building ---------------------------------------------------------

    def test_builds_with_default_config(self):
        win = self.make()
        self.assertFalse(win._dirty)
        self.assertEqual(win._engine_var.get(), "Auto — cloud first, local fallback")
        self.assertEqual(win._beam_var.get(), "3")
        self.assertEqual(win._tail_var.get(), "250")
        self.assertTrue(win._fix_grammar_var.get())

    def test_builds_with_old_style_config_shows_defaults_and_is_clean(self):
        win = self.make(_old_style_config())
        self.assertFalse(win._dirty)
        self.assertEqual(win._cloud_model_var.get(), "whisper-large-v3-turbo")
        self.assertEqual(win._threads_var.get(), "0")
        self.assertEqual(win._tail_var.get(), "250")
        self.assertTrue(win._fix_grammar_var.get())
        self.assertEqual(win._get_vocab_terms(), [])
        self.assertEqual(str(win._save_btn["state"]), "disabled")

    # -- round trip -------------------------------------------------------

    def test_fields_load_and_round_trip(self):
        cfg = default_config()
        cfg["transcription"].update(
            engine="local", cloud_model="whisper-large-v3", beam_size=5, cpu_threads=4
        )
        cfg["format"]["fix_grammar"] = False
        cfg["tail_ms"] = 400
        cfg["vocabulary"] = ["Kubernetes", "kubernetes", " gRPC ", ""]
        win = self.make(cfg)
        self.assertEqual(win._engine_var.get(), "Local only (audio never leaves this PC)")
        out, _ = win._collect_current()
        self.assertEqual(out["transcription"]["engine"], "local")
        self.assertEqual(out["transcription"]["cloud_model"], "whisper-large-v3")
        self.assertEqual(out["transcription"]["beam_size"], 5)
        self.assertIsInstance(out["transcription"]["beam_size"], int)
        self.assertEqual(out["transcription"]["cpu_threads"], 4)
        self.assertIs(out["format"]["fix_grammar"], False)
        self.assertEqual(out["tail_ms"], 400)
        self.assertIsInstance(out["tail_ms"], int)
        self.assertEqual(out["vocabulary"], ["Kubernetes", "gRPC"])

    def test_edit_and_save_writes_keys(self):
        win = self.make()
        win._engine_var.set("Cloud only")
        win._beam_var.set("2")
        win._threads_var.set("junk")  # invalid -> default 0
        win._tail_var.set("5000")  # clamped
        win._fix_grammar_var.set(False)
        win._vocab_text.insert("1.0", "Foo\nbar\nFOO\n")
        win._save()
        saved, _ = self.applied[-1]
        self.assertEqual(saved["transcription"]["engine"], "cloud")
        self.assertEqual(saved["transcription"]["beam_size"], 2)
        self.assertEqual(saved["transcription"]["cpu_threads"], 0)
        self.assertEqual(saved["tail_ms"], 1000)
        self.assertFalse(saved["format"]["fix_grammar"])
        self.assertEqual(saved["vocabulary"], ["Foo", "bar"])
        self.assertTrue(self.cfg_path.exists())
        self.assertFalse(win._dirty)

    def test_unknown_keys_survive_save(self):
        cfg = default_config()
        cfg["stats"] = {"total_dictations": 7, "total_words": 99}
        cfg["pre_roll_ms"] = 120
        cfg["mystery"] = {"a": 1}
        cfg["transcription"]["extra"] = "keep"
        win = self.make(cfg)
        win._fix_grammar_var.set(False)
        win._save()
        saved, _ = self.applied[-1]
        self.assertEqual(saved["stats"]["total_words"], 99)
        self.assertEqual(saved["pre_roll_ms"], 120)
        self.assertEqual(saved["mystery"], {"a": 1})
        self.assertEqual(saved["transcription"]["extra"], "keep")
        self.assertEqual(saved["dictionary"], cfg["dictionary"])

    # -- dirty tracking ---------------------------------------------------

    def test_dirty_and_revert(self):
        win = self.make()
        win._engine_var.set("Cloud only")
        win._refresh_dirty()
        self.assertTrue(win._dirty)
        self.assertEqual(str(win._save_btn["state"]), "normal")
        win._engine_var.set("Auto — cloud first, local fallback")
        win._refresh_dirty()
        self.assertFalse(win._dirty)

        win._fix_grammar_var.set(False)
        win._refresh_dirty()
        self.assertTrue(win._dirty)
        win._fix_grammar_var.set(True)
        win._refresh_dirty()
        self.assertFalse(win._dirty)

        win._vocab_text.insert("1.0", "NewTerm")
        win._mark_dirty()
        self.assertTrue(win._dirty)
        win._vocab_text.delete("1.0", "end")
        win._refresh_dirty()
        self.assertFalse(win._dirty)

        win._tail_var.set("300")
        self.assertTrue(win._dirty)
        win._tail_var.set("250")
        self.assertFalse(win._dirty)

    # -- lazy handlers ----------------------------------------------------

    def test_test_cloud_worker_missing_or_raising(self):
        win = self.make()
        with patch.dict(sys.modules, {"imla.transcribe": None}):
            ok, msg = win._test_cloud_worker()
        self.assertFalse(ok)
        self.assertTrue(msg)
        import imla.transcribe as tr

        with patch.object(tr, "test_cloud", side_effect=RuntimeError("boom"), create=True):
            ok, msg = win._test_cloud_worker()
        self.assertFalse(ok)
        self.assertIn("boom", msg)
        with patch.object(tr, "test_cloud", return_value=(True, "OK", 0.5), create=True):
            ok, msg = win._test_cloud_worker()
        self.assertTrue(ok)
        self.assertIn("OK", msg)
        win._on_test_cloud_done((ok, msg))
        self.assertIn("OK", win._cloud_status_var.get())
        self.assertEqual(str(win._test_cloud_btn["state"]), "normal")

    def test_groq_models_worker_and_apply(self):
        win = self.make()
        import imla.format as fmt

        with patch.object(fmt, "list_groq_chat_models", side_effect=OSError("x"), create=True):
            self.assertEqual(win._fetch_groq_models_worker(), [])
        with patch.object(fmt, "list_groq_chat_models", return_value=["a", "b"], create=True):
            self.assertEqual(win._fetch_groq_models_worker(), ["a", "b"])
        win._on_groq_models([])
        self.assertIn("Could not fetch models", win._groq_status_var.get())
        win._groq_model_var.set("custom/model")
        win._on_groq_models(["a", "b"])
        self.assertIn("custom/model", list(win._groq_box["values"]))
        self.assertEqual(win._groq_model_var.get(), "custom/model")

    def test_usage_empty_and_lines(self):
        win = self.make()
        import imla.usage as usage

        with patch.object(usage, "summary_lines", return_value=[], create=True):
            win._refresh_usage()
        self.assertEqual(
            win._usage_text.get("1.0", "end").strip(), "No cloud usage recorded today."
        )
        with patch.object(usage, "summary_lines", return_value=["l1", "l2"], create=True):
            win._refresh_usage()
        self.assertEqual(win._usage_text.get("1.0", "end").strip(), "l1\nl2")
        with patch.object(usage, "summary_lines", side_effect=RuntimeError("x"), create=True):
            win._refresh_usage()
        self.assertIn("No cloud usage", win._usage_text.get("1.0", "end"))

    def test_open_log_missing(self):
        win = self.make()
        with patch.object(win, "_open_path") as op:
            win._open_log_file()
            op.assert_not_called()
        self.assertTrue(win._general_status_var.get())

    def test_parse_vocabulary(self):
        self.assertEqual(parse_vocabulary(" a \n\nA\nb b\n"), ["a", "b b"])


if __name__ == "__main__":
    unittest.main()
