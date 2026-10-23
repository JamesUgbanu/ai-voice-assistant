# Voice protocol v1

Public base URL: your deployment origin (the reference deployment uses
`https://voice.jamesugbanu.com`). In production a TLS terminator such as
Cloudflare Tunnel or Caddy forwards to the service; local/dev may use Caddy or
the Compose stack. REST is JSON over HTTPS; realtime audio is one WebSocket per
call.

## Session handshake (REST)

`POST /v1/sessions` with optional JSON `{"visitor_id": "<opaque>"}`.

- New browsers omit it and receive a fresh id plus `Set-Cookie: voice_vid`
  (HttpOnly, Secure, `SameSite=None` over HTTPS so the cross-site WebSocket
  upgrade carries it; `Lax` over plain HTTP for local dev).
- Server enforces, in order: attempt budget → stale recovery → one active
  session → daily allowance. Denials are 429/409 with plain messages.
- Success `200`: `{session_id, ticket, ticket_expires_at, ws_url,
  max_seconds, warning_at_seconds, sample_rate}`. The ticket is an HMAC over
  `(session_id, expiry)`, valid 120 s, single purpose.

`GET /healthz` → `{status, db, warm, tts: {mode, ref_configured,
cloning_active, ...}}` (no auth, no secrets). `status` is `ok` only when the
SQLite session store is reachable; `warm` means models are loaded. Clone
readiness requires `tts.cloning_active === true`.

`GET /metrics` → operational counters only. It is not part of the public
ingress and is never proxied by the production tunnel.

## WebSocket `/v1/voice`

Connect, then send `hello` first (within 10 s) or be closed:

```json
{"v": 1, "type": "hello", "seq": 0, "turn_id": null,
 "payload": {"ticket": "<hmac>",
             "history": [{"question": "...", "answer": "..."}]}}
```

The ticket encodes the session id; a separate `session_id` field is not
required. `history` is optional and bounded to the last six completed turns
(32 KiB hello cap); it lets the voice session continue an existing text
conversation without a second retrieval path. Server replies `session_created`
then `listening`. Every message is a JSON envelope
`{v, type, seq, turn_id, payload}`; `seq` is a per-connection monotonic counter,
`turn_id` correlates one utterance end to end.

### Client → server

| type | payload | notes |
|---|---|---|
| `audio_frame` | `{audio_b64}` — base64 PCM16 mono 16 kHz | every 20–100 ms while speaking; an optional `final` is ignored — use `end_utterance` |
| `text_message` | `{text}` — 1–500 characters | typed turn on the active voice session; skips VAD/STT and uses the same RAG/TTS/booking path |
| `end_utterance` | `{}` | explicit turn boundary (VAD-confirmed server-side) |
| `interrupt` | `{}` | barge-in: cancels the turn, drops queued audio |
| `ping` | `{}` | keep-alive; server replies `pong` after re-validating session |
| `end_session` | `{reason}` | orderly hangup |
| `playback_complete` | `{}` | client confirms its playback queue drained (advertised via `capabilities`) |
| `playback_metrics` | numeric fields below | optional browser audio health; unknown/non-numeric keys are ignored |

### Server → client

| type | payload | notes |
|---|---|---|
| `session_created` | `{session_id, max_seconds, capabilities}` | ready; capabilities include `playback_complete`, `text_message`, `assistant_text`, `playback_metrics` |
| `listening` | `{}` | capturing |
| `transcript_partial` | `{text, seq, turn_id}` | internal hypothesis; clients must not render it as user-visible transcription |
| `transcript_final` | `{text, turn_id}` | authoritative user turn sent to RAG |
| `assistant_text` | `{text, turn_id}` | final assistant answer for the shared transcript; sent before `turn_end` |
| `rag_started` | `{turn_id}` | retrieval/generation running |
| `audio_frame` | `{audio_b64, sample_rate, final, turn_id}` | PCM16 mono chunks in order; `final` ends that turn's audio stream |
| `turn_end` | `{reason, turn_id}` | `complete` \| `empty` \| `interrupted` \| `stt_failed` \| `rag_failed` \| `tts_failed` \| `server_error` |
| `quota` | `{remaining_seconds}` | usage snapshot on request/session end |
| `error` | `{code, message}` | recoverable (`bad_message`, `*_failed`, …) vs fatal (closes socket) |
| `pong` | `{}` | heartbeat reply |
| `session_ended` | `{reason}` | `user_ended` \| `max_duration` \| `disconnect` |

### Audio framing

- Input: PCM16 mono 16000 Hz, base64 per frame, ≤192,000 bytes/frame
  (`VOICE_MAX_AUDIO_FRAME_BYTES`), ≤60 s audio per turn
  (`VOICE_MAX_TURN_AUDIO_SECONDS`). 16-bit alignment is enforced; odd byte
  counts are rejected.
- Output: PCM16 mono at the TTS native rate (24 kHz for Pocket TTS), chunked
  server-side, DC-blocked with continuous gain and capped at the target peak
  to avoid integer wrap or chunk-boundary clicks, then played through a queued
  AudioContext. Typed turns use the same output path.
- Ordering: frames carry connection `seq`; playback consumes FIFO per turn.
  Stale `turn_id` payloads are discarded.
- WebSocket message cap: 262,144 bytes (`VOICE_MAX_MESSAGE_BYTES`).

### Playback metrics (optional)

Clients that advertise `playback_metrics` may send:

```json
{"v":1,"type":"playback_metrics","seq":12,"turn_id":"...","payload":{
  "sample_rate":24000,"buffer_ms":180,"queue_max_ms":480,
  "render_lag_max_ms":35,"underruns":0,"dropped_frames":0,
  "decode_errors":0,"reconnect_count":0}}
```

Only the listed keys, integer values, and non-negative sizes are accepted.
Free text, audio, and unknown keys are discarded. The server logs numeric
metrics only; it never echoes them or uses them for scheduling.

### Cancellation

`interrupt` (or a new `audio_frame` while speaking) cancels the turn token:
STT/RAG/TTS work aborts at the next checkpoint, queued audio is dropped, and
late results with a dead `turn_id` are ignored. Usage counts session active
time regardless of outcome.

### Limits enforced server-side (never client-side)

5-minute sessions (warning at 4:00) · 600 s/day/visitor · 3 session attempts
per 10 min/IP · one active session · 120 s ticket life · 10 s hello deadline ·
256 KiB WebSocket message cap · 192,000-byte audio frames · 60 s audio/turn ·
RAG timeout 25 s · heartbeat re-validation every 30 s. Browser origins must be
in `VOICE_ALLOWED_ORIGINS`; otherwise the socket closes with code 4403.
