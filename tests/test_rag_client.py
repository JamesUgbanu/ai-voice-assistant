"""Contract tests: RAG client validation, SSE folding, failure mapping.

Transport is injected (fakes only) — no network, no httpx import needed.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.rag_client import RagError, build_rag_body, fetch_answer, parse_sse_events


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


def sse(*frames: str) -> str:
    return "".join(f"event: {t}\ndata: {d}\n\n" for t, d in frames)


class ParseSseTest(unittest.TestCase):
    def test_folds_tokens_sources_and_meta(self):
        parsed = parse_sse_events(
            sse(("meta", '{"sessionId":"x","route":"knowledge"}'),
                ("token", '{"text":"Hello "}'),
                ("token", '{"text":"there."}'),
                ("sources", '{"sources":[{"title":"T"}]}'),
                ("done", '{"metrics":{}}'))
        )
        self.assertEqual(parsed["answer"], "Hello there.")
        self.assertEqual(parsed["route"], "knowledge")
        self.assertEqual(parsed["sources"], [{"title": "T"}])
        self.assertIsNone(parsed["error"])

    def test_captures_error_events_and_ignores_unknown(self):
        parsed = parse_sse_events(
            sse(("bogus", '{"a":1}'), ("token", "not json"), ("error", '{"message":"oops"}')))
        self.assertEqual(parsed["answer"], "")
        self.assertEqual(parsed["error"], "oops")

    def test_empty_stream(self):
        parsed = parse_sse_events("")
        self.assertEqual(parsed["answer"], "")
        self.assertIsNone(parsed["error"])


class BuildBodyTest(unittest.TestCase):
    def test_bounds_and_history_cap(self):
        body = build_rag_body("  hello james, tell me stuff  ", history=[
            {"question": "q" * 600, "answer": "a" * 3000},
            {"question": "  ", "answer": "x"},
            "garbage",
        ])
        self.assertEqual(body["question"], "hello james, tell me stuff")
        self.assertEqual(len(body["history"]), 1)
        self.assertLessEqual(len(body["history"][0]["question"]), 500)
        self.assertLessEqual(len(body["history"][0]["answer"]), 2000)
        self.assertFalse(body["debug"])

    def test_rejects_short_or_empty_questions(self):
        for bad in ("", "  ", "hi", None):
            with self.assertRaises(RagError) as ctx:
                build_rag_body(bad)
            self.assertEqual(ctx.exception.code, "bad_question")

    def test_voice_mode_is_forwarded_only_when_set(self):
        # Voice turns request concise spoken answers via the existing API.
        body = build_rag_body("What has James built with AWS?", mode="voice")
        self.assertEqual(body["mode"], "voice")
        # Website/default callers are byte-identical to before: no mode key.
        self.assertNotIn("mode", build_rag_body("What has James built with AWS?"))
        self.assertNotIn("mode", build_rag_body("hello there", mode=None))
        self.assertFalse(body["debug"])


class FetchAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_auth_shape(self):
        client = FakeClient(FakeResponse(200, sse(("token", '{"text":"James builds backends."}'))))
        out = await fetch_answer(client, "https://rag.example/api/rag", "tok123", "What does he build?")
        self.assertEqual(out.text, "James builds backends.")
        self.assertFalse(out.truncated)
        call = client.calls[0]
        self.assertEqual(call["headers"], {"Authorization": "Bearer tok123"})
        self.assertEqual(call["json"]["question"], "What does he build?")

    async def test_missing_config_fails_closed(self):
        client = FakeClient()
        for url, tok in (("", "t"), ("https://x", "")):
            with self.assertRaises(RagError) as ctx:
                await fetch_answer(client, url, tok, "valid question here")
            self.assertEqual(ctx.exception.code, "not_configured")
        self.assertEqual(client.calls, [])

    async def test_status_mapping(self):
        for status, code in ((401, "unauthorized"), (429, "rate_limited"), (500, "http_error")):
            client = FakeClient(FakeResponse(status, "x"))
            with self.assertRaises(RagError) as ctx:
                await fetch_answer(client, "https://x", "t", "valid question here")
            self.assertEqual(ctx.exception.code, code)

    async def test_transport_failure_and_empty_answer(self):
        client = FakeClient(exc=ConnectionError("down"))
        with self.assertRaises(RagError) as ctx:
            await fetch_answer(client, "https://x", "t", "valid question here")
        self.assertEqual(ctx.exception.code, "transport")

        quiet = FakeClient(FakeResponse(200, sse(("done", "{}"))))
        with self.assertRaises(RagError) as ctx2:
            await fetch_answer(quiet, "https://x", "t", "valid question here")
        self.assertEqual(ctx2.exception.code, "empty_answer")


if __name__ == "__main__":
    unittest.main()
