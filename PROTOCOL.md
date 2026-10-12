# Voice protocol v1

Base URL: `https://voice.jamesugbanu.com` (Caddy → voice service).
REST is JSON over HTTPS. Realtime audio is one WebSocket per call.

## Session handshake (REST)

`POST /v1/sessions` with optional JSON `{"visitor_id": "<opaque>"}`.
- New browsers omit it and receive a fresh id plus `Set-Cookie: voice_vid`
  (HttpOnly, Secure, `SameSite=None` over HTTPS so the cross-site
  WebSocket upgrade carries it; `Lax` over plain HTTP for local dev).
- Server enforces, in order: attempt budget → stale recovery → one active
  session → daily allowance. Denials are 429/409 with plain messages.
- Success `200`: `{session_id, ticket, ticket_expires_at, ws_url,
  max_seconds, warning_at_seconds, sample_rate}`. The ticket is an
  HMAC over `(session_id, expiry)`, valid 120 s, single purpose.

`GET /healthz` → `{status, db}` (no auth, no secrets).
`GET /metrics` → operational counters only.

## WebSocket `/v1/voice`

Connect, then send `hello` first (within 10 s) or be closed:

```json
{"v": 1, "type": "hello", "seq": 0, "turn_id": null,
 "payload": {"session_id": "<uuid>", "ticket": "<hmac>"}}
```

Server replies `session_created` then `listening`. Every message is a
JSON envelope `{v, type, seq, turn_id, payload}`; `seq` is a per-connection
monotonic counter, `turn_id` correlates one utterance end to end.

### Client → server

| type | payload | notes |
|---|---|---|
| `audio_frame` | `{audio_b64, final}` — base64 PCM16 mono 16 kHz | every 20–100 ms while speaking; `final` marks utterance end |
| `end_utterance` | `{}` | explicit turn boundary (VAD-confirmed server-side) |
| `interrupt` | `{}` | barge-in: cancels the turn, drops queued audio |
| `ping` | `{}` | keep-alive; server replies `pong` after re-validating session |
| `end_session` | `{reason}` | orderly hangup |

### Server → client

| type | payload | notes |
|---|---|---|
| `session_created` | `{session_id, max_seconds}` | ready |
| `listening` | `{}` | capturing |
| `transcript_partial` | `{text}` | only if the STT backend supports it |
| `transcript_final` | `{text, turn_id}` | what was sent to RAG |
| `rag_started` | `{turn_id}` | retrieval/generation running |
| `audio_frame` | `{audio_b64, sample_rate, final, turn_id}` | PCM16 mono chunks in order; `final` ends the turn stream |
| `turn_end` | `{reason, turn_id}` | `complete` \| `empty` \| `interrupted` \| `stt_failed` \| `rag_failed` \| `tts_failed` \| `error` |
| `quota` | `{remaining_seconds}` | usage snapshot on request/session end |
| `error` | `{code, message}` | recoverable (`bad_message`, `*_failed`, …) vs fatal (closes socket) |
| `pong` | `{}` | heartbeat reply |
| `session_ended` | `{reason}` | `user_ended` \| `max_duration` \| `disconnect` |

### Audio framing

- Input: PCM16 mono 16000 Hz, base64 per frame, ≤192 kB/frame default,
  ≤60 s audio per turn. 16-bit alignment enforced; odd byte counts rejected.
- Output: PCM16 mono at the TTS native rate (24 kHz for Pocket TTS),
  chunked server-side (~200 ms), played through a queued AudioContext
  (never one `decodeAudioData()` per fragment — that API needs complete
  media, not stream fragments).
- Ordering: frames carry connection `seq`; playback consumes FIFO per
  turn. Stale `turn_id` payloads are discarded by both ends.

### Cancellation

`interrupt` (or a new `audio_frame` burst while speaking, per client UX)
cancels the turn token: STT/RAG/TTS work aborts at the next checkpoint,
queued audio is dropped, late results carry a dead `turn_id` and are
ignored. Usage counts session active time regardless of outcome.

### Limits enforced here (server-side, never client-side)

5-minute sessions (warning at 4:00) · ~10 min/day/visitor · one active
session · 120 s ticket life · 10 s hello deadline · 32 KiB WS message cap ·
bounded frames/turns · RAG timeout 25 s · heartbeat re-validation.
