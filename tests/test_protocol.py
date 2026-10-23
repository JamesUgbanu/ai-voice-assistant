"""Contract tests: protocol envelopes, audio framing, error taxonomy."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.protocol import (
    CLIENT_EVENTS,
    SERVER_CAPABILITIES,
    SERVER_EVENTS,
    is_fatal_error,
    make_envelope,
    should_answer_ping,
    validate_audio_frame,
    validate_envelope,
    validate_playback_metrics,
    ProtocolError,
)


class EnvelopeTest(unittest.TestCase):
    def test_accepts_wellformed_client_events(self):
        for t in ("hello", "text_message", "audio_frame", "end_utterance", "interrupt", "ping", "end_session"):
            env = validate_envelope({"v": 1, "type": t, "seq": 3, "turn_id": None, "payload": {}},
                                     max_bytes=1000, from_client=True)
            self.assertEqual(env["type"], t)

    def test_rejects_unknown_type_version_and_shapes(self):
        bad = [
            {"v": 2, "type": "hello"},
            {"v": 1, "type": "nonsense"},
            {"v": 1, "type": "audio_output"},  # server-only direction
            {"v": 1, "type": "hello", "seq": -1},
            {"v": 1, "type": "hello", "turn_id": "x" * 65},
            {"v": 1},
            "string",
            None,
            [],
        ]
        for m in bad:
            with self.assertRaises(ProtocolError, msg=repr(m)):
                validate_envelope(m, max_bytes=1000, from_client=True)

    def test_server_envelope_builder_rejects_unknown(self):
        with self.assertRaises(ProtocolError):
            make_envelope("audio_input")
        env = make_envelope("turn_end", {"reason": "complete"}, turn_id="t1")
        self.assertEqual(env, {"v": 1, "type": "turn_end", "seq": 0, "turn_id": "t1",
                               "payload": {"reason": "complete"}})

    def test_error_taxonomy(self):
        self.assertTrue(is_fatal_error("quota_exhausted"))
        self.assertTrue(is_fatal_error("unauthorized"))
        self.assertFalse(is_fatal_error("bad_message"))
        self.assertFalse(is_fatal_error("stt_failed"))


class AudioFrameTest(unittest.TestCase):
    def _b64(self, nbytes: int) -> str:
        import base64
        return base64.b64encode(b"\x01\x02" * (nbytes // 2)).decode()

    def test_accepts_valid_pcm16(self):
        raw = validate_audio_frame({"audio_b64": self._b64(320)}, max_frame_bytes=1024,
                                   max_turn_bytes=10_000, turn_bytes_so_far=0)
        self.assertEqual(len(raw), 320)

    def test_rejects_garbage(self):
        import base64
        odd_bytes = base64.b64encode(b"\x01\x02\x03").decode()
        for payload in ({}, {"audio_b64": ""}, {"audio_b64": "!!!not-base64!!!"},
                        {"audio_b64": odd_bytes}):
            with self.assertRaises(ProtocolError, msg=repr(payload)[:40]):
                validate_audio_frame(payload, max_frame_bytes=1024, max_turn_bytes=10_000,
                                     turn_bytes_so_far=0)

    def test_enforces_frame_and_turn_caps(self):
        with self.assertRaises(ProtocolError) as ctx:
            validate_audio_frame({"audio_b64": self._b64(2048)}, max_frame_bytes=1024,
                                 max_turn_bytes=10_000, turn_bytes_so_far=0)
        self.assertEqual(ctx.exception.code, "audio_too_large")
        with self.assertRaises(ProtocolError) as ctx2:
            validate_audio_frame({"audio_b64": self._b64(320)}, max_frame_bytes=4096,
                                 max_turn_bytes=500, turn_bytes_so_far=400)
        self.assertEqual(ctx2.exception.code, "turn_too_long")


class ServerCapabilitiesTest(unittest.TestCase):
    def test_playback_complete_is_advertised(self):
        # Clients gate the optional event on this capability, so an older server
        # (which does not advertise it) is never sent an unknown event.
        self.assertIn("playback_complete", SERVER_CAPABILITIES)
        self.assertIn("text_message", SERVER_CAPABILITIES)
        self.assertIn("assistant_text", SERVER_CAPABILITIES)
        self.assertIn("playback_metrics", SERVER_CAPABILITIES)

    def test_session_created_carries_capabilities(self):
        env = make_envelope(
            "session_created",
            {"session_id": "s", "max_seconds": 300, "capabilities": sorted(SERVER_CAPABILITIES)},
        )
        self.assertIn("playback_complete", env["payload"]["capabilities"])


class PlaybackMetricsTest(unittest.TestCase):
    def test_accepts_only_bounded_numeric_fields(self):
        values = validate_playback_metrics({
            "underruns": 3,
            "queue_max_ms": 480,
            "sample_rate": 24000,
            "notes": "private text must be dropped",
            "underruns_bad": True,
            "decode_errors": -1,
            "dropped_frames": "9",
        })
        self.assertEqual(values, {
            "underruns": 3,
            "queue_max_ms": 480,
            "sample_rate": 24000,
        })

    def test_non_object_is_empty(self):
        self.assertEqual(validate_playback_metrics("secret"), {})
        self.assertEqual(validate_playback_metrics(None), {})

    def test_event_is_accepted_from_client(self):
        env = validate_envelope(
            {"v": 1, "type": "playback_metrics", "seq": 4, "turn_id": "t1",
             "payload": {"underruns": 1}},
            max_bytes=1000, from_client=True,
        )
        self.assertEqual(env["type"], "playback_metrics")


class PingThrottleTest(unittest.TestCase):
    def test_first_ping_always_answered(self):
        self.assertTrue(should_answer_ping(0.0, 100.0))

    def test_rapid_repeat_ignored(self):
        self.assertFalse(should_answer_ping(100.0, 100.5))

    def test_answered_again_after_interval(self):
        self.assertTrue(should_answer_ping(100.0, 101.0))
        self.assertTrue(should_answer_ping(100.0, 200.0))


if __name__ == "__main__":
    unittest.main()
