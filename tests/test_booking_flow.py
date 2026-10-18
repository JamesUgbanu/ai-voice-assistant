import unittest
from app import booking


SLOTS = [
    {"start": "2026-10-19T09:00:00Z", "label": "Mon 19 Oct, 10:00", "durationMinutes": 30},
    {"start": "2026-10-19T09:30:00Z", "label": "Mon 19 Oct, 10:30", "durationMinutes": 30},
]


class BookingFlowTest(unittest.TestCase):
    def test_selects_ordinal_and_clock_time(self):
        self.assertEqual(booking.select_slot("the second one", SLOTS), SLOTS[1])
        self.assertEqual(booking.select_slot("10:00 please", SLOTS), SLOTS[0])
        self.assertIsNone(booking.select_slot("maybe another time", SLOTS))

    def test_parses_details_and_confirmation(self):
        self.assertEqual(booking.parse_email("ada@example.com"), "ada@example.com")
        self.assertEqual(booking.parse_name("My name is Ada"), "Ada")
        self.assertTrue(booking.wants_confirm("yes, confirm it"))
        self.assertTrue(booking.wants_cancel("no thanks"))
        self.assertFalse(booking.wants_confirm("not sure"))

    def test_never_renders_confirmation_for_failed_or_uncertain(self):
        self.assertIn("couldn't verify", booking.result_message({"status": "uncertain"}, SLOTS[0], "Africa/Lagos"))
        self.assertNotIn("confirmed", booking.result_message({"status": "uncertain"}, SLOTS[0], "Africa/Lagos"))
        self.assertIn("could not be completed", booking.result_message({"status": "failed"}, SLOTS[0], "Africa/Lagos"))
        self.assertIn("no longer available", booking.result_message({"status": "slot_unavailable"}, SLOTS[0], "Africa/Lagos"))
        self.assertIn("Your call is confirmed", booking.result_message({"status": "confirmed"}, SLOTS[0], "Africa/Lagos"))

    def test_confirmation_prompt_reads_back_slot_and_identity(self):
        prompt = booking.confirmation_prompt(SLOTS[0], "Ada", "ada@example.com", "Africa/Lagos")
        self.assertIn("Mon 19 Oct, 10:00", prompt)
        self.assertIn("Ada", prompt)
        self.assertIn("Africa/Lagos", prompt)
        self.assertIn("Should I book it?", prompt)


if __name__ == "__main__":
    unittest.main()


class VoiceBookingSessionTest(unittest.TestCase):
    OFFER = {
        "enabled": True,
        "timezone": "Africa/Lagos",
        "durationMinutes": 30,
        "slots": [
            {"start": "2026-10-19T09:00:00Z", "label": "Mon 19 Oct, 10:00", "durationMinutes": 30},
            {"start": "2026-10-19T09:30:00Z", "label": "Mon 19 Oct, 10:30", "durationMinutes": 30},
        ],
    }

    def test_availability_only_does_not_book(self):
        s = booking.VoiceBookingSession(); s.capture_offer(self.OFFER)
        reply, payload = s.handle("what about next week?", "Africa/Lagos")
        self.assertIsNone(reply)
        self.assertIsNone(payload)
        self.assertEqual(s.stage, "idle")

    def test_requires_details_and_explicit_confirmation(self):
        s = booking.VoiceBookingSession(); s.capture_offer(self.OFFER)
        reply, payload = s.handle("first one", "Africa/Lagos")
        self.assertIn("name", reply or "")
        self.assertIsNone(payload)
        reply, payload = s.handle("Ada, ada@example.com", "Africa/Lagos")
        self.assertIn("Should I book it?", reply or "")
        self.assertIsNone(payload)
        reply, payload = s.handle("yes", "Africa/Lagos")
        self.assertIsNone(reply)
        self.assertTrue(payload and payload["confirmed"])
        self.assertEqual(payload["name"], "Ada")
        self.assertEqual(payload["email"], "ada@example.com")
        self.assertEqual(payload["timeZone"], "Africa/Lagos")
        # Once submitted, a repeated yes does not create a second payload.
        reply2, payload2 = s.handle("yes", "Africa/Lagos")
        self.assertIsNone(reply2)
        self.assertIsNone(payload2)

    def test_correction_before_confirmation(self):
        s = booking.VoiceBookingSession(); s.capture_offer(self.OFFER)
        s.handle("second", "Africa/Lagos")
        s.handle("Ada, ada@example.com", "Africa/Lagos")
        reply, payload = s.handle("actually use ada2@example.com", "Africa/Lagos")
        self.assertIn("ada2@example.com", reply or "")
        self.assertIsNone(payload)
        _, payload = s.handle("yes", "Africa/Lagos")
        self.assertEqual(payload and payload["email"], "ada2@example.com")

    def test_cancel_and_foreign_session_isolation(self):
        a = booking.VoiceBookingSession(); a.capture_offer(self.OFFER)
        b = booking.VoiceBookingSession()
        a.handle("first one", "Africa/Lagos")
        self.assertEqual(b.stage, "idle")
        reply, payload = a.handle("no thanks", "Africa/Lagos")
        self.assertIn("won't book", reply or "")
        self.assertIsNone(payload)
        self.assertEqual(a.stage, "idle")

    def test_disabled_or_unavailable_offer_does_not_enter_booking(self):
        s = booking.VoiceBookingSession()
        s.capture_offer({"enabled": False, "slots": []})
        self.assertEqual(s.stage, "idle")
        self.assertEqual(s.handle("yes", "Africa/Lagos"), (None, None))

    def test_uncertain_result_is_never_confirmed(self):
        self.assertNotIn("confirmed", booking.result_message({"status": "uncertain"}, SLOTS[0], "Africa/Lagos"))
        self.assertIn("could not be completed", booking.result_message({"status": "failed"}, SLOTS[0], "Africa/Lagos"))

    def test_can_change_previously_selected_time_before_confirmation(self):
        s = booking.VoiceBookingSession(); s.capture_offer(self.OFFER)
        s.handle("first one", "Africa/Lagos")
        s.handle("Ada, ada@example.com", "Africa/Lagos")
        first_key = s.idempotency
        reply, payload = s.handle("actually the second time", "Africa/Lagos")
        self.assertIn("Mon 19 Oct, 10:30", reply or "")
        self.assertIn("Should I book it?", reply or "")
        self.assertIsNone(payload)
        self.assertNotEqual(s.idempotency, first_key)
        _, payload = s.handle("yes", "Africa/Lagos")
        self.assertEqual(payload and payload["slotStart"], "2026-10-19T09:30:00Z")
        self.assertEqual(payload and payload["email"], "ada@example.com")

    def test_mixed_input_booking_correction_and_single_confirmation(self):
        s = booking.VoiceBookingSession(); s.capture_offer(self.OFFER)
        # Spoken/typed turns share one deterministic session.
        self.assertIn("name", (s.handle("second", "Africa/Lagos")[0] or ""))
        self.assertIn("Should I book it?", (s.handle("Ada, ada@example.com", "Africa/Lagos")[0] or ""))
        reply, payload = s.handle("actually use the first time", "Africa/Lagos")
        self.assertIn("Mon 19 Oct, 10:00", reply or "")
        self.assertIsNone(payload)
        _, payload = s.handle("yes, confirm", "Africa/Lagos")
        self.assertEqual(payload and payload["slotStart"], "2026-10-19T09:00:00Z")
        self.assertEqual(payload and payload["email"], "ada@example.com")
        # A repeated confirmation cannot create a second booking payload.
        self.assertEqual(s.handle("yes", "Africa/Lagos"), (None, None))


if __name__ == "__main__":
    unittest.main()
