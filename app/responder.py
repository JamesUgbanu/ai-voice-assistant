"""Conversational-backend boundary.

The voice service owns audio transport, speech recognition, turn lifecycle, and
speech synthesis. A *responder* turns a finalized utterance plus bounded
history into answer text. The default portfolio responder lives in
``rag_client.py``/``booking.py`` and is wired in ``main.py``; this module adds a
small, self-contained HTTP adapter for any other application.

Generic backend contract
------------------------
``POST VOICE_RESPONDER_URL`` with JSON::

    {"question": "What is your refund policy?",
     "history": [{"question": "...", "answer": "..."}],
     "sessionId": "opaque-session-id"}

The response must be ``text/event-stream`` with frames::

    event: token
    data: {"text": "partial answer text"}

    event: done
    data: {}

``event: error`` with ``data: {"message": "..."}`` aborts the turn. The service
concatenates token text in order, stops reading on cancellation, and bounds the
answer length. SSE is used because it lets TTS start before the full answer is
ready; a non-streaming backend can emit one token frame and then ``done``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from .rag_client import SseStreamParser

#: A sync or async predicate that reports whether the current turn was cancelled.
StopPredicate = Callable[[], bool | Awaitable[bool]]
#: Returns a fresh httpx.AsyncClient (injected so examples/tests need no network).
ClientFactory = Callable[[], Awaitable[object]]


class ResponderError(RuntimeError):
    """A backend failure. The pipeline maps this to ``turn_end: rag_failed``."""


async def _is_stopped(should_stop: StopPredicate | None) -> bool:
    if should_stop is None:
        return False
    result = should_stop()
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


@dataclass
class HttpResponder:
    """Minimal adapter for an application-owned conversational HTTP backend."""

    url: str
    token: str = ""
    timeout_seconds: float = 25.0
    max_answer_chars: int = 4000
    session_id: str = ""
    client_factory: ClientFactory | None = None

    async def ask(self, question: str, history: list[dict]) -> str:
        """Collect the streamed answer (used when a caller needs one string)."""
        parts: list[str] = []
        async for chunk in self.stream(question, history):
            parts.append(chunk)
        return "".join(parts).strip()[: self.max_answer_chars]

    async def stream(
        self,
        question: str,
        history: list[dict],
        should_stop: StopPredicate | None = None,
    ) -> AsyncIterator[str]:
        """Yield answer text from the backend's SSE stream, in order."""
        if self.client_factory is None:
            raise ResponderError("responder client is not configured")
        client = await self.client_factory()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = {
            "question": question,
            "history": list(history),
            "sessionId": self.session_id,
        }
        try:
            context = client.stream(
                "POST",
                self.url,
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # connection setup failed
            raise ResponderError(f"backend request failed: {type(exc).__name__}") from exc

        try:
            response = await context.__aenter__()
        except Exception as exc:
            raise ResponderError(f"backend request failed: {type(exc).__name__}") from exc

        emitted = 0
        error: str | None = None
        try:
            status = getattr(response, "status_code", 0)
            if status < 200 or status >= 300:
                raise ResponderError(f"backend returned HTTP {status}")
            parser = SseStreamParser()
            iterator = response.aiter_text()
            while True:
                try:
                    text = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    raise ResponderError(
                        f"backend stream failed: {type(exc).__name__}"
                    ) from exc
                if await _is_stopped(should_stop):
                    return
                for event in parser.feed(text):
                    if event["event"] == "error":
                        message = event["data"].get("message")
                        error = message if isinstance(message, str) else "backend error"
                        break
                    if event["event"] != "token":
                        continue
                    chunk = event["data"].get("text")
                    if not isinstance(chunk, str) or not chunk:
                        continue
                    remaining = self.max_answer_chars - emitted
                    if remaining <= 0:
                        return
                    chunk = chunk[:remaining]
                    emitted += len(chunk)
                    yield chunk
                if error:
                    break
            if error is None and not await _is_stopped(should_stop):
                for event in parser.close():
                    if event["event"] == "error":
                        message = event["data"].get("message")
                        error = message if isinstance(message, str) else "backend error"
                    elif event["event"] == "token":
                        chunk = event["data"].get("text")
                        if isinstance(chunk, str) and chunk:
                            remaining = self.max_answer_chars - emitted
                            if remaining <= 0:
                                return
                            chunk = chunk[:remaining]
                            emitted += len(chunk)
                            yield chunk
        finally:
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                pass
        if error is not None:
            raise ResponderError(error)
        if emitted == 0:
            raise ResponderError("backend returned no answer text")

    async def submit_booking(self, payload: dict) -> dict:
        """Booking is optional and portfolio-specific in the reference service."""
        return {
            "status": "failed",
            "message": "This conversational backend does not support booking.",
        }
