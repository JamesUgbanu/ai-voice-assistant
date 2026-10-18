# Portfolio RAG integration contract (verified from implementation)

Source of truth: the portfolio repository (`/api/rag`, `lib/rag/pipeline.ts`,
`lib/ratelimit.ts`). This document mirrors the **verified** contract as of
the voice-service build date. Re-verify before changing the client — never
assume endpoint names, schemas, or streaming behavior.

## Endpoint

`POST {VOICE_RAG_API_URL}` (production: `https://www.jamesugbanu.com/api/rag`)

Headers: `Content-Type: application/json`, `Authorization: Bearer <VOICE_RAG_SERVICE_TOKEN>`

## Request schema (zod, exact bounds)

```json
{
  "question": "string, trimmed, 2..500 chars (portfolio schema; the voice client requires ≥3)",
  "history": [ {"question": "≤500 chars", "answer": "≤2000 chars"} ],  // max 6 turns, optional, default []
  "debug": false,                                                     // optional, default false
  "sessionId": "opaque string ≤64 chars, optional",
  "mode": "voice"                                                    // optional; concise spoken answer + isolated cache
}
```

The voice service sends `{question, history: <last six completed turns>, debug: false, mode: "voice"}`.
Only completed, uncancelled turns are appended, so retries cannot duplicate
history. The voice client caps the list at six turns; the portfolio accepts up
to ten. The portfolio's authenticated service budget is used instead of visitor
IP/session windows.

## Response: Server-Sent Events

`Content-Type: text/event-stream`, frames shaped exactly as:

```
event: <type>
data: <JSON>

```

Event types: `meta` (`{sessionId, route, cached, ...}`), `token`
(`{text}` — append in order), `sources` (`{sources: [...]}` — voice
ignores these; citations are never spoken), `done` (`{metrics}`),
`error` (`{message}` — user-safe fallback copy).

Non-2xx statuses: `400` invalid body, `401` bad service token, `413` oversized
body, `429` service budget exhausted (with `Retry-After`). The voice client
passes `VOICE_RAG_TIMEOUT_SECONDS` (default 25 s) as the per-operation timeout,
aborts on turn cancellation, and maps transport, HTTP, empty, and pipeline
failures to `turn_end {reason: "rag_failed"}` without fabricating an answer. If
audio has already started, the pipeline finalizes the partial answer instead of
failing the whole turn.

## Authentication (added for server-to-server)

- Browser clients: unchanged, no header, existing IP/session budgets apply.
- Voice service: `Authorization: Bearer <RAG_SERVICE_TOKEN>` verified with
  a constant-time compare against server env; a wrong token → 401. A missing
  Authorization header is treated as public browser traffic, not as an
  authenticated service call.
  Authenticated calls use a dedicated service budget instead of the visitor
  IP/session windows (all voice traffic shares one egress IP), with identical
  validation, body caps, concurrency slots, contact-action budgets, and
  telemetry. No secrets in `NEXT_PUBLIC_*` or browser bundles.

## Worked example (sanitized)

Request:

```http
POST /api/rag HTTP/1.1
Content-Type: application/json
Authorization: Bearer <redacted>

{"question": "What has James built with AWS?", "history": [], "debug": false, "mode": "voice"}
```

Stream (abridged):

```
event: meta
data: {"sessionId":"…","route":"knowledge","cached":false}

event: token
data: {"text":"James has built "}

event: token
data: {"text":"several backends on AWS…"}

event: sources
data: {"sources":[{"title":"…","anchor":"…"}]}

event: done
data: {"metrics":{"route":"knowledge", ...}}
```

The voice client folds `token` frames into one answer string, compacts
whitespace/markdown, caps at 2800 chars (compact enough for spoken delivery and
small webhook-style payloads), and treats empty/error streams as a failed turn —
it never fabricates text.

## What the voice service must never do

Connect to portfolio PostgreSQL/pgvector. Import portfolio modules.
Copy the corpus, embeddings, or retrieval code. Exceed schema bounds.
Trust client-supplied usage numbers. Log questions, answers, or tokens.
