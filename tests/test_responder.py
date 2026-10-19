"""Contract tests for the generic HTTP conversational-backend adapter."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.responder import HttpResponder, ResponderError


def sse(*frames: tuple[str, dict]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames)


class FakeResponse:
    def __init__(self, status_code: int = 200, chunks: tuple[str, ...] = ()) -> None:
        self.status_code = status_code
        self._chunks = chunks

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk


class FakeContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return FakeContext(self.response)


def make_responder(response: FakeResponse, **overrides) -> tuple[HttpResponder, FakeClient]:
    client = FakeClient(response)

    async def factory():
        return client

    responder = HttpResponder(
        url="http://backend.test/respond",
        token="secret-token",
        session_id="session-1",
        client_factory=factory,
        **overrides,
    )
    return responder, client


async def collect(responder: HttpResponder, question: str, history=None, should_stop=None):
    chunks = []
    async for chunk in responder.stream(question, history or [], should_stop=should_stop):
        chunks.append(chunk)
    return chunks


class HttpResponderTest(unittest.IsolatedAsyncioTestCase):
    async def test_streams_tokens_in_order_and_sends_contract(self):
        response = FakeResponse(
            200,
            (
                sse(("token", {"text": "Hello "})),
                sse(("token", {"text": "world."}), ("done", {})),
            ),
        )
        responder, client = make_responder(response)
        chunks = await collect(
            responder,
            "What do you do?",
            [{"question": "Earlier?", "answer": "An answer."}],
        )
        self.assertEqual("".join(chunks), "Hello world.")
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://backend.test/respond")
        self.assertEqual(call["json"]["question"], "What do you do?")
        self.assertEqual(
            call["json"]["history"], [{"question": "Earlier?", "answer": "An answer."}]
        )
        self.assertEqual(call["json"]["sessionId"], "session-1")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-token")

    async def test_ask_returns_one_bounded_string(self):
        response = FakeResponse(
            200,
            (sse(("token", {"text": "abcdefgh"}), ("token", {"text": "ijkl"})),),
        )
        responder, _ = make_responder(response, max_answer_chars=6)
        self.assertEqual(await responder.ask("q", []), "abcdef")

    async def test_error_event_aborts_the_turn(self):
        response = FakeResponse(200, (sse(("error", {"message": "backend exploded"})),))
        responder, _ = make_responder(response)
        with self.assertRaises(ResponderError) as ctx:
            await collect(responder, "q")
        self.assertIn("backend exploded", str(ctx.exception))

    async def test_non_2xx_is_never_fabricated_into_an_answer(self):
        responder, _ = make_responder(FakeResponse(503))
        with self.assertRaises(ResponderError):
            await collect(responder, "q")

    async def test_empty_stream_is_an_error(self):
        responder, _ = make_responder(FakeResponse(200, ()))
        with self.assertRaises(ResponderError):
            await collect(responder, "q")

    async def test_cancellation_stops_reading(self):
        response = FakeResponse(200, (sse(("token", {"text": "should not play"})),))
        responder, _ = make_responder(response)
        chunks = await collect(responder, "q", should_stop=lambda: True)
        self.assertEqual(chunks, [])

    async def test_booking_is_optional(self):
        responder, _ = make_responder(FakeResponse(200))
        result = await responder.submit_booking({"slotStart": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("confirmed", result["message"].lower())


if __name__ == "__main__":
    unittest.main()
