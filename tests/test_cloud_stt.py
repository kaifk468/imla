"""Tests for Groq cloud STT with local fallback (httpx and Whisper mocked)."""

import io
import sys
import unittest
import wave
from pathlib import Path
from unittest import mock

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imla import transcribe as tr  # noqa: E402


def _tone(seconds=1.0, amp=0.3):
    t = np.arange(int(16000 * seconds)) / 16000.0
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _resp(status=200, payload=None, headers=None, bad_json=False):
    r = mock.MagicMock()
    r.status_code = status
    r.headers = headers or {}
    if bad_json:
        r.json.side_effect = ValueError("bad json")
    else:
        r.json.return_value = payload if payload is not None else {"text": " hello world "}
    return r


def _client_patch(resp=None, exc=None):
    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if exc is not None:
        client.post.side_effect = exc
    else:
        client.post.return_value = resp
    return mock.patch("imla.transcribe.httpx.Client", return_value=client), client


class CloudSTTTests(unittest.TestCase):
    def setUp(self):
        self._saved = (tr._engine, tr._cloud_model)
        tr.reset_cloud_breaker()
        tr.set_engine("auto")
        p = mock.patch("imla.transcribe._transcribe_local", return_value="local text")
        self.local = p.start()
        self.addCleanup(p.stop)
        u = mock.patch.dict("sys.modules", {"imla.usage": mock.MagicMock()})
        u.start()
        self.addCleanup(u.stop)

    def tearDown(self):
        tr._engine, tr._cloud_model = self._saved
        tr.reset_cloud_breaker()

    def run_with(self, resp=None, exc=None, audio=None, engine=None):
        if engine:
            tr.set_engine(engine)
        patcher, client = _client_patch(resp, exc)
        with patcher:
            res = tr.transcribe_detailed(_tone() if audio is None else audio, api_key="k")
        return res, client

    def test_wav_roundtrip(self):
        a = _tone(0.5)
        with wave.open(io.BytesIO(tr.encode_wav(a)), "rb") as w:
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getnframes(), len(a))

    def test_wav_clips(self):
        wav = tr.encode_wav(np.array([5.0, -5.0], dtype=np.float32))
        with wave.open(io.BytesIO(wav), "rb") as w:
            d = np.frombuffer(w.readframes(2), dtype="<i2")
        self.assertEqual(list(d), [32767, -32767])

    def test_success(self):
        res, client = self.run_with(_resp())
        self.assertEqual((res.text, res.engine), ("hello world", "groq"))
        self.assertIsNone(res.fallback_reason)
        self.local.assert_not_called()
        kw = client.post.call_args.kwargs
        self.assertEqual(kw["headers"]["Authorization"], "Bearer k")
        self.assertEqual(kw["data"]["model"], "whisper-large-v3-turbo")
        self.assertNotIn("prompt", kw["data"])

    def test_vocab_prompt_sent(self):
        patcher, client = _client_patch(_resp())
        with patcher:
            tr.transcribe_groq(_tone(), vocab=["PyTorch"], api_key="k")
        self.assertIn("PyTorch", client.post.call_args.kwargs["data"]["prompt"])

    def test_429_opens_breaker(self):
        res, _ = self.run_with(_resp(429, headers={"retry-after": "30"}))
        self.assertEqual(res.engine, "local")
        self.assertEqual(res.fallback_reason, "http 429")
        self.assertTrue(tr._breaker_open())
        self.local.assert_called_once()

    def test_429_retry_after_parsed(self):
        patcher, _ = _client_patch(_resp(429, headers={"retry-after": "12"}))
        with patcher, self.assertRaises(tr.CloudSTTError) as cm:
            tr.transcribe_groq(_tone(), api_key="k")
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(cm.exception.retry_after, 12.0)

    def test_timeout(self):
        res, _ = self.run_with(exc=httpx.ReadTimeout("slow"))
        self.assertEqual((res.engine, res.fallback_reason), ("local", "timeout"))
        self.local.assert_called_once()

    def test_offline(self):
        res, _ = self.run_with(exc=httpx.ConnectError("down"))
        self.assertEqual((res.engine, res.fallback_reason), ("local", "offline"))

    def test_401(self):
        res, _ = self.run_with(_resp(401))
        self.assertEqual((res.engine, res.fallback_reason), ("local", "http 401"))
        self.local.assert_called_once()

    def test_malformed_json(self):
        res, _ = self.run_with(_resp(bad_json=True))
        self.assertEqual(res.engine, "local")
        self.assertEqual(res.fallback_reason, "malformed response")

    def test_no_api_key(self):
        import imla.keychain  # noqa: F401

        env = {k: v for k, v in __import__("os").environ.items() if k != "GROQ_API_KEY"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "imla.keychain.get_api_key", return_value=None
        ):
            res = tr.transcribe_detailed(_tone())
        self.assertEqual((res.engine, res.fallback_reason), ("local", "no API key"))
        self.local.assert_called_once()

    def test_empty_cloud_result_falls_back(self):
        res, _ = self.run_with(_resp(payload={"text": "  "}))
        self.assertEqual(res.engine, "local")
        self.assertEqual(res.fallback_reason, "empty cloud result")
        self.local.assert_called_once()

    def test_three_failures_open_breaker(self):
        for _ in range(3):
            self.run_with(_resp(500))
        self.assertTrue(tr._breaker_open())
        patcher, client = _client_patch(_resp())
        with patcher:
            res = tr.transcribe_detailed(_tone(), api_key="k")
        client.post.assert_not_called()
        self.assertEqual(res.engine, "local")
        self.assertTrue(res.fallback_reason)

    def test_success_resets_failures(self):
        self.run_with(_resp(500))
        self.run_with(_resp(500))
        self.run_with(_resp())
        self.run_with(_resp(500))
        self.run_with(_resp(500))
        self.assertFalse(tr._breaker_open())

    def test_local_engine_never_calls_cloud(self):
        res, client = self.run_with(_resp(), engine="local")
        client.post.assert_not_called()
        self.assertEqual(res.engine, "local")
        self.local.assert_called_once()

    def test_cloud_engine_falls_back(self):
        res, _ = self.run_with(_resp(500), engine="cloud")
        self.assertEqual(res.engine, "local")

    def test_unknown_engine_is_auto(self):
        tr.set_engine("bogus")
        self.assertEqual(tr.get_engine(), "auto")

    def test_short_and_silent_skip_cloud(self):
        for audio in (_tone(0.2), np.zeros(16000, dtype=np.float32)):
            res, client = self.run_with(_resp(), audio=audio)
            client.post.assert_not_called()
            self.assertEqual(res.engine, "local")
        self.assertEqual(self.local.call_count, 2)

    def test_empty_audio(self):
        res = tr.transcribe_detailed(np.zeros(0, dtype=np.float32))
        self.assertEqual((res.text, res.engine, res.latency_s), ("", "local", 0.0))
        self.local.assert_not_called()

    def test_transcribe_returns_str(self):
        patcher, _ = _client_patch(_resp())
        with patcher, mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}):
            out = tr.transcribe(_tone())
        self.assertIsInstance(out, str)
        self.assertEqual(out, "hello world")

    def test_test_cloud_failure_does_not_raise(self):
        patcher, _ = _client_patch(exc=httpx.ConnectError("down"))
        with patcher:
            ok, msg, lat = tr.test_cloud(api_key="k")
        self.assertFalse(ok)
        self.assertIsInstance(msg, str)
        self.assertGreaterEqual(lat, 0.0)

    def test_test_cloud_success(self):
        patcher, _ = _client_patch(_resp(payload={"text": ""}))
        with patcher:
            ok, _msg, _lat = tr.test_cloud(api_key="k")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
