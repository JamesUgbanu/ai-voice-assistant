"""Streaming RAG client: incremental SSE parsing and token streaming.

Transport injected as a fake async context manager — no network, no httpx.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.rag_client import RagError, SseStreamParser, iter_answer


def sse(*frames: str) -> str:
    return "".join(f"event: {t}\ndata: {d}\n\n" for t, d in frames)


class FakeStreamResponse:
    def __init__(self, status_code=200, chunks=None):
        self.status_code = status_code
        self._chunks = chunks or []

    async def aiter_text(self):
        for c in self._chunks:
            yield c


class _Ctx:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeStreamClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return _Ctx(self.response, self.exc)


async def collect(agen):
    return [c async for c in agen]


class SseStreamParserTest(unittest.TestCase):
    def test_splits_frames_across_arbitrary_chunks(self):
        p = SseStreamParser()
        # Feed one frame split mid-line and mid-JSON.
        out = []
        out += p.feed("event: tok")
        out += p.feed('en\ndata: {"te')
        out += p.feed('xt": "Hel')
        out += p.feed('lo"}\n\nevent: token\ndata: {"text":" world"}\n\n')
        self.assertEqual([e["event"] for e in out], ["token", "token"])
        self.assertEqual([e["data"]["text"] for e in out], ["Hello", " world"])

    def test_ignores_unknown_and_malformed(self):
        p = SseStreamParser()
        out = p.feed("event: metrics\ndata: not-json\n\nno-colon-line\n\n")
        self.assertEqual(out, [])

    def test_close_drains_terminatorless_frame(self):
        p = SseStreamParser()
        self.assertEqual(p.feed('event: token\ndata: {"text":"end"}'), [])
        drained = p.close()
        self.assertEqual(drained, [{"event": "token", "data": {"text": "end"}}])


class IterAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_streams_tokens_in_order(self):
        body = sse(("meta", '{"route":"knowledge"}'), ("token", '{"text":"Alpha "}'),
                   ("token", '{"text":"runs on AWS."}'), ("sources", '{"sources":[]}'))
        # Deliver the body in tiny slices to exercise frame reassembly.
        slices = [body[i:i + 7] for i in range(0, len(body), 7)]
        client = FakeStreamClient(FakeStreamResponse(200, slices))
        chunks = await collect(iter_answer(client, "https://rag/api/rag", "tok", "What runs on AWS?"))
        self.assertEqual(chunks, ["Alpha ", "runs on AWS."])
        self.assertEqual(client.calls[0]["headers"], {"Authorization": "Bearer tok"})

    async def test_error_event_raises_rag_error(self):
        client = FakeStreamClient(FakeStreamResponse(200, [sse(("error", '{"message":"boom"}'))]))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question"))
        self.assertEqual(ctx.exception.code, "rag_error")

    async def test_status_mapping(self):
        for status, code in ((401, "unauthorized"), (429, "rate_limited"), (500, "http_error")):
            client = FakeStreamClient(FakeStreamResponse(status, []))
            with self.assertRaises(RagError) as ctx:
                await collect(iter_answer(client, "https://x", "t", "valid question"))
            self.assertEqual(ctx.exception.code, code)

    async def test_empty_stream_is_empty_answer(self):
        client = FakeStreamClient(FakeStreamResponse(200, [sse(("done", "{}"))]))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question"))
        self.assertEqual(ctx.exception.code, "empty_answer")

    async def test_transport_failure_maps_to_transport(self):
        client = FakeStreamClient(exc=ConnectionError("down"))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question"))
        self.assertEqual(ctx.exception.code, "transport")

    async def test_missing_config_fails_closed(self):
        client = FakeStreamClient(FakeStreamResponse(200, []))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "", "", "valid question"))
        self.assertEqual(ctx.exception.code, "not_configured")
        self.assertEqual(client.calls, [])

    async def test_should_stop_ends_early_with_partial(self):
        # Three tokens; stop after the first is yielded.
        body = sse(("token", '{"text":"one "}'), ("token", '{"text":"two "}'), ("token", '{"text":"three"}'))
        client = FakeStreamClient(FakeStreamResponse(200, [body]))
        seen = {"n": 0}

        def should_stop():
            seen["n"] += 1
            return seen["n"] > 1

        chunks = await collect(iter_answer(client, "https://x", "t", "valid question", should_stop=should_stop))
        self.assertEqual(chunks, ["one "])

    async def test_max_answer_chars_caps_output(self):
        body = sse(("token", '{"text":"abcdefghij"}'), ("token", '{"text":"klmnopqrst"}'))
        client = FakeStreamClient(FakeStreamResponse(200, [body]))
        chunks = await collect(iter_answer(client, "https://x", "t", "valid question", max_answer_chars=12))
        self.assertEqual("".join(chunks), "abcdefghijkl")

    async def test_short_question_rejected_before_network(self):
        client = FakeStreamClient(FakeStreamResponse(200, []))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "hi"))
        self.assertEqual(ctx.exception.code, "bad_question")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
