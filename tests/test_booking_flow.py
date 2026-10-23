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
        s.capture_offer(self.OFFER)
        reply, payload = s.handle("anything this weekend?", "Africa/Lagos")
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

    def test_relative_day_filter_does_not_offer_the_wrong_date(self):
        from datetime import datetime, timezone

        raw = {
            "enabled": True,
            "timezone": "Africa/Lagos",
            "slots": [
                {"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 10:00", "durationMinutes": 30},
                {"start": "2026-10-23T09:00:00Z", "label": "Fri 23 Oct, 10:00", "durationMinutes": 30},
            ],
        }
        offer = booking.prepare_offer(
            raw, "Do you have anything tomorrow?",
            now=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([s["label"] for s in offer["slots"]], ["Thu 22 Oct, 10:00"])
        self.assertEqual(offer["requested_day"], "tomorrow")

        week = {
            "enabled": True,
            "timezone": "Africa/Lagos",
            "slots": [
                {"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 10:00"},
                {"start": "2026-10-24T09:00:00Z", "label": "Sat 24 Oct, 10:00"},
                {"start": "2026-10-26T09:00:00Z", "label": "Mon 26 Oct, 10:00"},
                {"start": "2026-11-01T09:00:00Z", "label": "Sun 1 Nov, 10:00"},
                {"start": "2026-11-02T09:00:00Z", "label": "Mon 2 Nov, 10:00"},
            ],
        }
        offer = booking.prepare_offer(
            week, "What about next week?",
            now=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [s["label"] for s in offer["slots"]],
            ["Mon 26 Oct, 10:00", "Sun 1 Nov, 10:00"],
        )
        offer = booking.prepare_offer(
            week, "Anything this weekend?",
            now=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([s["label"] for s in offer["slots"]], ["Sat 24 Oct, 10:00"])
        self.assertEqual(offer["requested_day"], "this weekend")


if __name__ == "__main__":
    unittest.main()


class BookingOnlyGuardrailTest(unittest.TestCase):
    def test_contact_channels_are_never_spoken(self):
        from app.booking import (
            availability_message,
            booking_guidance_message,
            result_message,
        )

        messages = [booking_guidance_message()]
        for status in ("confirmed", "uncertain", "failed", "slot_unavailable"):
            messages.append(result_message({"status": status}, SLOTS[0], "Africa/Lagos"))
        for reason in (
            "disabled", "calendar_unconfigured", "calendar_unavailable", "no_slots"
        ):
            messages.append(availability_message({"reason": reason}, "Africa/Lagos"))
        messages.append(availability_message(
            {"reason": "available", "timezone": "Africa/Lagos", "slots": SLOTS},
            "Africa/Lagos",
        ))
        for message in messages:
            lowered = message.lower()
            self.assertNotIn("contact", lowered)
            self.assertNotIn("e-mail", lowered)
            self.assertNotIn("reach james", lowered)
            self.assertNotIn("@", message)

    def test_indirect_scheduling_requests_reach_availability(self):
        for text in (
            "Can I book a call?",
            "I'd like to schedule something.",
            "Do you have anything tomorrow?",
            "What times are available?",
            "Friday at ten",
            "Any slots next week?",
            "What time works?",
            "Tomorrow at ten",
            "Friday please",
            "ten o'clock",
            "Next week please",
            "Anything this weekend?",
        ):
            self.assertTrue(booking.is_booking_request(text), text)
        for text in (
            "Hello.", "What projects has James built?", "No thanks.",
            "I had a good time yesterday.",
        ):
            self.assertFalse(booking.is_booking_request(text), text)

    def test_availability_parser_never_invents_slots(self):
        out = booking.parse_availability_response(503, {"enabled": True}, "Africa/Lagos")
        self.assertFalse(out["enabled"])
        self.assertEqual(out["reason"], "calendar_unavailable")
        self.assertEqual(out["slots"], [])
        out = booking.parse_availability_response(200, {
            "enabled": True,
            "configured": True,
            "timezone": "Africa/Lagos",
            "durationMinutes": 30,
            "slots": [{"start": "bad"}, {"label": "no start"}, {"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 09:00"}],
        }, "Africa/Lagos")
        self.assertTrue(out["enabled"])
        self.assertEqual(len(out["slots"]), 1)
        self.assertEqual(out["slots"][0]["label"], "Thu 22 Oct, 09:00")

        disabled = booking.parse_availability_response(200, {
            "enabled": False, "configured": True, "reason": "disabled",
            "slots": [{"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 09:00"}],
        }, "Africa/Lagos")
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["reason"], "disabled")
        unconfigured = booking.parse_availability_response(200, {
            "enabled": True, "configured": False,
            "slots": [{"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 09:00"}],
        }, "Africa/Lagos")
        self.assertFalse(unconfigured["enabled"])
        self.assertEqual(unconfigured["reason"], "calendar_unconfigured")

    def test_requested_weekday_filters_and_caps_presented_slots(self):
        raw = {
            "enabled": True,
            "timezone": "Africa/Lagos",
            "slots": [
                {"start": f"2026-10-{19+i:02d}T09:00:00Z", "label": f"Mon {19+i} Oct, 09:00", "durationMinutes": 30}
                for i in range(3)
            ] + [
                {"start": "2026-10-23T10:00:00Z", "label": "Fri 23 Oct, 10:00", "durationMinutes": 30},
                {"start": "2026-10-23T11:00:00Z", "label": "Fri 23 Oct, 11:00", "durationMinutes": 30},
            ],
        }
        offer = booking.prepare_offer(raw, "What about Friday?", limit=1)
        self.assertEqual(len(offer["slots"]), 1)
        self.assertIn("Fri", offer["slots"][0]["label"])
        self.assertEqual(offer["requested_day"], "Friday")
        message = booking.availability_message(offer, "Africa/Lagos")
        self.assertIn("Fri 23 Oct", message)
        self.assertNotIn("Mon", message)

    def test_fresh_availability_keeps_name_and_email(self):
        s = booking.VoiceBookingSession()
        s.capture_offer(VoiceBookingSessionTest.OFFER)
        s.handle("first", "Africa/Lagos")
        s.handle("Ada, ada@example.com", "Africa/Lagos")
        refreshed = dict(VoiceBookingSessionTest.OFFER)
        refreshed["slots"] = [
            {"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 09:00", "durationMinutes": 30},
        ]
        s.capture_offer(refreshed, keep_details=True)
        self.assertEqual(s.name, "Ada")
        self.assertEqual(s.email, "ada@example.com")
        reply, payload = s.handle("first", "Africa/Lagos")
        self.assertIn("Should I book it?", reply or "")
        self.assertIsNone(payload)
        self.assertEqual(s.slot and s.slot["label"], "Thu 22 Oct, 09:00")

    def test_asking_for_another_day_does_not_erase_partial_details(self):
        s = booking.VoiceBookingSession()
        s.capture_offer(VoiceBookingSessionTest.OFFER)
        reply, _ = s.handle("first", "Africa/Lagos")
        self.assertIn("name", reply or "")
        reply, _ = s.handle("My name is Ada", "Africa/Lagos")
        self.assertIn("email", reply or "")
        reply, payload = s.handle("What about Friday?", "Africa/Lagos")
        self.assertIsNone(reply)
        self.assertIsNone(payload)
        self.assertEqual(s.stage, "idle")
        self.assertEqual(s.name, "Ada")
        refreshed = dict(VoiceBookingSessionTest.OFFER)
        refreshed["slots"] = [
            {"start": "2026-10-23T09:00:00Z", "label": "Fri 23 Oct, 09:00", "durationMinutes": 30},
        ]
        s.capture_offer(booking.prepare_offer(refreshed, "Friday"), keep_details=True)
        reply, _ = s.handle("first", "Africa/Lagos")
        self.assertIn("email", reply or "")
        self.assertNotIn("name and", (reply or "").lower())
        self.assertEqual(s.name, "Ada")
