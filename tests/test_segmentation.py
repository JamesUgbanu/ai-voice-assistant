"""Segmentation buffer contract: boundaries, bounds, ordering, cancellation.

Pure logic — no models, no network. Deterministic via injected now_ms.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.segmentation import SegmentationBuffer


def feed(buf, *chunks, now=0):
    out = []
    for c in chunks:
        out.extend(buf.push(c, now_ms=now))
    return out


class SentenceBoundaryTest(unittest.TestCase):
    def test_emits_complete_sentence_once_terminated(self):
        b = SegmentationBuffer()
        self.assertEqual(b.push("Hello world.", now_ms=0), [])
        self.assertEqual(b.push(" How are you?", now_ms=1), ["Hello world.", "How are you?"])
        self.assertEqual(b.push(" Great.", now_ms=2), [])
        self.assertEqual(b.flush(), ["Great."])

    def test_bang_and_question_terminate_at_end_of_buffer(self):
        b = SegmentationBuffer()
        self.assertEqual(b.push("Wow!", now_ms=0), ["Wow!"])
        self.assertEqual(b.push("Really?", now_ms=0), ["Really?"])

    def test_preserves_order_across_many_tokens(self):
        b = SegmentationBuffer()
        out = []
        for tok in ["One. ", "Two! ", "Three? ", "Four."]:
            out.extend(b.push(tok, now_ms=0))
        out.extend(b.flush())
        self.assertEqual(out, ["One.", "Two!", "Three?", "Four."])

    def test_unicode_and_fullwidth_terminators(self):
        b = SegmentationBuffer()
        out = feed(b, "Café déjà vu. ", "Très bien!")
        self.assertEqual(out, ["Café déjà vu.", "Très bien!"])
        b2 = SegmentationBuffer()
        self.assertEqual(b2.push("你好。", now_ms=0), ["你好。"])

    def test_trailing_whitespace_is_trimmed(self):
        b = SegmentationBuffer()
        self.assertEqual(b.push("Hi there.    ", now_ms=0), ["Hi there."])
        self.assertEqual(b.pending, "")


class AbbreviationTest(unittest.TestCase):
    def test_never_breaks_after_titles_ordinals_acronyms_or_decimals(self):
        cases = [
            ("Mr. Smith went home. ", "Mr. Smith went home."),
            ("Dr. Jones arrived at 3.14 sharp. ", "Dr. Jones arrived at 3.14 sharp."),
            ("The U.S. Army is here. ", "The U.S. Army is here."),
            ("See e.g. the appendix. ", "See e.g. the appendix."),
            ("J. R. R. Tolkien wrote it. ", "J. R. R. Tolkien wrote it."),
        ]
        for stream, expected_first in cases:
            b = SegmentationBuffer()
            out = feed(b, stream)
            self.assertEqual(out, [expected_first], stream)

    def test_protected_dot_waits_for_following_text(self):
        b = SegmentationBuffer()
        # A lone trailing "Mr." must not be spoken as a sentence.
        self.assertEqual(b.push("Mr.", now_ms=0), [])
        self.assertEqual(b.push(" Smith", now_ms=1), [])


class ClauseAndBoundTest(unittest.TestCase):
    def test_commas_do_not_chop_the_stream(self):
        b = SegmentationBuffer(clause_trigger_chars=1000)
        self.assertEqual(feed(b, "Well, I think, maybe, we could, go."), [])
        self.assertEqual(b.flush(), ["Well, I think, maybe, we could, go."])

    def test_clause_boundary_used_once_buffer_is_substantial(self):
        b = SegmentationBuffer(max_chars=400, clause_trigger_chars=20, min_chars=5)
        out = feed(b, "First part of a thought; ", "and it continues onward")
        self.assertEqual(out, ["First part of a thought;"])

    def test_soft_length_bound_breaks_at_last_space(self):
        b = SegmentationBuffer(max_chars=40, clause_trigger_chars=9999, min_chars=5)
        out = feed(b, "word " * 12)  # 60 chars, no terminator
        self.assertTrue(out)
        self.assertTrue(all(len(s) <= 40 for s in out))
        # Never splits mid-word.
        self.assertTrue(all("word" in s or s == "" for s in out))

    def test_hard_cap_for_unbroken_run(self):
        b = SegmentationBuffer(max_chars=40, hard_max_chars=64, min_chars=5)
        out = feed(b, "x" * 200)
        out.extend(b.flush())
        self.assertTrue(out)
        self.assertEqual("".join(out), "x" * 200)
        self.assertLessEqual(max(len(s) for s in out), 64)


class MaxWaitTest(unittest.TestCase):
    def test_poll_emits_after_max_wait(self):
        b = SegmentationBuffer(max_wait_ms=1000, min_chars=5)
        self.assertEqual(b.push("this is a stalled thought", now_ms=0), [])
        self.assertEqual(b.poll(now_ms=500), [])
        self.assertEqual(b.poll(now_ms=1001), ["this is a stalled thought"])

    def test_max_wait_never_emits_tiny_fragment(self):
        b = SegmentationBuffer(max_wait_ms=1000, min_chars=12)
        self.assertEqual(b.push("ok", now_ms=0), [])
        self.assertEqual(b.poll(now_ms=5000), [])
        self.assertEqual(b.flush(), ["ok"])

    def test_wait_window_resets_after_a_partial_emit(self):
        b = SegmentationBuffer(max_wait_ms=1000, min_chars=5, clause_trigger_chars=10)
        out = feed(b, "alpha; beta gamma delta", now=0)
        self.assertEqual(out, ["alpha;"])
        # The remainder has a fresh wait window.
        self.assertEqual(b.poll(now_ms=999), [])
        self.assertEqual(b.poll(now_ms=1001), ["beta gamma delta"])


class FlushResetTest(unittest.TestCase):
    def test_flush_emits_remainder_and_clears(self):
        b = SegmentationBuffer()
        feed(b, "Trailing text with no terminator")
        self.assertEqual(b.flush(), ["Trailing text with no terminator"])
        self.assertEqual(b.flush(), [])
        self.assertEqual(b.pending, "")

    def test_reset_discards_everything(self):
        b = SegmentationBuffer()
        feed(b, "Half a sentence that will, ")
        b.reset()
        self.assertEqual(b.pending, "")
        self.assertEqual(b.flush(), [])

    def test_whitespace_only_never_emits(self):
        b = SegmentationBuffer()
        self.assertEqual(b.push("   ", now_ms=0), [])
        self.assertEqual(b.flush(), [])


if __name__ == "__main__":
    unittest.main()
