"""Tests for desktop-integration modules (launcher, applog, single_instance,
keychain import, Windows startup command). All mocked / temp dirs."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from imla import applog, keychain, launcher, single_instance, startup  # noqa: E402


class LauncherTests(unittest.TestCase):
    def test_frozen_command(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\App\Imla.exe"):
            self.assertTrue(launcher.is_frozen())
            self.assertEqual(
                launcher.launch_command("--settings"),
                [r"C:\App\Imla.exe", "--settings"],
            )
            self.assertEqual(launcher.project_root(), Path(r"C:\App").resolve())

    def test_not_frozen_command(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            cmd = launcher.launch_command("--settings", windowed=False)
            self.assertEqual(cmd, [sys.executable, "-m", "imla.main", "--settings"])
            self.assertEqual(launcher.project_root(), ROOT)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_gui_python_prefers_pythonw(self):
        with tempfile.TemporaryDirectory() as d:
            py = Path(d) / "python.exe"
            pyw = Path(d) / "pythonw.exe"
            py.write_text("")
            with mock.patch.object(sys, "frozen", False, create=True), \
                    mock.patch.object(sys, "executable", str(py)):
                self.assertEqual(launcher.gui_python(), str(py))
                pyw.write_text("")
                self.assertEqual(launcher.gui_python(), str(pyw))


class AppLogTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.saved_handlers = list(self.root.handlers)
        self.saved_level = self.root.level
        self.saved = (sys.stdout, sys.stderr, sys.excepthook)

    def tearDown(self):
        for h in list(self.root.handlers):
            if h not in self.saved_handlers:
                self.root.removeHandler(h)
                h.close()
        self.root.setLevel(self.saved_level)
        sys.stdout, sys.stderr, sys.excepthook = self.saved

    def test_writes_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = applog.setup_logging(log_dir=Path(d))
            p2 = applog.setup_logging(log_dir=Path(d))
            self.assertEqual(p1, p2)
            marked = [h for h in self.root.handlers if getattr(h, applog._MARK, False)]
            self.assertLessEqual(len(marked), 2)
            files = [h for h in marked if isinstance(h, logging.FileHandler)]
            self.assertEqual(len(files), 1)
            logging.getLogger("imla.test").info("hello-log")
            for h in marked:
                h.flush()
            self.assertIn("hello-log", p1.read_text(encoding="utf-8"))
            # release file handle so the temp dir can be removed on Windows
            for h in marked:
                self.root.removeHandler(h)
                h.close()

    def test_survives_no_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            sys.stderr = None
            sys.stdout = None
            p = applog.setup_logging(log_dir=Path(d))
            self.assertIsNotNone(sys.stderr)
            self.assertIsNotNone(sys.stdout)
            print("stray print")
            marked = [h for h in self.root.handlers if getattr(h, applog._MARK, False)]
            self.assertFalse(
                [h for h in marked if type(h) is logging.StreamHandler]
            )
            logging.getLogger("imla.test").warning("no-console")
            for h in marked:
                h.flush()
            self.assertIn("no-console", p.read_text(encoding="utf-8"))
            for h in marked:
                self.root.removeHandler(h)
                h.close()
            sys.stderr.close()
            sys.stdout.close()

    def test_excepthook_installed(self):
        import threading

        saved_thread_hook = threading.excepthook
        try:
            applog.install_excepthook()
            self.assertIsNot(sys.excepthook, sys.__excepthook__)
        finally:
            threading.excepthook = saved_thread_hook


class SingleInstanceTests(unittest.TestCase):
    def test_second_process_is_refused(self):
        name = "imla-test-" + uuid.uuid4().hex
        self.assertTrue(single_instance.acquire(name))
        try:
            code = (
                "import sys; sys.path.insert(0, %r); "
                "from imla import single_instance as s; "
                "sys.exit(0 if s.acquire(%r) else 3)" % (str(ROOT), name)
            )
            r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT))
            self.assertEqual(r.returncode, 3)
        finally:
            single_instance.release(name)
        # After release a new process can take it.
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT))
        self.assertEqual(r.returncode, 0)


class KeychainImportTests(unittest.TestCase):
    def test_imports_when_missing(self):
        with mock.patch.object(keychain.keyring, "get_password", return_value=None), \
                mock.patch.object(keychain.keyring, "set_password") as sp, \
                mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fake"}):
            self.assertTrue(keychain.import_env_key_if_missing())
            sp.assert_called_once_with(keychain.SERVICE_NAME, keychain.KEY_NAME, "gsk_fake")

    def test_present_key_not_overwritten(self):
        with mock.patch.object(keychain.keyring, "get_password", return_value="x"), \
                mock.patch.object(keychain.keyring, "set_password") as sp, \
                mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fake"}):
            self.assertFalse(keychain.import_env_key_if_missing())
            sp.assert_not_called()

    def test_empty_env(self):
        with mock.patch.object(keychain.keyring, "get_password", return_value=None), \
                mock.patch.object(keychain.keyring, "set_password") as sp, \
                mock.patch.dict(os.environ, {"GROQ_API_KEY": "  "}):
            self.assertFalse(keychain.import_env_key_if_missing())
            sp.assert_not_called()

    def test_keyring_raises(self):
        with mock.patch.object(keychain.keyring, "get_password", side_effect=RuntimeError("boom")), \
                mock.patch.object(keychain.keyring, "set_password", side_effect=RuntimeError("boom")), \
                mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_fake"}):
            self.assertFalse(keychain.import_env_key_if_missing())


class WindowsStartupTests(unittest.TestCase):
    def test_command_quoted_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="dir with space ") as d:
            py = Path(d) / "python.exe"
            pyw = Path(d) / "pythonw.exe"
            py.write_text("")
            pyw.write_text("")
            with mock.patch.object(sys, "frozen", False, create=True), \
                    mock.patch.object(sys, "executable", str(py)), \
                    mock.patch.object(sys, "platform", "win32"):
                cmd = startup._windows_command()
            self.assertTrue(cmd.startswith('"' + str(pyw) + '"'), cmd)
            self.assertIn("-c", cmd)
            self.assertIn("imla.main", cmd)

    def test_frozen_command(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Program Files\Imla\Imla.exe"):
            self.assertEqual(startup._windows_command(), '"C:\\Program Files\\Imla\\Imla.exe"')

    def test_enable_writes_run_value(self):
        fake = mock.MagicMock()
        fake.HKEY_CURRENT_USER = 1
        fake.REG_SZ = 1
        fake.KEY_SET_VALUE = 2
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            startup._enable_windows()
        args = fake.SetValueEx.call_args[0]
        self.assertEqual(args[1], startup.DISPLAY_NAME)
        self.assertEqual(args[4], startup._windows_command())


if __name__ == "__main__":
    unittest.main()
