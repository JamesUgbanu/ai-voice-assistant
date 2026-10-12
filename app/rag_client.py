"""Client for the existing portfolio RAG API. The voice service is a thin
client of POST {RAG_API_URL} — it never reimplements retrieval, reranking,
embeddings, or grounding. HTTP transport is injected so unit tests run
without network or third-party packages (httpx is imported lazily, only
when building the real client).
"""
from __future__ import annotations

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


def build_rag_body(question: str, history: list | None = None) -> dict:
    """Request payload honoring the portfolio schema (question 3..500 chars,
    history capped at 6 turns). Raises RagError on invalid input."""
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
    return {"question": q[:500], "history": clean_history, "debug": False}


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
