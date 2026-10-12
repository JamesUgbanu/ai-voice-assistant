"""Turn lifecycle state machine: pure logic, no I/O, no model imports.

One turn = one user utterance through STT → RAG → TTS → playback.
States: idle → collecting → processing → speaking → idle.
Every transition is explicit; cancellation invalidates the turn id so late
results from STT/RAG/TTS workers can never leak into a newer turn.

Audio accounting: accepted PCM16 bytes per turn are counted here so routes
can enforce the per-turn cap before buffering unbounded input.
"""
from __future__ import annotations

from dataclasses import dataclass


IDLE = "idle"
COLLECTING = "collecting"
PROCESSING = "processing"
SPEAKING = "speaking"

VALID_TRANSITIONS = {
    (IDLE, COLLECTING),
    (COLLECTING, COLLECTING),
    (COLLECTING, PROCESSING),
    (COLLECTING, IDLE),  # empty/silent utterance discarded
    (PROCESSING, SPEAKING),
    (PROCESSING, IDLE),  # failure, fallback, or interrupt before audio
    (SPEAKING, IDLE),  # turn_end, interrupt, or error
    (IDLE, IDLE),
    (SPEAKING, SPEAKING),
    (PROCESSING, PROCESSING),
}


@dataclass
class TurnState:
    state: str = IDLE
    turn_id: str | None = None
    turn_bytes: int = 0
    turn_seq: int = 0
    transcript: str = ""
    cancelled: bool = False

    def transition(self, to: str) -> None:
        if (self.state, to) not in VALID_TRANSITIONS:
            raise ValueError(f"illegal turn transition {self.state} -> {to}")
        self.state = to

    def begin_turn(self) -> str:
        """Start a fresh turn, invalidating anything in flight."""
        self.turn_seq += 1
        self.turn_id = f"t{self.turn_seq}"
        self.turn_bytes = 0
        self.transcript = ""
        self.cancelled = False
        self.state = COLLECTING
        return self.turn_id

    def add_audio(self, byte_count: int, max_turn_bytes: int) -> None:
        if self.state != COLLECTING:
            raise ValueError(f"audio frame in state {self.state}")
        if self.turn_bytes + byte_count > max_turn_bytes:
            raise OverflowError("turn audio budget exceeded")
        self.turn_bytes += byte_count

    def cancel(self) -> str | None:
        """Invalidate the current turn. Returns the cancelled turn id."""
        cancelled_id = self.turn_id if self.state in (COLLECTING, PROCESSING, SPEAKING) else None
        self.cancelled = True
        self.turn_id = None
        self.turn_bytes = 0
        self.transcript = ""
        self.state = IDLE
        return cancelled_id

    def is_current(self, turn_id: str | None) -> bool:
        return (
            not self.cancelled
            and self.turn_id is not None
            and turn_id is not None
            and self.turn_id == turn_id
            and self.state in (PROCESSING, SPEAKING)
        )


def split_sentences(text: str, max_chars: int = 400) -> list[str]:
    """Split response text into speakable chunks for incremental synthesis.
    Pure string splitting — no model calls. Keeps chunks bounded so one
    pathological sentence cannot stall first audio."""
    import re

    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    chunks: list[str] = []
    for part in parts:
        while len(part) > max_chars:
            cut = part.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            chunks.append(part)
    return chunks
