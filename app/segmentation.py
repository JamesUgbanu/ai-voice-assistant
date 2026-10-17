"""Bounded incremental segmentation buffer for streaming TTS.

The portfolio RAG streams answer text token by token. Synthesizing each
token is wasteful and choppy; synthesizing only after the full answer
defeats first-audio latency. This buffer turns the token stream into
natural, ordered speakable segments:

- **Sentence boundaries first** (`. ! ? …`), so speech pauses where a
  reader would — but never after a protected abbreviation (`Mr.`), a
  dotted acronym (`U.S.`), a single initial (`J.`), or a decimal (`3.14`).
- **Clause/whitespace fallback** when waiting for a full sentence would
  delay first audio past `max_wait_ms` or the buffer past `max_chars`.
- **Bounded**: never grows without limit; a hard cap forces a break even
  mid-sentence when no whitespace exists.
- **No mid-token cuts**: breaks happen only after terminal punctuation or
  at whitespace, so a partial word never reaches the synthesizer.
- **Order preserved**: segments are emitted in the exact stream order.
- **Cancellation-safe**: `reset()` discards everything; the caller stops
  feeding on cancel, so no stale turn can be spoken.

Pure logic — no I/O, no models, no clock dependence beyond an injected
`now_ms` used to drive the max-wait rule deterministically in tests.
"""
from __future__ import annotations

import re
import time

# Sentence terminators (ASCII + common CJK fullwidth forms).
_SENTENCE_TERMINALS = ".!?\u2026\u3002\uff01\uff1f"
# Safe clause terminators: strong enough to pause on, never a comma.
_CLAUSE_TERMINALS = ";:\u2014\u2013"  # ; : em-dash en-dash
# Abbreviations whose trailing "." must never end a segment.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc",
    "e.g", "i.e", "no", "inc", "ltd", "co", "corp", "dept", "est", "approx",
    "fig", "vol", "al", "am", "pm", "a.m", "p.m", "u.s", "u.k", "u.n", "i.e",
}
_TRAILING_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")


class SegmentationBuffer:
    """Accumulate streaming text; emit ordered, speakable segments."""

    def __init__(
        self,
        max_chars: int = 400,
        hard_max_chars: int = 640,
        min_chars: int = 12,
        max_wait_ms: int = 1200,
        clause_trigger_chars: int = 80,
        first_max_wait_ms: int | None = None,
        now_ms=None,
    ) -> None:
        self.max_chars = max(16, max_chars)
        self.hard_max_chars = max(self.max_chars, hard_max_chars)
        self.min_chars = max(1, min_chars)
        self.max_wait_ms = max(1, max_wait_ms)
        # A shorter wait before the FIRST segment only: speech starts sooner
        # (the measured first-audio delay), while later segments keep the full
        # `max_wait_ms` so they stay natural and sentence-sized. Defaults to
        # `max_wait_ms` (previous behavior) when unset.
        self.first_max_wait_ms = max(1, first_max_wait_ms) if first_max_wait_ms is not None else self.max_wait_ms
        self.clause_trigger_chars = max(1, clause_trigger_chars)
        self._now = now_ms or (lambda: int(time.monotonic() * 1000))
        self._buf = ""
        self._pending_since: int | None = None
        self._emitted_any = False

    # -- public API ---------------------------------------------------------

    @property
    def pending(self) -> str:
        return self._buf

    def push(self, chunk: str, now_ms: int | None = None) -> list[str]:
        """Feed streamed text; return any segments now ready, in order."""
        now = self._now() if now_ms is None else now_ms
        if chunk:
            if not self._buf:
                self._pending_since = now
            self._buf += chunk
        out: list[str] = []
        while True:
            seg = self._take_ready(now)
            if seg is None:
                break
            out.append(seg)
        return out

    def poll(self, now_ms: int | None = None) -> list[str]:
        """Emit on the max-wait rule without new input (call on a timer when
        the upstream stream stalls)."""
        now = self._now() if now_ms is None else now_ms
        out: list[str] = []
        while True:
            seg = self._take_ready(now)
            if seg is None:
                break
            out.append(seg)
        return out

    def flush(self) -> list[str]:
        """End of stream: emit the remaining text as one final segment."""
        text = self._buf.strip()
        self._buf = ""
        self._pending_since = None
        return [text] if text else []

    def reset(self) -> None:
        """Discard all buffered text (turn cancelled / superseded)."""
        self._buf = ""
        self._pending_since = None
        self._emitted_any = False

    # -- internals ----------------------------------------------------------

    def _take_ready(self, now: int) -> str | None:
        buf = self._buf
        if not buf.strip():
            if buf:
                self._buf = ""
                self._pending_since = None
            return None

        # 1. Sentence boundary — the natural, preferred break.
        end = self._sentence_boundary(buf)
        if end is not None:
            return self._emit(buf, end, now)

        # 2. Hard length bound — force a break rather than buffer forever.
        if len(buf) >= self.hard_max_chars:
            cut = self._last_space(buf, self.hard_max_chars)
            if cut <= 0:
                cut = self.hard_max_chars  # no whitespace: break mid-run (rare)
            return self._emit(buf, cut, now)

        # 3. Soft length bound — break at the last whitespace if we must.
        if len(buf) >= self.max_chars:
            cut = self._last_space(buf, self.max_chars)
            if cut > 0 and cut >= self.min_chars:
                return self._emit(buf, cut, now)

        # 4. Clause boundary — only once the buffer is already substantial,
        #    so we never emit a comma-choked stream of tiny fragments.
        if len(buf) >= self.clause_trigger_chars:
            c = self._last_clause(buf)
            if c > 0 and c >= self.min_chars:
                return self._emit(buf, c, now)

        # 5. Max wait — the stream stalled; emit everything buffered so the
        #    final word isn't held back indefinitely. The first segment uses a
        #    shorter wait so speech starts sooner; later segments wait longer
        #    for a natural sentence boundary.
        limit = self.max_wait_ms if self._emitted_any else self.first_max_wait_ms
        waited = self._pending_since is not None and (now - self._pending_since) >= limit
        if waited and len(buf.strip()) >= self.min_chars:
            return self._emit(buf, len(buf), now)

        return None

    def _emit(self, buf: str, end: int, now: int) -> str:
        seg = buf[:end].strip()
        rest = buf[end:].lstrip()
        self._buf = rest
        self._emitted_any = True
        # Remaining text starts a fresh wait window; empty buffer clears it.
        self._pending_since = now if rest else None
        return seg

    @staticmethod
    def _last_space(text: str, limit: int) -> int:
        idx = min(limit, len(text))
        cut = text.rfind(" ", 0, idx)
        if cut < 0:
            return -1
        return cut

    @staticmethod
    def _last_clause(text: str) -> int:
        best = -1
        for i, ch in enumerate(text):
            if ch in _CLAUSE_TERMINALS:
                best = i + 1
        return best

    def _sentence_boundary(self, text: str) -> int | None:
        """Index just past the earliest safe sentence terminator, else None."""
        n = len(text)
        for i, ch in enumerate(text):
            if ch not in _SENTENCE_TERMINALS:
                continue
            nxt = text[i + 1] if i + 1 < n else ""
            if ch == ".":
                # A trailing "." may still be an abbreviation/decimal whose
                # next character hasn't streamed in yet: wait for more text.
                if not nxt:
                    return None
                if not nxt.isspace():
                    continue
                if self._dot_is_protected(text, i):
                    continue
            elif nxt and not nxt.isspace() and nxt not in _SENTENCE_TERMINALS:
                # "Wow!" fine at end; "Wow!Great" (no space) is not a break.
                if ch != ".":
                    continue
            return i + 1
        return None

    @staticmethod
    def _dot_is_protected(text: str, i: int) -> bool:
        before = text[:i]
        # Decimal: digit '.' digit.
        if i + 1 < len(text) and before[-1:].isdigit() and text[i + 1].isdigit():
            return True
        m = _TRAILING_TOKEN_RE.search(before)
        if not m:
            return False
        token = m.group(1)
        if "." in token:  # dotted acronym: U.S, e.g, i.e
            return True
        if len(token) == 1:  # single initial: J.
            return True
        return token.lower() in _ABBREVIATIONS
