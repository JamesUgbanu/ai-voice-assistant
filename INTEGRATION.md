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
  "question": "string, trimmed, 3..500 chars (required)",
  "history": [ {"question": "≤500 chars", "answer": "≤2000 chars"} ],  // max 6 turns, optional, default []
  "debug": false,                                                     // optional, default false
  "sessionId": "opaque string ≤64 chars, optional"
}
```

The voice service sends `{question, history: [], debug: false}` — the
voice agent holds conversation server-side, so each voice turn is
self-contained. History is accepted by the schema but unused.

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

Non-2xx statuses: `400` invalid body, `401` bad/missing service token,
`413` oversized body, `429` service budget exhausted (with `Retry-After`).

## Authentication (added for server-to-server)

- Browser clients: unchanged, no header, existing IP/session budgets apply.
- Voice service: `Authorization: Bearer <RAG_SERVICE_TOKEN>` verified with
  a constant-time compare against server env; wrong/missing token → 401.
  Authenticated calls use a dedicated service budget instead of the
  visitor IP/session windows (all voice traffic shares one egress IP),
  with identical validation, body caps, concurrency slots, contact-action
  budgets, and telemetry. No secrets in `NEXT_PUBLIC_*` or browser bundles.

## Worked example (sanitized)

Request:

```http
POST /api/rag HTTP/1.1
Content-Type: application/json
Authorization: Bearer <redacted>

{"question": "What has James built with AWS?", "history": [], "debug": false}
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
whitespace/markdown, caps at 2800 chars (compact enough for spoken
delivery and small webhook-style payloads), and treats empty/error streams as a spoken
apology — never fabricated text.

## What the voice service must never do

Connect to portfolio PostgreSQL/pgvector. Import portfolio modules.
Copy the corpus, embeddings, or retrieval code. Exceed schema bounds.
Trust client-supplied usage numbers. Log questions, answers, or tokens.
