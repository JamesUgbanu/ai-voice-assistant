"""Deterministic voice booking helpers.

The portfolio owns availability, validation, idempotency, and Calendar event
creation. These helpers only parse a spoken conversation and render the
server's result; they never invent a slot or confirm a booking.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)?\b", re.I)
CONFIRM_RE = re.compile(r"^(yes|yeah|yep|confirm|confirmed|go ahead|book it|please do|that works|sounds good|correct|do it)\b", re.I)
CANCEL_RE = re.compile(r"\b(cancel|stop|don't book|do not book|no thanks|not now|forget it)\b", re.I)
BOOK_RE = re.compile(r"\b(book|schedule|appointment|meeting|call)\b", re.I)
FRESH_RE = re.compile(r"\b(available|availability|times?|slots?|another|different|next week|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
NAME_INTRO_RE = re.compile(r"(?i:my name is|this is|i am|i'm|call me)")

# Explicit restart/abandon requests must release the booking flow.
RESTART_RE = re.compile(
    r"\b(start over|start again|restart|new conversation|reset|forget (it|the booking)|never ?mind)\b",
    re.I,
)
# A full portfolio question during booking is a subject change, not a reply.
# Mirrors the portfolio's own topic-change rule so the voice service releases
# the flow and lets RAG answer instead of asking "which time?".
TOPIC_FACTUAL_RE = re.compile(
    r"\b(james|you|your|experience|project|projects|built|work|worked|technolog|skill|aws|react|terraform|rag|agent|contact|hire|hiring|role|resume|cv)\b",
    re.I,
)
TOPIC_QUESTION_RE = re.compile(
    r"\b(what|which|how|why|where|when|who|tell|explain|describe|show)\b", re.I
)


def wants_restart(text: str) -> bool:
    return bool(RESTART_RE.search(text or ""))


def looks_like_topic_change(text: str) -> bool:
    q = (text or "").strip()
    if len(q) <= 24:
        return False
    return bool(TOPIC_FACTUAL_RE.search(q) and TOPIC_QUESTION_RE.search(q))


DAY_ABBR = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}


def mentions_unoffered_weekday(text: str, slots: list[dict]) -> bool:
    """True when the visitor names a weekday that none of the offered slots is
    on. Prevents "Friday" from selecting a Thursday slot just because the clock
    time matches; the caller then asks the portfolio for fresh availability."""
    q = (text or "").lower()
    labels = " ".join(str(s.get("label", "")).lower() for s in slots)
    for name, abbr in DAY_ABBR.items():
        if re.search(rf"\b{name}\b", q) and abbr not in labels:
            return True
    return False


def select_slot(text: str, slots: list[dict]) -> dict | None:
    if not slots:
        return None
    q = (text or "").lower().strip()
    # An explicit clock time is unambiguous and must win over word ordinals:
    # "the 10:00 one" means the 10:00 slot, not the first slot ("one").
    times = []
    for match in TIME_RE.finditer(q):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if match.group(3):
            ampm = match.group(3).lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        times.append(f"{hour:02d}:{minute:02d}")
    for slot in slots:
        label = str(slot.get("label", ""))
        if any(t in label for t in times):
            return slot
    for word in sorted(ORDINALS, key=len, reverse=True):
        index = ORDINALS[word]
        if re.search(rf"\b{re.escape(word)}\b", q) and index < len(slots):
            return slots[index]
    return None


def parse_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else ""


def has_explicit_name(text: str) -> bool:
    return bool(NAME_INTRO_RE.search(text or ""))


def parse_name(text: str) -> str:
    q = (text or "").strip()
    # Never treat "first one", "second option", etc. as a person's name.
    if not NAME_INTRO_RE.search(q) and not re.search(r",\s*[A-Z][A-Za-z'-]+", q):
        if any(re.search(rf"\b{word}\b", q.lower()) for word in ORDINALS):
            return ""
    for pattern in (
        r"(?i:my name is|this is|i am|i'm|call me)\s+([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,2})",
        r"^([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,2})(?:\s*,|\s+and\b|\s*$)",
    ):
        match = re.search(pattern, q)
        if match:
            candidate = match.group(1).strip()
            if candidate.lower() not in {"i", "im", "id", "yes", "no"}:
                return candidate
    return ""


def wants_confirm(text: str) -> bool:
    return bool(CONFIRM_RE.search((text or "").strip()))


def wants_cancel(text: str) -> bool:
    return bool(CANCEL_RE.search(text or ""))


def wants_fresh_availability(text: str) -> bool:
    return bool(FRESH_RE.search(text or ""))


def looks_like_booking(text: str) -> bool:
    return bool(BOOK_RE.search(text or ""))


def new_idempotency_key() -> str:
    return f"voice-{uuid.uuid4()}"


def readback(slot: dict, timezone: str) -> str:
    label = str(slot.get("label") or slot.get("start") or "")
    duration = slot.get("durationMinutes")
    suffix = f", {duration} minutes" if duration else ""
    return f"{label} ({timezone}{suffix})"


def confirmation_prompt(slot: dict, name: str, email: str, timezone: str) -> str:
    return (
        f"Just to confirm: {readback(slot, timezone)}, for {name} at {email}. "
        "Should I book it?"
    )


def result_message(result: dict, slot: dict, timezone: str) -> str:
    status = str(result.get("status", "failed"))
    if status == "confirmed":
        return f"Your call is confirmed for {readback(slot, timezone)}. A calendar invitation has been sent."
    if status == "uncertain":
        return (
            "I couldn't verify whether that booking completed, so I'm not treating "
            "it as booked. Say 'yes' and I'll check again with the same request, or "
            "contact James before trying anything else."
        )
    if status == "slot_unavailable":
        return "That time is no longer available. I won't book it; ask me for updated times."
    return "That booking could not be completed. I haven't confirmed anything; please try again or use the contact section."


def parse_booking_response(status_code: int, body: object) -> dict:
    """Map the portfolio `/api/booking` response onto the voice contract.

    Success is claimed ONLY for the portfolio's confirmed contract: HTTP 200/201
    **and** `status == "confirmed"`. A 2xx with any other body, an uncertain
    (202), a taken slot (409), and every other status are never treated as
    confirmed. Pure and testable; no I/O."""
    data = body if isinstance(body, dict) else {}
    status = data.get("status")
    if status_code in (200, 201) and status == "confirmed":
        return data
    if status_code == 202 or status == "uncertain":
        return {"status": "uncertain", "message": "The booking could not be confirmed."}
    if status_code == 409 or status == "slot_unavailable":
        return {"status": "slot_unavailable", "message": "That slot is no longer available."}
    return {"status": "failed", "message": "The booking was not confirmed."}


class VoiceBookingSession:
    """Per-WebSocket booking state. No cross-session or cross-visitor state."""

    def __init__(self) -> None:
        self.offer: dict[str, Any] | None = None
        self.slot: dict[str, Any] | None = None
        self.stage = "idle"
        self.name = ""
        self.email = ""
        self.idempotency = ""

    def clear(self) -> None:
        self.offer = None
        self.slot = None
        self.stage = "idle"
        self.name = ""
        self.email = ""
        self.idempotency = ""

    def capture_offer(self, data: dict[str, Any]) -> None:
        slots = data.get("slots")
        if data.get("enabled") and isinstance(slots, list) and slots:
            self.offer = data
            self.slot = None
            self.stage = "select"
            self.name = ""
            self.email = ""
            self.idempotency = ""
        else:
            self.clear()

    def mark_uncertain(self) -> None:
        """A submission could not be verified. Keep the chosen slot, details,
        and idempotency key so an explicit retry reconciles with the
        portfolio's existing idempotency record instead of booking twice."""
        if self.stage == "submitted":
            self.stage = "confirm"

    def payload(self, timezone: str) -> dict[str, Any] | None:
        if not self.slot or not self.offer:
            return None
        return {
            "slotStart": self.slot.get("start"),
            "durationMinutes": int(self.slot.get("durationMinutes") or self.offer.get("durationMinutes") or 30),
            "timeZone": str(self.offer.get("timezone") or timezone),
            "name": self.name,
            "email": self.email,
            "agenda": "",
            "confirmed": True,
            "idempotencyKey": self.idempotency,
        }


    def _reselect_slot(self, text: str, timezone: str) -> str | None:
        """Allow a correction to the chosen time in details/confirm stages."""
        if not self.offer:
            return None
        slot = select_slot(text, self.offer.get("slots") or [])
        if not slot:
            return None
        if self.slot and slot.get("start") == self.slot.get("start"):
            return None
        self.slot = slot
        self.idempotency = new_idempotency_key()
        self.email = parse_email(text) or self.email
        self.name = parse_name(text) or self.name
        if self.name and self.email:
            self.stage = "confirm"
            return confirmation_prompt(slot, self.name, self.email, timezone)
        self.stage = "details"
        missing = "name and email" if not self.name and not self.email else ("name" if not self.name else "email")
        return f"Okay, {readback(slot, timezone)}. What {missing} should I use?"

    def handle(self, text: str, timezone: str) -> tuple[str | None, dict[str, Any] | None]:
        """Return (spoken reply, booking payload). Payload requires explicit yes.

        A None reply means the caller should fall through to ordinary RAG
        (for example, "show me other times" while a slot offer is pending).
        """
        q = (text or "").strip()
        if not q or self.stage == "idle":
            return None, None
        # A subject change or explicit restart abandons the booking flow so the
        # normal RAG answer is used. A clear confirmation never trips this.
        if self.stage != "idle" and (
            wants_restart(q)
            or (looks_like_topic_change(q) and not wants_confirm(q) and not parse_email(q))
            or (not wants_confirm(q) and mentions_unoffered_weekday(q, (self.offer or {}).get("slots") or []))
        ):
            self.clear()
            return None, None
        if self.stage == "select":
            if wants_cancel(q):
                self.clear()
                return "Okay, I won't book anything.", None
            slot = select_slot(q, (self.offer or {}).get("slots") or [])
            if slot:
                self.slot = slot
                self.idempotency = new_idempotency_key()
                self.stage = "details"
                self.name = parse_name(q) or self.name
                self.email = parse_email(q) or self.email
                if self.name and self.email:
                    self.stage = "confirm"
                    return confirmation_prompt(self.slot, self.name, self.email, timezone), None
                missing = "name and email" if not self.name and not self.email else ("name" if not self.name else "email")
                return f"Got it. What {missing} should I use for the booking?", None
            if looks_like_booking(q) or wants_fresh_availability(q):
                self.clear()
                return None, None
            return "Which of those times should I hold? You can say first, second, third, or the time.", None
        if self.stage == "details":
            if wants_cancel(q):
                self.clear()
                return "Okay, I won't book anything.", None
            corrected = self._reselect_slot(q, timezone)
            if corrected is not None:
                return corrected, None
            if wants_fresh_availability(q) and not parse_email(q):
                self.clear()
                return None, None
            self.email = parse_email(q) or self.email
            self.name = parse_name(q) or self.name
            if self.name and self.email:
                self.stage = "confirm"
                return confirmation_prompt(self.slot or {}, self.name, self.email, timezone), None
            missing = []
            if not self.name:
                missing.append("name")
            if not self.email:
                missing.append("email")
            return f"I still need your {' and '.join(missing)} to confirm the booking.", None
        if self.stage == "submitted":
            # Exactly one confirmation may be submitted. The caller clears the
            # session after reporting the server result.
            return None, None
        if self.stage == "confirm":
            if wants_cancel(q):
                self.clear()
                return "Okay, I won't book anything.", None
            corrected = self._reselect_slot(q, timezone)
            if corrected is not None:
                return corrected, None
            if wants_fresh_availability(q) and not wants_confirm(q) and not parse_email(q):
                self.clear()
                return None, None
            if wants_confirm(q):
                payload = self.payload(timezone)
                if not payload:
                    self.clear()
                    return "That offer is no longer available. Ask me for current times.", None
                self.stage = "submitted"
                return None, payload
            new_email = parse_email(q)
            new_name = parse_name(q)
            if new_email or new_name:
                self.email = new_email or self.email
                self.name = new_name or self.name
                if self.name and self.email:
                    return confirmation_prompt(self.slot or {}, self.name, self.email, timezone), None
            return "Please say yes to confirm, or no to cancel.", None
        return None, None
