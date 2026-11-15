"""Tests for the accuracy improvements: retention guard, vocabulary prompts,
dictionary pre-pass, rule tier, and config helpers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imla import format as fmt  # noqa: E402
from imla.config import default_config, get_vocabulary  # noqa: E402
from imla.transcribe import build_initial_prompt  # noqa: E402


class RetentionGuardTests(unittest.TestCase):
    def test_rejects_output_that_drops_content(self):
        raw = "we should deploy the new payment service to production after the review"
        cleaned = "We should deploy."
        self.assertFalse(fmt._retention_ok(raw, cleaned))

    def test_accepts_faithful_output(self):
        raw = "we should deploy the new payment service to production after the review"
        cleaned = "We should deploy the new payment service to production after the review."
        self.assertTrue(fmt._retention_ok(raw, cleaned))

    def test_allows_self_correction_drop(self):
        raw = "send the file to Alice wait no send it to Bob right now please"
        cleaned = "Send it to Bob right now please."
        self.assertTrue(fmt._retention_ok(raw, cleaned))

    def test_short_inputs_skip_guard(self):
        self.assertTrue(fmt._retention_ok("hello there", "Hi."))

    def test_spoken_punctuation_not_counted_as_dropped(self):
        raw = "send a message to John comma tell him the meeting is at three period"
        cleaned = "Send a message to John, tell him the meeting is at three."
        self.assertTrue(fmt._retention_ok(raw, cleaned))


class RuleTierTests(unittest.TestCase):
    def test_keeps_like_and_actually(self):
        out = fmt._rule_cleanup("i like pizza and actually i love it")
        self.assertIn("like", out)
        self.assertIn("actually", out)

    def test_still_removes_hesitation_sounds(self):
        out = fmt._rule_cleanup("um i think uh this works")
        self.assertNotIn("um", out.lower().split())
        self.assertNotIn("uh", out.lower().split())


class PromptTests(unittest.TestCase):
    def test_grammar_rule_toggle(self):
        on = fmt.build_system_prompt(fix_grammar=True)
        off = fmt.build_system_prompt(fix_grammar=False)
        self.assertIn("grammatical mistakes", on)
        self.assertNotIn("grammatical mistakes", off)

    def test_vocabulary_injected(self):
        p = fmt.build_system_prompt(["Terraform", "Zustand", "terraform"])
        self.assertIn("Preserve these exact spellings", p)
        self.assertEqual(p.count("Terraform"), 1)  # de-duplicated, case-insensitive
        self.assertIn("Zustand", p)
        self.assertNotIn("Preserve these exact spellings", fmt.build_system_prompt())

    def test_prompt_has_no_unfilled_placeholders(self):
        p = fmt.build_system_prompt(["X"])
        self.assertNotIn("{grammar_rule}", p)
        self.assertNotIn("{vocab_block}", p)
        self.assertNotIn("{grammar_examples}", p)


class DictionaryPrePassTests(unittest.TestCase):
    def test_dictionary_applied_before_and_after(self):
        res = fmt.clean_transcript(
            "we use pie torch daily",
            tier_override="rule",
            dictionary=[("pie torch", "PyTorch")],
        )
        self.assertIn("PyTorch", res.text)


class VocabularyTests(unittest.TestCase):
    def test_build_initial_prompt(self):
        self.assertIsNone(build_initial_prompt([]))
        self.assertIsNone(build_initial_prompt(None))
        p = build_initial_prompt(["PyTorch", "gRPC", "pytorch"])
        self.assertEqual(p, "Technical terms: PyTorch, gRPC.")

    def test_get_vocabulary_merges_and_dedupes(self):
        cfg = default_config()
        cfg["vocabulary"] = ["Terraform", "pytorch"]
        vocab = get_vocabulary(cfg)
        self.assertIn("Terraform", vocab)
        self.assertEqual([v.lower() for v in vocab].count("pytorch"), 1)

    def test_default_config_values(self):
        cfg = default_config()
        self.assertEqual(cfg["transcription"]["model"], "small.en")
        self.assertGreaterEqual(cfg["transcription"]["beam_size"], 2)
        self.assertTrue(cfg["format"]["fix_grammar"])


if __name__ == "__main__":
    unittest.main()
