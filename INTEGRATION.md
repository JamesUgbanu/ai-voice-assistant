# Conversational backend integration

The voice service owns audio, speech recognition, turn state, and speech
synthesis. A **responder** turns a finalized utterance plus bounded conversation
history into answer text. Two adapters exist:

| Adapter | Selected by | Status |
|---|---|---|
| Portfolio RAG | `VOICE_RESPONDER=portfolio` (default) | Preserved reference deployment |
| Generic HTTP | `VOICE_RESPONDER=http` | Reusable, no portfolio dependency |

Both receive the current utterance and prior turns. Neither receives audio.
The service speaks only the text the backend returns; it never speaks retrieval
metadata, citations, or internal identifiers.

`VOICE_RESPONDER=portfolio` defaults to **booking-only** mode
(`VOICE_BOOKING_ONLY=1`): scheduling requests read the calendar directly,
greetings offer to book, and other messages get a fixed booking prompt. The
portfolio RAG stream is not used for contact/factual answers in this mode.
Set `VOICE_BOOKING_ONLY=0` to restore the RAG fallback.

## Generic HTTP responder

Set:

```bash
VOICE_RESPONDER=http
VOICE_RESPONDER_URL=https://your-app.example/respond
# optional:
VOICE_RESPONDER_TOKEN=...
VOICE_RESPONDER_TIMEOUT_SECONDS=25
```

The service sends:

```http
POST /respond
Content-Type: application/json
Authorization: Bearer <token>        # only when configured

{
  "question": "What is your refund policy?",
  "history": [
    {"question": "Do you ship internationally?", "answer": "Yes, to 30 countries."}
  ],
  "sessionId": "opaque-session-id"
}
```

It expects `text/event-stream`:

```text
event: token
data: {"text": "Refunds are available "}

event: token
data: {"text": "within 30 days."}

event: done
data: {}
```

Rules:

- Tokens are concatenated in order. Empty token text is ignored.
- `event: error` with `data: {"message": "..."}` ends the turn as
  `rag_failed`. The message must be safe to show a visitor.
- Non-2xx responses and transport failures are turn errors; the service never
  invents an answer.
- `history` contains at most the last six completed, uncancelled turns. The
  current utterance is never repeated inside `history`.
- `sessionId` is the voice session id; it is stable for the call and useful for
  per-call backend state. It is not a persistent cross-session identity.
- The service stops reading when the visitor interrupts or the call ends, and
  applies `VOICE_RESPONDER_TIMEOUT_SECONDS` to the request.
- Answers are bounded by the service's `max_answer_chars` (4000 in the generic
  adapter).
- Booking is optional. The generic adapter does not support it; implement the
  portfolio contract below only if your application needs scheduling.

A runnable implementation is in `examples/echo_backend.py`.

## Portfolio RAG responder (default, preserved)

Source of truth: the portfolio repository (`/api/rag`, `lib/rag/pipeline.ts`,
`lib/ratelimit.ts`). This is the **existing integration** and must remain
compatible.

### Endpoint

`POST {VOICE_RAG_API_URL}` (the reference deployment uses its own `/api/rag`).

Headers: `Content-Type: application/json`,
`Authorization: Bearer <VOICE_RAG_SERVICE_TOKEN>`.

### Request

```json
{
  "question": "What has James built with AWS?",
  "history": [
    {"question": "What projects has James built?", "answer": "Several, including ..."}
  ],
  "debug": false,
  "mode": "voice",
  "timeZone": "Africa/Lagos"
}
```

- `question`: 2–500 characters (the voice client requires at least 3).
- `history`: at most six completed turns; each question ≤500 and answer ≤2000
  characters.
- `mode: "voice"` asks for a concise spoken answer and isolates the cache.
- `timeZone` labels booking slots and is passed through to the portfolio.
- The service does not send `bookingState`; the portfolio reconstructs context
  from history for voice sessions.

The portfolio's authenticated service budget is used; visitor IP/session limits
do not apply to this server-to-server call. A wrong bearer token fails closed
with 401.

### Response

`text/event-stream` frames:

```
event: meta
data: {"sessionId":"...","route":"knowledge","cached":false}

event: token
data: {"text":"James has built "}

event: token
data: {"text":"several backends on AWS."}

event: sources
data: {"sources":[...]}

event: booking
data: {"enabled":true,"timezone":"Africa/Lagos","durationMinutes":30,"slots":[...]}

event: done
data: {"metrics":{...}}

event: error
data: {"message":"..."}
```

- `token`: answer text, appended in order. `sources` are ignored and never
  spoken.
- `booking`: optional genuine availability. The portfolio adapter can still
  capture this event in non-booking-only mode, but booking-only mode reads
  availability directly (below) so an indirect request does not depend on the
  event arriving.
- `error`: ends the turn as `rag_failed`; no fabricated answer.
- Non-2xx statuses map to turn errors. `429` includes `Retry-After`.

### Booking (portfolio adapter only)

The portfolio owns availability, validation, idempotency, and calendar event
creation. Availability is read directly through the existing endpoint:

```http
GET /api/booking/availability?timeZone=Africa%2FLagos&durationMinutes=30
```

The response must match `BookingAvailability` (`enabled`, `configured`,
`timezone`, `durationMinutes`, `reason`, `slots[]`). The voice service:

1. Reads availability directly for explicit and indirect requests (“anything
   Friday?”, “do you have a slot tomorrow?”); it does not wait for the optional
   RAG `booking` event.
2. Filters the presented slots to the requested weekday and caps the spoken
   list, so “first one” always maps to a time the caller heard.
3. Selects a slot, collects name/email, and reads back the details. Name/email
   survive a fresh-availability request and are never used as a contact path.
4. Calls `POST /api/booking` with `confirmed: true` and an idempotency key only
   after an explicit “yes”.
5. Speaks success **only** for HTTP 200/201 with `status: "confirmed"`.
6. Treats timeout/202/uncertain as not booked, retains the idempotency key, and
   allows an explicit retry to reconcile instead of double-booking.
7. Speaks no contact channel or fallback address, including on failure.

The URL defaults from `VOICE_RAG_API_URL` when it ends in `/api/rag`, or can be
set explicitly with `VOICE_BOOKING_AVAILABILITY_URL`. Booking-only startup fails
closed if no availability URL can be derived.

## Audio and playback telemetry

The server logs per-turn `tts_queue_max`, `tts_send_ms`,
`tts_frame_gap_max_ms`, `tts_underrun_risk_frames`, and
`tts_producer_wait_ms`, plus process `event_loop_lag_max_ms`, `rss_mb`,
`cpu_throttled_total`, and `cpu_throttle_usec_total` in `/metrics`. Clients may
send optional `playback_metrics` frames (capability-advertised) with numeric
fields only: `sample_rate`, `buffer_ms`, `queue_max_ms`, `render_lag_max_ms`,
`underruns`, `dropped_frames`, `decode_errors`, `reconnect_count`. This is the
only supported way to report browser-side underruns without sending audio or
free text.

## Security expectations

- Use HTTPS for any remote responder and a bearer token when available.
- Do not log questions, answers, audio, transcripts, or tokens in the backend.
- Keep `sessionId` scoped to the call; do not use it as a long-lived user id.
- Never return internal retrieval metadata, citations, or chain-of-thought as
  `token` text; the service will speak it verbatim.
