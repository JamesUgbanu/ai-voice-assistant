"""Rolling-window incremental STT for voice turns.

The production decoder (faster-whisper / CTranslate2) is a **batch** engine:
it exposes no token or chunk streaming API, so there is no way to decode a
prefix of an utterance and continue decoding from it. This module makes
transcription *incremental in effect, not in the decoder* — it is honest
about that distinction:

- While the user is speaking, the audio accumulated so far is re-decoded on
  a debounce and emitted as ``transcript_partial``. Each partial decodes the
  **whole utterance window so far** (never a suffix), so every hypothesis is
  a complete, coherent sentence and nothing is ever stitched from
  overlapping windows — the failure mode that produces duplicated or
  dropped words at chunk boundaries.
- Because each partial re-decodes the growing window, cost grows with
  utterance length; no new partials are started once the window exceeds
  ``max_audio_seconds`` (the final decode still uses the whole buffer).
- Partials are provisional and replaceable. The authoritative final
  transcript comes from exactly one full-buffer decode in the pipeline, so
  it can never contain window-boundary duplication.
- If the last completed partial happened to cover **exactly** the final
  buffer (the client's ~900 ms silence endpoint leaves a window for this),
  ``settle()`` returns that decode so the turn can skip re-decoding
  identical bytes. This is exact reuse of an identical input, never reuse
  of a partial suffix, so trailing words can never be clipped.

Pure orchestration over an injected ``stt`` backend and an async ``emit``
callback: no models, no network, no framework. Unit-tested on stdlib alone.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class PartialResult:
    """One accepted provisional hypothesis."""

    text: str
    #: The backing decode, kept whole so an exact-length reuse can preserve
    #: its confidence (the greeting fast path keys off ``avg_logprob``).
    transcript: Any
    #: PCM byte length the decode was computed over.
    covered_bytes: int
    #: Monotonic per-turn partial sequence (stale updates must not win).
    seq: int


EmitPartial = Callable[[str, int, str], Optional[Awaitable[None]]]


class PartialTranscriber:
    """Single-flight, debounced, whole-window partial transcription.

    One instance per WebSocket connection; ``begin(turn_id)`` and ``stop()``
    bracket each utterance. Emission is gated on the turn still being the
    active one, so a late decode from a superseded turn is silently dropped.
    """

    def __init__(
        self,
        *,
        stt,
        stt_config,
        emit: EmitPartial,
        enabled: bool = True,
        interval_ms: int = 900,
        min_audio_ms: int = 700,
        max_audio_seconds: float = 15.0,
        sample_rate: int = 16000,
        gate: "asyncio.Semaphore | None" = None,
        on_error: Callable[[str], None] | None = None,
        to_thread: Callable[..., Any] | None = None,
    ) -> None:
        self._stt = stt
        self._cfg = stt_config
        self._emit = emit
        self.enabled = enabled
        self._interval_ms = max(0, int(interval_ms))
        # PCM16 mono: bytes = samples * 2.
        self._min_bytes = max(2, int(min_audio_ms * sample_rate / 1000) * 2)
        self._max_bytes = max(self._min_bytes, int(max_audio_seconds * sample_rate) * 2)
        self._gate = gate
        self._on_error = on_error
        self._to_thread = to_thread or asyncio.to_thread
        self._turn_id: str | None = None
        self._stopped = True
        self._task: "asyncio.Task | None" = None
        self._last_start_ms = -(10**12)
        self._last_considered = 0
        self._result: PartialResult | None = None
        self._seq = 0
        #: Number of partials actually emitted this turn (telemetry).
        self.emitted = 0

    # -- lifecycle ----------------------------------------------------------

    def begin(self, turn_id: str) -> None:
        """Arm for a fresh utterance, discarding any prior turn's state."""
        self._turn_id = turn_id
        self._stopped = False
        self._last_start_ms = -(10**12)
        self._last_considered = 0
        self._result = None
        self._seq = 0
        self.emitted = 0

    async def stop(self) -> None:
        """Disarm; no partial may emit afterwards. Waits (briefly) for an
        in-flight decode so the caller's final STT is not racing it."""
        self._stopped = True
        self._turn_id = None
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 2.0)
            except Exception:
                task.cancel()
        self._task = None
        self._result = None

    # -- scheduling ---------------------------------------------------------

    def maybe_start(self, audio: bytes, turn_id: str, now_ms: int) -> None:
        """Start a partial decode if enough new audio has accrued.

        Deliberately returns early (skip, never queue) when a decode is
        already in flight — the growing buffer will be captured by the next
        call, so audio can never pile up unbounded, and a slow decoder
        applies natural backpressure instead of building a backlog."""
        if self._turn_id != turn_id:
            return
        n = len(audio)
        if not should_start_partial(
            enabled=self.enabled,
            stopped=self._stopped,
            in_flight=self._task is not None and not self._task.done(),
            gate_locked=self._gate is not None and self._gate.locked(),
            audio_bytes=n,
            last_considered_bytes=self._last_considered,
            min_bytes=self._min_bytes,
            max_bytes=self._max_bytes,
            now_ms=now_ms,
            last_start_ms=self._last_start_ms,
            interval_ms=self._interval_ms,
        ):
            return
        self._last_start_ms = now_ms
        self._last_considered = n
        snapshot = bytes(audio)
        self._task = asyncio.ensure_future(self._run(snapshot, turn_id))

    async def _run(self, snapshot: bytes, turn_id: str) -> None:
        acquired = False
        try:
            if self._gate is not None:
                await self._gate.acquire()
                acquired = True
            transcript = await self._to_thread(self._stt.transcribe, snapshot, self._cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # recoverable: never abort the turn
            if self._on_error is not None:
                try:
                    self._on_error(type(exc).__name__)
                except Exception:
                    pass
            return
        finally:
            if acquired:
                self._gate.release()
        # Superseded/disarmed while decoding: drop the hypothesis.
        if self._stopped or self._turn_id != turn_id:
            return
        text = (getattr(transcript, "text", "") or "").strip()
        if not text or getattr(transcript, "no_speech", False):
            return  # hallucination-guarded / empty: nothing to show
        self._seq += 1
        self._result = PartialResult(
            text=text, transcript=transcript, covered_bytes=len(snapshot), seq=self._seq
        )
        self.emitted += 1
        try:
            res = self._emit(text, self._seq, turn_id)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            # A failed emit (client gone) is not a turn failure.
            pass

    # -- finalization -------------------------------------------------------

    async def settle(self, total_bytes: int, *, timeout: float | None = 3.0) -> Any | None:
        """Await any in-flight decode, then return the latest accepted decode
        **only if it covered exactly ``total_bytes``** — a safe, exact reuse;
        otherwise None so the caller runs the authoritative full decode."""
        task = self._task
        if task is not None and not task.done():
            try:
                if timeout is None:
                    await asyncio.shield(task)
                else:
                    await asyncio.wait_for(asyncio.shield(task), timeout)
            except Exception:
                pass
        if self._result is not None and self._result.covered_bytes == total_bytes:
            return self._result.transcript
        return None

    # -- introspection ------------------------------------------------------

    @property
    def last(self) -> PartialResult | None:
        return self._result

    @property
    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()


def should_start_partial(
    *,
    enabled: bool,
    stopped: bool,
    in_flight: bool,
    gate_locked: bool,
    audio_bytes: int,
    last_considered_bytes: int,
    min_bytes: int,
    max_bytes: int,
    now_ms: int,
    last_start_ms: int,
    interval_ms: int,
) -> bool:
    """Pure predicate for the scheduling rule, so the debounce / increment /
    bound policy is unit-testable without a running loop."""
    if not enabled or stopped or in_flight or gate_locked:
        return False
    if audio_bytes < min_bytes or audio_bytes > max_bytes:
        return False
    if audio_bytes - last_considered_bytes < min_bytes:
        return False
    if now_ms - last_start_ms < interval_ms:
        return False
    return True
