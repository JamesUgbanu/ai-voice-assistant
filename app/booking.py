"""Deterministic voice booking helpers.

The portfolio owns availability, validation, idempotency, and Calendar event
creation. These helpers only parse a spoken conversation and render the
server's result; they never invent a slot or confirm a booking.

Booking-only mode: unrelated questions receive a fixed scheduling prompt. The
caller's email is used only for the calendar invitation and is never replaced
with, or redirected to, another contact channel.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)?\b", re.IGNORECASE)
CLOCK_WORD_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*(?:o'?clock|a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)
CLOCK_TIME_RE = re.compile(
    r"\b(?:at|around|by)\s+(?:one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|\d{1,2})(?::\d{2})?\s*(?:o'?clock|a\.?m\.?|p\.?m\.?)?\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"\d{1,2})\s+o'?clock\b"
    r"|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
CONFIRM_RE = re.compile(r"^(yes|yeah|yep|confirm|confirmed|go ahead|book it|please do|that works|sounds good|correct|do it)\b", re.IGNORECASE)
CANCEL_RE = re.compile(r"\b(cancel|stop|don't book|do not book|no thanks|not now|forget it)\b", re.IGNORECASE)
BOOK_RE = re.compile(r"\b(book|schedule|appointment|meeting|consultation|call|reserve|availability|available)\b", re.IGNORECASE)
FRESH_RE = re.compile(
    r"\b(available|availability|slots?|another|different|today|tomorrow|"
    r"next week|this week|weekend|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b",
    re.IGNORECASE,
)
RELATIVE_DAY_RE = re.compile(
    r"\b(today|tomorrow|next week|this week|this weekend|weekend)\b",
    re.IGNORECASE,
)
TIME_QUERY_RE = re.compile(
    r"\b(?:what|which|any|another)\s+times?\b|\btimes?\s+(?:work|works|are available)\b",
    re.IGNORECASE,
)
BOOKING_QUESTION_RE = re.compile(
    r"\b(what|which|when|any|do|does|can|could|would|is|are|have|how about|what about)\b",
    re.IGNORECASE,
)
NAME_INTRO_RE = re.compile(r"(?i:my name is|this is|i am|i'm|call me)")

# Explicit restart/abandon requests must release the booking flow.
RESTART_RE = re.compile(
    r"\b(start over|start again|restart|new conversation|reset|forget (it|the booking)|never ?mind)\b",
    re.IGNORECASE,
)
# A full portfolio question during booking is a subject change, not a reply.
# Mirrors the portfolio's own topic-change rule so the voice service releases
# the flow and lets RAG answer instead of asking "which time?".
TOPIC_FACTUAL_RE = re.compile(
    r"\b(james|you|your|experience|project|projects|built|work|worked|technolog|skill|aws|react|terraform|rag|agent|contact|hire|hiring|role|resume|cv)\b",
    re.IGNORECASE,
)
TOPIC_QUESTION_RE = re.compile(
    r"\b(what|which|how|why|where|when|who|tell|explain|describe|show)\b", re.IGNORECASE
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
DAY_ABBR_RE = re.compile(r"\b(" + "|".join(DAY_ABBR) + r")\b", re.IGNORECASE)


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
    # Spoken clock hours ("Friday at ten", "ten a.m."). "one/two/three" stay
    # ordinal-first unless an explicit clock marker makes the intent clear.
    for word, hour in WORD_HOURS.items():
        explicit = re.search(
            rf"\b(?:at\s+{word}|{word}\s*(?:o'?clock|a\.?m\.?|p\.?m\.?))\b",
            q,
            re.IGNORECASE,
        )
        if not explicit and word in ORDINALS:
            continue
        if re.search(rf"\b{word}\b", q):
            for slot in slots:
                label = str(slot.get("label", ""))
                if f"{hour:02d}:" in label or f"{hour}:" in label:
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
    if (
        not NAME_INTRO_RE.search(q)
        and not re.search(r",\s*[A-Z][A-Za-z'-]+", q)
        and any(re.search(rf"\b{word}\b", q.lower()) for word in ORDINALS)
    ):
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
    q = text or ""
    return bool(FRESH_RE.search(q) or TIME_QUERY_RE.search(q))


def looks_like_booking(text: str) -> bool:
    return bool(BOOK_RE.search(text or ""))


def is_booking_request(text: str) -> bool:
    """True for an explicit or indirect request to schedule a call.

    This is intentionally broader than ``looks_like_booking``: "Do you have
    anything tomorrow?" and "Friday at ten" must reach the calendar instead of
    falling through to a generic answer."""
    q = (text or "").strip()
    if not q:
        return False
    if BOOK_RE.search(q) or CLOCK_TIME_RE.search(q) or RELATIVE_DAY_RE.search(q):
        return True
    if (FRESH_RE.search(q) or TIME_QUERY_RE.search(q)) and (
        BOOKING_QUESTION_RE.search(q) or TIME_RE.search(q) or CLOCK_WORD_RE.search(q)
    ):
        return True
    return bool(
        DAY_ABBR_RE.search(q)
        and (
            TIME_RE.search(q)
            or CLOCK_WORD_RE.search(q)
            or len(q.split()) <= 2
        )
    )


def new_idempotency_key() -> str:
    return f"voice-{uuid.uuid4()}"


def booking_guidance_message() -> str:
    """Fixed booking-only reply. Deliberately contains no contact channel."""
    return (
        "I can help you book a call with James. Tell me a day or time that works "
        "for you, and I'll check the calendar."
    )


def parse_availability_response(
    status_code: int, body: object, fallback_timezone: str
) -> dict[str, Any]:
    """Normalize the portfolio availability response; never invent slots."""
    base: dict[str, Any] = {
        "enabled": False,
        "configured": False,
        "reason": "calendar_unavailable",
        "timezone": fallback_timezone,
        "durationMinutes": 30,
        "slots": [],
    }
    if status_code != 200 or not isinstance(body, dict):
        return base

    raw_slots = body.get("slots")
    slots: list[dict[str, Any]] = []
    if isinstance(raw_slots, list):
        for item in raw_slots:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            label = item.get("label")
            if not isinstance(start, str) or not start.strip():
                continue
            if not isinstance(label, str) or not label.strip():
                continue
            try:
                duration = int(item.get("durationMinutes") or body.get("durationMinutes") or 30)
            except (TypeError, ValueError):
                duration = 30
            slots.append({
                "start": start.strip(),
                "end": str(item.get("end") or ""),
                "label": label.strip()[:120],
                "durationMinutes": max(5, min(240, duration)),
                "timeZone": str(item.get("timeZone") or body.get("timezone") or fallback_timezone),
            })

    configured = bool(body.get("configured", True))
    requested_enabled = bool(body.get("enabled"))
    if not configured:
        enabled = False
        reason = "calendar_unconfigured"
    elif not requested_enabled:
        enabled = False
        reason = str(body.get("reason") or "disabled")
    elif not slots:
        enabled = False
        reason = "no_slots"
    else:
        enabled = True
        reason = "available"
    timezone = str(body.get("timezone") or fallback_timezone)
    try:
        duration = int(body.get("durationMinutes") or 30)
    except (TypeError, ValueError):
        duration = 30
    return {
        "enabled": enabled,
        "configured": configured,
        "reason": reason,
        "timezone": timezone,
        "durationMinutes": max(5, min(240, duration)),
        "slots": slots,
    }


def _slot_local_date(slot: dict[str, Any], zone: ZoneInfo) -> date | None:
    raw = str(slot.get("start") or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(zone).date()


def prepare_offer(
    data: dict[str, Any],
    text: str,
    limit: int = 4,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Filter availability to the requested weekday and cap what is spoken.

    A caller saying "Friday" must never hear Monday's slots merely because
    those were first in the calendar response. The returned offer contains
    exactly the slots that were presented, so "first" always maps to the first
    time the caller actually heard."""
    offer = dict(data or {})
    slots = [s for s in (offer.get("slots") or []) if isinstance(s, dict)]
    q = (text or "").lower()
    relative = ""
    for phrase in ("today", "tomorrow", "next week", "this week", "this weekend", "weekend"):
        if re.search(rf"\b{re.escape(phrase)}\b", q):
            relative = phrase
            break
    requested = [name for name in DAY_ABBR if re.search(rf"\b{name}\b", q)]
    if relative:
        try:
            zone = ZoneInfo(str(offer.get("timezone") or "UTC"))
        except (ValueError, ZoneInfoNotFoundError):
            zone = UTC
        current = now or datetime.now(zone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        today = current.astimezone(zone).date()
        if relative == "today":
            dates = {today}
        elif relative == "tomorrow":
            dates = {today + timedelta(days=1)}
        elif relative == "next week":
            start = today + timedelta(days=7 - today.weekday())
            dates = {start + timedelta(days=i) for i in range(7)}
        elif relative == "this week":
            dates = {today + timedelta(days=i) for i in range(7 - today.weekday())}
        else:
            if today.weekday() == 6:  # Sunday: the weekend ends today.
                dates = {today}
            else:
                saturday = today + timedelta(days=(5 - today.weekday()) % 7)
                dates = {saturday, saturday + timedelta(days=1)}
        slots = [s for s in slots if _slot_local_date(s, zone) in dates]
        offer["requested_day"] = "this weekend" if relative == "weekend" else relative
    elif requested:
        wanted = {DAY_ABBR[name] for name in requested}
        slots = [
            s for s in slots
            if any(abbr in str(s.get("label", "")).lower() for abbr in wanted)
        ]
        offer["requested_day"] = requested[0].capitalize()

    slots.sort(key=lambda s: str(s.get("start", "")))
    offer["slots"] = slots[: max(0, limit)]
    offer["total_slots"] = len(slots)
    if not offer["slots"]:
        offer["enabled"] = False
        offer["reason"] = "no_slots"
    return offer


def availability_message(offer: dict[str, Any], fallback_timezone: str) -> str:
    """Speak verified availability, or a fixed failure; never a contact path."""
    reason = str(offer.get("reason") or "")
    requested = str(offer.get("requested_day") or "")
    if reason == "disabled":
        return "Booking is turned off right now, so I can't confirm an appointment."
    if reason == "calendar_unconfigured":
        return "The calendar isn't connected right now, so I can't confirm a booking."
    if reason == "calendar_unavailable":
        return (
            "I can't reach the calendar right now, so I can't confirm a booking. "
            "Please try again shortly."
        )
    if reason == "no_slots" and requested:
        when = (
            f" {requested}"
            if requested.lower() in {
                "today", "tomorrow", "next week", "this week", "this weekend",
            }
            else f" on {requested}"
        )
        return (
            f"I don't see any open times{when}. Would you like me to "
            "check a different day?"
        )
    if reason == "no_slots":
        return (
            "I don't see any open times in the next two weeks. Tell me another "
            "day and I'll check again."
        )

    slots = [s for s in (offer.get("slots") or []) if isinstance(s, dict)]
    if not slots:
        return "I couldn't find an open time. I haven't booked anything."
    labels = [str(s.get("label") or s.get("start") or "").strip() for s in slots]
    labels = [label for label in labels if label]
    timezone = str(offer.get("timezone") or fallback_timezone)
    if not labels:
        return "I couldn't read those times. I haven't booked anything."
    if len(labels) == 1:
        return f"I have this time in {timezone}: {labels[0]}. Does that work for you?"
    spoken = "; ".join(labels)
    return f"I have these times in {timezone}: {spoken}. Which one works for you?"


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
            "it as booked. Say 'yes' and I'll check again with the same request."
        )
    if status == "slot_unavailable":
        return "That time is no longer available. I won't book it; ask me for updated times."
    return (
        "That booking could not be completed. I haven't confirmed anything. "
        "Ask me for current times if you'd like to try again."
    )


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
        self.uncertain = False

    def clear(self) -> None:
        self.offer = None
        self.slot = None
        self.stage = "idle"
        self.name = ""
        self.email = ""
        self.idempotency = ""
        self.uncertain = False

    def capture_offer(self, data: dict[str, Any], keep_details: bool = False) -> None:
        slots = data.get("slots")
        if data.get("enabled") and isinstance(slots, list) and slots:
            name, email = self.name, self.email
            self.offer = data
            self.slot = None
            self.stage = "select"
            self.name = name if keep_details else ""
            self.email = email if keep_details else ""
            self.idempotency = ""
        else:
            self.clear()

    def release_for_refresh(self) -> None:
        """Drop a stale offer but retain identity for the next availability.

        "What about Friday instead?" must not force the caller to repeat their
        name and email. Explicit cancels/restarts use ``clear`` instead."""
        name, email = self.name, self.email
        self.clear()
        self.name, self.email = name, email

    def mark_uncertain(self) -> None:
        """A submission could not be verified. Keep the chosen slot, details,
        and idempotency key so an explicit retry reconciles with the
        portfolio's existing idempotency record instead of booking twice."""
        if self.stage == "submitted":
            self.stage = "confirm"
            self.uncertain = True

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

        A None reply means the caller should refresh availability or use the
        fixed booking-only guidance (for example, "show me other times" clears
        the pending offer so the caller can fetch a new one).
        """
        q = (text or "").strip()
        if not q or self.stage == "idle":
            return None, None
        # A subject change or explicit restart abandons the booking flow; the
        # caller then speaks booking guidance (never portfolio content). A
        # clear confirmation never trips this.
        if self.stage != "idle" and not self.uncertain and (
            wants_restart(q)
            or (looks_like_topic_change(q) and not wants_confirm(q) and not parse_email(q))
        ):
            self.clear()
            return None, None
        if (
            self.stage != "idle"
            and not self.uncertain
            and not wants_confirm(q)
            and mentions_unoffered_weekday(q, (self.offer or {}).get("slots") or [])
        ):
            self.release_for_refresh()
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
                self.release_for_refresh()
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
                self.release_for_refresh()
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
            if self.uncertain:
                # The portfolio has no cancel endpoint. Until the same
                # idempotency key is reconciled, starting a different booking
                # could create a second event, so require verification first.
                if wants_confirm(q):
                    payload = self.payload(timezone)
                    if not payload:
                        self.clear()
                        return "That offer is no longer available. Ask me for current times.", None
                    self.stage = "submitted"
                    return None, payload
                message = (
                    "I still need to verify the earlier request before changing or "
                    "starting another booking. Say yes and I'll check the same request."
                )
                return message, None
            if wants_cancel(q):
                self.clear()
                return "Okay, I won't book anything.", None
            corrected = self._reselect_slot(q, timezone)
            if corrected is not None:
                return corrected, None
            if wants_fresh_availability(q) and not wants_confirm(q) and not parse_email(q):
                self.release_for_refresh()
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
