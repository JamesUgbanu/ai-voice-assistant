"""Client for the existing portfolio RAG API. The voice service is a thin
client of POST {RAG_API_URL} — it never reimplements retrieval, reranking,
embeddings, or grounding. HTTP transport is injected so unit tests run
without network or third-party packages (httpx is imported lazily, only
when building the real client).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class RagError(Exception):
    """Machine-readable RAG failure; never leaks upstream internals."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RagAnswer:
    text: str
    route: str = ""
    cached: bool = False
    truncated: bool = False


class SseStreamParser:
    """Incremental SSE parser for a live response body.

    The portfolio sends `event: <type>\\ndata: <json>\\n\\n` frames. Network
    delivery can split a frame across arbitrary chunk boundaries, so this
    parser buffers the trailing incomplete block and only emits complete
    frames. `close()` drains a final frame that lacked the blank-line
    terminator. Unknown event types and non-JSON payloads are ignored
    (forward compatible with new portfolio events).
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list:
        if not chunk:
            return []
        self._buffer += chunk.replace("\r\n", "\n")
        events: list = []
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            parsed = _parse_frame(block)
            if parsed is not None:
                events.append(parsed)
        return events

    def close(self) -> list:
        block, self._buffer = self._buffer, ""
        if not block.strip():
            return []
        parsed = _parse_frame(block)
        return [parsed] if parsed is not None else []


def _parse_frame(block: str) -> dict | None:
    """Parse one `event:`/`data:` block → {"event", "data"} or None."""
    import json

    event_type: str | None = None
    data_lines: list = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if not event_type:
        return None
    try:
        data = json.loads("\n".join(data_lines)) if data_lines else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {"event": event_type, "data": data}


def parse_sse_events(body: str) -> dict:
    """Fold a completed SSE stream into {answer, route, cached, sources}.
    Pure function over the verified portfolio contract:
    `event: <type>\\ndata: <json>` frames with types meta/token/sources/
    done/error. Unknown event types are ignored (forward compatible)."""
    import json

    answer_parts: list = []
    route = ""
    cached = False
    sources: list = []
    error: str | None = None
    for block in body.split("\n\n"):
        event_type: str | None = None
        data_lines: list = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not event_type:
            continue
        try:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if event_type == "token" and isinstance(data.get("text"), str):
            answer_parts.append(data["text"])
        elif event_type == "sources" and isinstance(data.get("sources"), list):
            sources = data["sources"]
        elif event_type == "meta":
            if isinstance(data.get("route"), str):
                route = data["route"]
            cached = bool(data.get("cached", False))
        elif event_type == "error" and isinstance(data.get("message"), str):
            error = data["message"]
    return {"answer": "".join(answer_parts), "route": route, "cached": cached, "sources": sources, "error": error}


def build_rag_body(question: str, history: list | None = None, mode: str | None = None) -> dict:
    """Request payload honoring the portfolio schema (question 3..500 chars,
    history capped at 6 turns). `mode="voice"` asks the portfolio for a
    concise spoken answer and isolates its cache; omitted keeps website
    behavior. Raises RagError on invalid input."""
    q = (question or "").strip()
    if len(q) < 3:
        raise RagError("bad_question", "question is too short")
    clean_history = []
    for turn in (history or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        tq = str(turn.get("question", ""))[:500]
        ta = str(turn.get("answer", ""))[:2000]
        if tq.strip() and ta.strip():
            clean_history.append({"question": tq, "answer": ta})
    body = {"question": q[:500], "history": clean_history, "debug": False}
    if mode:
        body["mode"] = mode
    return body


async def fetch_answer(
    client,
    base_url: str,
    service_token: str,
    question: str,
    history: list | None = None,
    timeout_seconds: float = 25.0,
    max_answer_chars: int = 4000,
) -> RagAnswer:
    """POST one question to the portfolio RAG and fold the SSE stream.
    `client` is an httpx-like object with .post(); tests inject fakes.
    Raises RagError on transport/HTTP/RAG failures — never returns
    fabricated text (callers fall back to a spoken apology instead)."""
    if not base_url or not service_token:
        raise RagError("not_configured", "RAG endpoint or service token missing")
    body = build_rag_body(question, history)
    try:
        response = await client.post(
            base_url.rstrip("/"),
            json=body,
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise RagError("transport", f"RAG request failed: {type(exc).__name__}")
    if response.status_code == 401:
        raise RagError("unauthorized", "RAG service credential rejected")
    if response.status_code == 429:
        raise RagError("rate_limited", "RAG service is busy")
    if response.status_code < 200 or response.status_code >= 300:
        raise RagError("http_error", f"RAG service returned {response.status_code}")
    parsed = parse_sse_events(response.text)
    if parsed["error"]:
        raise RagError("rag_error", "RAG pipeline reported an error")
    text = parsed["answer"].strip()
    if not text:
        raise RagError("empty_answer", "RAG returned no answer text")
    truncated = len(text) > max_answer_chars
    return RagAnswer(text=text[:max_answer_chars], route=parsed["route"], cached=parsed["cached"], truncated=truncated)


async def iter_answer(
    client,
    base_url: str,
    service_token: str,
    question: str,
    history: list | None = None,
    timeout_seconds: float = 25.0,
    max_answer_chars: int = 4000,
    should_stop=None,
    mode: str | None = None,
):
    """Stream answer text from the portfolio RAG as it arrives.

    Async generator yielding token strings in order, so the caller can feed
    a segmentation buffer and start TTS before the answer completes. The
    injected `client` must expose ``stream(method, url, json=, headers=,
    timeout=)`` as an async context manager whose response exposes
    ``status_code`` and ``aiter_text()`` (httpx satisfies this; tests inject
    a fake). `should_stop` is polled between network chunks; when it returns
    true the generator stops early, leaving already-yielded text with the
    caller. Raises RagError for config/transport/HTTP/rag failures — never
    yields fabricated text."""
    if not base_url or not service_token:
        raise RagError("not_configured", "RAG endpoint or service token missing")
    body = build_rag_body(question, history, mode)

    async def stopped() -> bool:
        if should_stop is None:
            return False
        result = should_stop()
        return await result if asyncio.iscoroutine(result) else bool(result)

    parser = SseStreamParser()
    emitted = 0
    error_event: str | None = None
    stopped_early = False
    try:
        ctx = client.stream(
            "POST",
            base_url.rstrip("/"),
            json=body,
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise RagError("transport", f"RAG request failed: {type(exc).__name__}")
    # Only network acquisition/reads are wrapped as transport failures; the
    # yields below sit outside those handlers so an exception raised by the
    # consumer (e.g. a TTS failure) is never reclassified as a RAG error.
    try:
        response = await ctx.__aenter__()
    except Exception as exc:
        raise RagError("transport", f"RAG request failed: {type(exc).__name__}")
    try:
        status = response.status_code
        if status == 401:
            raise RagError("unauthorized", "RAG service credential rejected")
        if status == 429:
            raise RagError("rate_limited", "RAG service is busy")
        if status < 200 or status >= 300:
            raise RagError("http_error", f"RAG service returned {status}")
        iterator = response.aiter_text()
        while True:
            try:
                text = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except Exception as exc:
                raise RagError("transport", f"RAG request failed: {type(exc).__name__}")
            for ev in parser.feed(text):
                if await stopped():
                    stopped_early = True
                    break
                ev_type, data = ev["event"], ev["data"]
                if ev_type == "token" and isinstance(data.get("text"), str):
                    chunk = data["text"]
                    if emitted + len(chunk) > max_answer_chars:
                        chunk = chunk[: max(0, max_answer_chars - emitted)]
                    if chunk:
                        emitted += len(chunk)
                        yield chunk
                elif ev_type == "error" and isinstance(data.get("message"), str):
                    error_event = data["message"]
                    break
            if error_event or stopped_early:
                break
        if not error_event and not stopped_early:
            for ev in parser.close():
                if ev["event"] == "token" and isinstance(ev["data"].get("text"), str):
                    chunk = ev["data"]["text"]
                    if emitted + len(chunk) > max_answer_chars:
                        chunk = chunk[: max(0, max_answer_chars - emitted)]
                    if chunk:
                        emitted += len(chunk)
                        yield chunk
    finally:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass
    if stopped_early:
        return
    if error_event:
        raise RagError("rag_error", "RAG pipeline reported an error")
    if emitted == 0:
        raise RagError("empty_answer", "RAG returned no answer text")
