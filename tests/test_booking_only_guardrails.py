"""Static guardrails for booking-only disclosures.

These catch a future edit that reintroduces an outbound contact path or an
old portfolio prompt. They intentionally scan source/doc strings, not runtime
answers (which are covered by test_booking_flow).
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISCLOSURES = (
    "mailto:",
    "contact section",
    "contact James",
    "linkedin.com/in",
    "twitter.com/",
    "x.com/",
    "calendly.com",
    "@jamesugbanu.com",
)
OLD_PROMPTS = (
    "what would you like to know about my work",
    "what can I tell you about my work",
)


class BookingOnlySourceGuardrailTest(unittest.TestCase):
    def test_no_outbound_contact_channel_in_application_source(self):
        for path in sorted((ROOT / "app").glob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            for needle in DISCLOSURES:
                self.assertNotIn(needle, text, f"{needle!r} in {path.name}")

    def test_no_portfolio_prompt_remains_in_greeting(self):
        text = (ROOT / "app" / "greeting.py").read_text(encoding="utf-8").lower()
        for needle in OLD_PROMPTS:
            self.assertNotIn(needle, text)
        # Social replies live in greeting.py and must not hard-sell the
        # calendar; booking guidance remains in booking.py.
        self.assertNotIn("book a call", text)


if __name__ == "__main__":
    unittest.main()
