"""Tests for Groq model rotation on rate limits, usage tracking, and the
short-transcript LLM bypass. No real network calls."""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from imla import format as fmt  # noqa: E402
from imla import usage  # noqa: E402

M1, M2, M3 = fmt.GROQ_FALLBACK_MODELS


def _resp(status, json_data=None, headers=None, text=None):
    req = httpx.Request("POST", fmt.GROQ_API_URL)
    if json_data is not None:
        return httpx.Response(status, json=json_data, headers=headers, request=req)
    return httpx.Response(
        status, text=text or "", headers=headers, request=req
    )


def _ok(text="Hello there.", tokens=1234):
    return _resp(200, {
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": tokens},
    })


class FakeClient:
    """Stand-in for httpx.Client; replies come from a model->response map
    (or list of responses consumed in order per model)."""

    def __init__(self, replies):
        self.replies = replies
        self.requested = []

    def __call__(self, *a, **kw):
        outer = self

        class _Ctx:
            def __enter__(s):
                return s

            def __exit__(s, *exc):
                return False

            def post(s, url, json=None, headers=None):
                model = json["model"]
                outer.requested.append(model)
                rep = outer.replies[model]
                if isinstance(rep, Exception):
                    raise rep
                return rep

        return _Ctx()


class BaseCase(unittest.TestCase):
    def setUp(self):
        fmt._RATE_LIMITED_UNTIL.clear()
        fmt._UNAVAILABLE_GROQ_MODELS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.usage_path = Path(self.tmp.name) / "usage.json"
        p = mock.patch.object(usage, "USAGE_PATH", self.usage_path)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        fmt._RATE_LIMITED_UNTIL.clear()
        fmt._UNAVAILABLE_GROQ_MODELS.clear()
        self.tmp.cleanup()

    def call(self, replies, model=M1):
        fake = FakeClient(replies)
        with mock.patch.object(fmt.httpx, "Client", fake):
            try:
                out = fmt._call_groq("hello there", "KEY", model, "sys")
            finally:
                self.fake = fake
        return out


class RotationTests(BaseCase):
    def test_429_then_next_model(self):
        out = self.call({M1: _resp(429, headers={"retry-after": "30"}), M2: _ok()})
        self.assertEqual(out, "Hello there.")
        self.assertEqual(self.fake.requested, [M1, M2])
        self.assertIn(M1, fmt._RATE_LIMITED_UNTIL)
        left = fmt._RATE_LIMITED_UNTIL[M1] - time.time()
        self.assertTrue(20 < left <= 31)

    def test_default_retry_after_60(self):
        self.call({M1: _resp(429), M2: _ok()})
        left = fmt._RATE_LIMITED_UNTIL[M1] - time.time()
        self.assertTrue(50 < left <= 61)

    def test_later_call_skips_limited_model(self):
        self.call({M1: _resp(429), M2: _ok()})
        self.call({M1: _resp(500), M2: _ok()})
        self.assertEqual(self.fake.requested, [M2])

    def test_per_day_marks_until_midnight(self):
        body = '{"error":{"message":"Rate limit reached ... tokens per day (TPD)"}}'
        self.call({M1: _resp(429, text=body, headers={"retry-after": "5"}), M2: _ok()})
        now = datetime.now(timezone.utc)
        mid = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        self.assertAlmostEqual(fmt._RATE_LIMITED_UNTIL[M1], mid, delta=5)

    def test_all_limited_raises(self):
        far = time.time() + 1000
        for m in (M1, M2, M3):
            fmt._RATE_LIMITED_UNTIL[m] = far
        with mock.patch.object(fmt.httpx, "Client", FakeClient({})) as _:
            with self.assertRaises(RuntimeError):
                fmt._call_groq("hi", "KEY", M1, "sys")

    def test_all_429_raises(self):
        with self.assertRaises(httpx.HTTPStatusError):
            self.call({M1: _resp(429), M2: _resp(429), M3: _resp(429)})
        for m in (M1, M2, M3):
            self.assertIn(m, fmt._RATE_LIMITED_UNTIL)

    def test_500_then_next_model_no_mark(self):
        out = self.call({M1: _resp(500), M2: _ok()})
        self.assertEqual(out, "Hello there.")
        self.assertEqual(self.fake.requested, [M1, M2])
        self.assertEqual(fmt._RATE_LIMITED_UNTIL, {})

    def test_timeout_then_next_model(self):
        out = self.call({M1: httpx.ReadTimeout("slow"), M2: _ok()})
        self.assertEqual(out, "Hello there.")
        self.assertEqual(fmt._RATE_LIMITED_UNTIL, {})

    def test_401_raises_immediately(self):
        with self.assertRaises(httpx.HTTPStatusError):
            self.call({M1: _resp(401), M2: _ok()})
        self.assertEqual(self.fake.requested, [M1])

    def test_404_marks_unavailable(self):
        out = self.call({M1: _resp(404), M2: _ok()})
        self.assertEqual(out, "Hello there.")
        self.assertIn(M1, fmt._UNAVAILABLE_GROQ_MODELS)

    def test_usage_recorded(self):
        self.call({M1: _ok(tokens=777)})
        d = usage.get_today()
        self.assertEqual(d["tokens"][M1], 777)
        self.assertEqual(d["requests"][M1], 1)


class UsageTests(BaseCase):
    def test_audio_and_totals(self):
        usage.record_audio_seconds(12.5)
        usage.record_audio_seconds(7.5)
        d = usage.get_today()
        self.assertEqual(d["audio_seconds"], 20.0)
        self.assertEqual(d["audio_requests"], 2)

    def test_day_rollover_resets(self):
        self.usage_path.write_text(json.dumps({
            "date": "2000-01-01", "tokens": {"m": 5}, "requests": {"m": 1},
            "audio_seconds": 9.0, "audio_requests": 3,
        }))
        d = usage.get_today()
        self.assertEqual(d["tokens"], {})
        self.assertEqual(d["audio_seconds"], 0.0)
        usage.record_tokens("m", 10)
        self.assertEqual(usage.get_today()["tokens"], {"m": 10})

    def test_unwritable_path_swallowed(self):
        bad = Path(self.tmp.name) / "afile"
        bad.write_text("x")
        with mock.patch.object(usage, "USAGE_PATH", bad / "sub" / "usage.json"):
            usage.record_tokens("m", 1)
            usage.record_audio_seconds(1)
            self.assertEqual(usage.get_today()["tokens"], {})
            usage.summary_lines()

    def test_summary_warning_at_80_percent(self):
        usage.record_tokens("openai/gpt-oss-20b", 41_200)
        lines = usage.summary_lines()
        self.assertIn("openai/gpt-oss-20b: 41,200 / 200,000 tokens (20%)", lines)
        self.assertFalse(any(l.startswith("⚠") for l in lines))
        usage.record_tokens("openai/gpt-oss-20b", 130_000)
        lines = usage.summary_lines()
        self.assertTrue(any(l.startswith("⚠") for l in lines))

    def test_audio_summary_line(self):
        usage.record_audio_seconds(312)
        self.assertIn("Cloud Whisper: 312 s / 28,800 s (1%)", usage.summary_lines())


class BypassTests(BaseCase):
    def run_clean(self, raw, **kw):
        with mock.patch.object(fmt, "_call_groq", return_value="Mocked.") as g, \
                mock.patch.object(fmt, "_call_ollama", side_effect=RuntimeError):
            res = fmt.clean_transcript(raw, groq_api_key="KEY", **kw)
        return res, g

    def test_short_clean_skips_llm(self):
        res, g = self.run_clean("open the door")
        g.assert_not_called()
        self.assertEqual(res.tier, "rule")

    def test_override_groq_still_calls(self):
        res, g = self.run_clean("open the door", tier_override="groq")
        g.assert_called_once()
        self.assertEqual(res.tier, "groq")

    def test_disabled_flag_calls_llm(self):
        res, g = self.run_clean("open the door", skip_llm_for_short=False)
        g.assert_called_once()

    def test_filler_not_bypassed(self):
        for raw in ("um open the door", "hello comma world", "wait no stop"):
            res, g = self.run_clean(raw)
            g.assert_called_once()

    def test_long_not_bypassed(self):
        res, g = self.run_clean("one two three four five six seven")
        g.assert_called_once()


class ListModelsTests(BaseCase):
    def _client(self, resp=None, exc=None):
        class C:
            def __init__(s, *a, **k):
                pass

            def __enter__(s):
                return s

            def __exit__(s, *e):
                return False

            def get(s, url, headers=None):
                if exc:
                    raise exc
                return resp

        return C

    def test_filters_non_chat(self):
        ids = ["whisper-large-v3", "openai/gpt-oss-20b", "canopylabs/orpheus-v1",
               "meta-llama/llama-prompt-guard-2-86m", "openai/gpt-oss-safeguard-20b",
               "qwen/qwen3.8-27b"]
        resp = httpx.Response(
            200, json={"data": [{"id": i} for i in ids]},
            request=httpx.Request("GET", fmt.GROQ_MODELS_URL),
        )
        with mock.patch.object(fmt.httpx, "Client", self._client(resp)):
            out = fmt.list_groq_chat_models("KEY")
        self.assertEqual(out, ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"])

    def test_error_returns_empty(self):
        with mock.patch.object(
            fmt.httpx, "Client", self._client(exc=httpx.ConnectError("x"))
        ):
            self.assertEqual(fmt.list_groq_chat_models("KEY"), [])


if __name__ == "__main__":
    unittest.main()
