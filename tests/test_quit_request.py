"""Quit request marker used by Settings' "Quit Imla" button and `--quit`."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imla import single_instance as si  # noqa: E402


class QuitRequestTests(unittest.TestCase):
    def test_request_then_consume_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(si, "_lock_dir", lambda: Path(d)):
            self.assertFalse(si.quit_requested())
            self.assertTrue(si.request_quit())
            self.assertTrue(si.quit_requested())
            self.assertFalse(si.quit_requested())  # consumed

    def test_stale_marker_cleared_at_startup(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(si, "_lock_dir", lambda: Path(d)):
            si.request_quit()
            si.clear_quit_request()
            self.assertFalse(si.quit_requested())

    def test_cli_quit_flag_exits_zero(self):
        from imla import main as m
        with tempfile.TemporaryDirectory() as d, mock.patch.object(si, "_lock_dir", lambda: Path(d)):
            self.assertEqual(m.main(["--quit"]), 0)
            self.assertTrue(si.quit_requested())


if __name__ == "__main__":
    unittest.main()
