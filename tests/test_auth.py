"""Contract tests: HMAC tickets, visitor ids, IP hashing."""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.auth import (
    COOKIE_NAME,
    hash_ip,
    is_valid_id,
    issue_ticket,
    new_visitor_id,
    verify_ticket,
)

SECRET = "test-secret-that-is-long-enough-for-hmac"


class TicketTest(unittest.TestCase):
    def test_round_trip(self):
        now = time.time()
        tok = issue_ticket(SECRET, "sess123", now + 60)
        self.assertEqual(verify_ticket(SECRET, tok, now=now), "sess123")

    def test_rejects_tampering_expiry_and_wrong_secret(self):
        now = time.time()
        tok = issue_ticket(SECRET, "sess123", now + 60)
        self.assertIsNone(verify_ticket("other-secret", tok, now=now))
        self.assertIsNone(verify_ticket(SECRET, tok + "x", now=now))
        self.assertIsNone(verify_ticket(SECRET, tok, now=now + 61))
        self.assertIsNone(verify_ticket(SECRET, "garbage", now=now))
        self.assertIsNone(verify_ticket(SECRET, "", now=now))
        self.assertIsNone(verify_ticket("", tok, now=now))
        self.assertIsNone(verify_ticket(SECRET, None, now=now))

    def test_ticket_carries_no_secrets_or_pii(self):
        import base64
        now = time.time()
        tok = issue_ticket(SECRET, "sess123", now + 60)
        body = base64.urlsafe_b64decode(tok.split(".")[0] + "==")
        self.assertNotIn(b"test-secret", body)
        self.assertIn(b"sess123", body)

    def test_visitor_ids_are_opaque_and_validated(self):
        vid = new_visitor_id()
        self.assertTrue(is_valid_id(vid))
        for bad in ("", "x", "not a uuid!!", "a" * 200, None, 42):
            self.assertFalse(is_valid_id(bad))

    def test_ip_hash_is_stable_salted_and_pii_free(self):
        self.assertEqual(hash_ip("1.2.3.4", "s"), hash_ip("1.2.3.4", "s"))
        self.assertNotEqual(hash_ip("1.2.3.4", "s"), hash_ip("1.2.3.4", "other"))
        self.assertNotIn("1.2.3.4", hash_ip("1.2.3.4", "s"))

    def test_cookie_name_constant(self):
        self.assertEqual(COOKIE_NAME, "voice_vid")


if __name__ == "__main__":
    unittest.main()
