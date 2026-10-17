"""Streaming RAG client: incremental SSE parsing and token streaming.

Transport injected as a fake async context manager — no network, no httpx.
"""
import asyncio
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

    async def test_voice_mode_reaches_the_request_body(self):
        client = FakeStreamClient(FakeStreamResponse(200, [sse(("token", '{"text":"Hi."}'))]))
        await collect(iter_answer(client, "https://rag", "t", "What runs on AWS?", mode="voice"))
        self.assertEqual(client.calls[0]["json"]["mode"], "voice")
        # Default callers send no mode key (website behavior preserved).
        client2 = FakeStreamClient(FakeStreamResponse(200, [sse(("token", '{"text":"Hi."}'))]))
        await collect(iter_answer(client2, "https://rag", "t", "What runs on AWS?"))
        self.assertNotIn("mode", client2.calls[0]["json"])

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


class _DelayedResponse(FakeStreamResponse):
    async def aiter_text(self):
        await asyncio.sleep(0.02)  # first body byte is deliberately late
        for c in self._chunks:
            yield c


class _CancelResponse(FakeStreamResponse):
    async def aiter_text(self):
        # Body read is cancelled before any byte arrives. CancelledError is a
        # BaseException, so it must propagate (not be reclassified) and must
        # not produce a TTFB.
        raise asyncio.CancelledError()
        yield ""  # pragma: no cover — makes this an async generator


class RagTtfbTest(unittest.IsolatedAsyncioTestCase):
    """Request→first-body-byte (TTFB) instrumentation. Metric presence and
    ordering only — no brittle exact-millisecond assertions."""

    async def test_ttfb_reported_once_before_first_token(self):
        log: list = []
        body = sse(("meta", "{}"), ("token", '{"text":"one "}'), ("token", '{"text":"two"}'))
        client = FakeStreamClient(FakeStreamResponse(200, [body]))
        agen = iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms))
        first = await agen.__anext__()
        self.assertEqual(first, "one ")
        # TTFB fired at/before the first token, exactly once, non-negative int.
        self.assertEqual(len(log), 1)
        self.assertIsInstance(log[0], int)
        self.assertGreaterEqual(log[0], 0)
        rest = [c async for c in agen]
        self.assertEqual(rest, ["two"])
        self.assertEqual(len(log), 1)  # never repeated per chunk

    async def test_first_byte_and_first_token_in_same_chunk(self):
        log: list = []
        client = FakeStreamClient(FakeStreamResponse(200, [sse(("token", '{"text":"Hi."}'))]))
        chunks = await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(chunks, ["Hi."])
        self.assertEqual(len(log), 1)

    async def test_delayed_first_byte_still_recorded(self):
        log: list = []
        client = FakeStreamClient(_DelayedResponse(200, [sse(("token", '{"text":"late"}'))]))
        await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(len(log), 1)
        self.assertGreaterEqual(log[0], 0)

    async def test_no_ttfb_on_empty_body(self):
        log: list = []
        client = FakeStreamClient(FakeStreamResponse(200, []))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(ctx.exception.code, "empty_answer")
        self.assertEqual(log, [])  # no body byte → no fabricated timing

    async def test_no_ttfb_on_http_error_before_body(self):
        log: list = []
        client = FakeStreamClient(FakeStreamResponse(503, []))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(ctx.exception.code, "http_error")
        self.assertEqual(log, [])

    async def test_no_ttfb_on_transport_failure(self):
        log: list = []
        client = FakeStreamClient(exc=ConnectionError("down"))
        with self.assertRaises(RagError):
            await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(log, [])

    async def test_no_ttfb_on_timeout_before_body(self):
        log: list = []
        client = FakeStreamClient(exc=TimeoutError("timed out"))
        with self.assertRaises(RagError) as ctx:
            await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(ctx.exception.code, "transport")
        self.assertEqual(log, [])

    async def test_cancellation_before_body_propagates_without_ttfb(self):
        log: list = []
        client = FakeStreamClient(_CancelResponse(200, [sse(("token", '{"text":"never"}'))]))
        with self.assertRaises(asyncio.CancelledError):
            await collect(iter_answer(client, "https://x", "t", "valid question", on_ttfb=lambda ms: log.append(ms)))
        self.assertEqual(log, [])  # no byte → no fabricated timing

    async def test_ttfb_sinks_are_per_call(self):
        a: list = []
        b: list = []
        ca = FakeStreamClient(FakeStreamResponse(200, [sse(("token", '{"text":"a"}'))]))
        cb = FakeStreamClient(FakeStreamResponse(200, [sse(("token", '{"text":"b"}'))]))
        await collect(iter_answer(ca, "https://x", "t", "valid question", on_ttfb=lambda ms: a.append(ms)))
        await collect(iter_answer(cb, "https://x", "t", "valid question", on_ttfb=lambda ms: b.append(ms)))
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()
