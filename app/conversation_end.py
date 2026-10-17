"""Automatic call termination: conversational-completion detection and the
call-level lifecycle (ACTIVE → ENDING → TERMINATED).

Pure and deterministic — no models, network, or framework — so the decision
rules and lifecycle safety are unit-testable on stdlib alone. The LLM's own
output never terminates a call directly: only an explicit, high-confidence
*machine* decision produced here does, and the orchestrator enforces the
lifecycle transitions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: High-precision explicit farewells. Matched on word boundaries so a word
#: inside another ("goodbyes") does not fire; "thanks" alone is deliberately
#: NOT a trigger (a follow-up usually follows "thanks, what about ...").
_GOODBYE_RE = re.compile(
    r"\b(good\s?bye|bye|see (you|ya)|talk (to you )?later|catch you later|"
    r"speak (to you )?(soon|later)|take care|farewell)\b",
    re.IGNORECASE,
)

#: Explicit "we're finished" confirmations.
_DONE_RE = re.compile(
    r"\b("
    r"that'?s (all|everything|it)|that will be all|"
    r"no more questions|no further questions|i have no (more|further) questions|"
    r"nothing else|no thanks|no thank you|"
    r"i'?m (all )?(done|good|set|finished)|we'?re (all )?(good|done|set|finished)|"
    r"i'?m good now|all good now"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompletionDecision:
    """Structured completion decision — never free text."""

    should_end_call: bool
    reason: str
    confidence: float


def classify_completion(transcript: str, threshold: float = 0.8) -> CompletionDecision:
    """Decide whether the caller's finalized transcript concludes the call.

    Conservative by construction: only explicit, unambiguous closings qualify.
    Silence, thinking aloud, brief pauses, empty transcripts, questions, and
    anything ambiguous return ``should_end_call=False`` — the conversation
    continues. The transcript here is already VAD-gated and finalized upstream,
    so empty/uncertain text never reaches this function on the live path.
    """
    text = (transcript or "").strip()
    if not text:
        return CompletionDecision(False, "empty", 0.0)
    if _GOODBYE_RE.search(text):
        return CompletionDecision(0.95 >= threshold, "goodbye", 0.95 if 0.95 >= threshold else 0.0)
    if _DONE_RE.search(text):
        return CompletionDecision(0.9 >= threshold, "no_more_questions", 0.9 if 0.9 >= threshold else 0.0)
    return CompletionDecision(False, "continuing", 0.0)


class CallLifecycle:
    """Call-level state machine. Every transition is guarded so repeated
    signals are idempotent and cannot produce conflicting cleanup."""

    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    TERMINATED = "TERMINATED"

    def __init__(self) -> None:
        self.state = self.ACTIVE

    @property
    def reversible(self) -> bool:
        """True while ENDING can still be cancelled back to ACTIVE."""
        return self.state == self.ENDING

    def begin_ending(self) -> bool:
        """ACTIVE → ENDING. Returns True only on the first call."""
        if self.state != self.ACTIVE:
            return False
        self.state = self.ENDING
        return True

    def cancel_ending(self) -> bool:
        """ENDING → ACTIVE (caller resumed). No-op unless reversible."""
        if self.state != self.ENDING:
            return False
        self.state = self.ACTIVE
        return True

    def terminate(self) -> bool:
        """Any → TERMINATED. Returns True only on the first call (idempotent)."""
        if self.state == self.TERMINATED:
            return False
        self.state = self.TERMINATED
        return True


def ending_wait_seconds(
    audio_frames: int,
    *,
    chunk_ms: int = 80,
    margin: float = 1.5,
    minimum_s: float = 4.0,
    maximum_s: float = 20.0,
) -> float:
    """Bounded ceiling for waiting on client playback confirmation.

    The server cannot see the speaker; it waits up to ``audio_ms * margin`` plus
    slack for the closing audio to finish, clamped to a hard bound so a lost
    confirmation can never leave the call open indefinitely (the caller
    documents the fallback: terminate when this bound elapses)."""
    audio_s = max(0, audio_frames) * chunk_ms / 1000.0
    return max(minimum_s, min(maximum_s, audio_s * margin + 2.0))
