"""Bounded same-session conversation history for the portfolio RAG call."""
from __future__ import annotations

MAX_HISTORY_TURNS = 6


def append_turn(history: list[dict], question: str, answer: str, max_turns: int = MAX_HISTORY_TURNS) -> None:
    """Append one completed turn; ignore empty values and cap the tail."""
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return
    history.append({"question": q[:500], "answer": a[:2000]})
    if len(history) > max_turns:
        del history[:-max_turns]


def clean_history(raw: object, max_turns: int = MAX_HISTORY_TURNS) -> list[dict]:
    """Validate history supplied in the WebSocket hello payload.

    Only completed question/answer strings survive; fields are bounded to the
    same limits as locally appended turns so a client cannot inflate the prompt.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        q = str(turn.get("question", "")).strip()[:500]
        a = str(turn.get("answer", "")).strip()[:2000]
        if q and a:
            out.append({"question": q, "answer": a})
    return out[-max_turns:]
