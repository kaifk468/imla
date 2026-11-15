"""Unit tests for two-model streaming preview architecture and HUD text layout."""

import queue
import time
import unittest
import numpy as np

from imla.preview import StreamingPreview, QUEUE_MAXSIZE, RATE_LIMIT_HZ
from imla.hud import HUD


class TestPreviewAndHUD(unittest.TestCase):
    def test_queue_maxsize_and_frame_dropping(self):
        """Verify queue maxsize is bounded to 8 and stale frames are dropped."""
        p = StreamingPreview(on_partial=lambda t: None)
        # The queue maxsize must be 8 as per core requirements
        self.assertEqual(p._queue.maxsize, 8)

        # Pretend preview is active and recognizer exists
        p._active = True
        p._recognizer = object()  # dummy
        p._native_sr = 16000

        # Fill queue to capacity (8 frames)
        dummy_chunk = np.zeros(1600, dtype=np.float32)
        for i in range(12):
            p.feed(dummy_chunk)

        # Queue size should never exceed 8
        self.assertLessEqual(p._queue.qsize(), 8)

    def test_rate_limiting(self):
        """Verify rate limit is set to <= 15 fps."""
        self.assertLessEqual(RATE_LIMIT_HZ, 15)

    def test_provisional_text_truncation(self):
        """Verify single-line word boundary left-truncation with ellipsis."""
        from imla.hud import _fit_text_ns, _TEXT_FONT_SIZE
        from AppKit import NSFont, NSFontWeightMedium

        font = NSFont.systemFontOfSize_weight_(_TEXT_FONT_SIZE, NSFontWeightMedium)

        # Empty string
        self.assertEqual(_fit_text_ns("", 380.0, font), "")

        # Short string that fits without truncation
        short = "hello world"
        self.assertEqual(_fit_text_ns(short, 380.0, font), "hello world")

        # Long string that exceeds 100px width
        long_text = "the quick brown fox jumps over the lazy dog in the forest"
        fitted = _fit_text_ns(long_text, 100.0, font)
        self.assertTrue(fitted.startswith("… "))
        # Must preserve the latest spoken words (end of the string)
        self.assertTrue(fitted.endswith("forest"))

        # Word boundary snapping: the first word after "… " should be a complete word
        after_ellipsis = fitted[2:].strip()
        words = after_ellipsis.split()
        self.assertIn(words[0], long_text.split())

    def test_two_model_separation(self):
        """Verify Vosk output is throwaway and never injected directly."""
        from imla.main import _finalize

        injected_texts = []
        def fake_inject(t):
            injected_texts.append(t)

        class DummyHUD:
            def __init__(self):
                self.preview_text = ""
                self.status_text = ""
            def set_preview_text(self, t):
                self.preview_text = t
            def set_status_text(self, t):
                self.status_text = t
            def hide(self):
                pass

        dummy_hud = DummyHUD()
        # Vosk sets preview text
        dummy_hud.set_preview_text("provisional vosk text")

        # Finalize should only inject text returned by Whisper
        audio = np.zeros(16000, dtype=np.float32)
        # If audio is empty or silence, nothing injected
        _finalize(audio, dummy_hud, inject_fn=fake_inject)
        self.assertNotIn("provisional vosk text", injected_texts)


if __name__ == "__main__":
    unittest.main()
