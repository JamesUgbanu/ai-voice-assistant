# Reference integration example

Two small, dependency-light files show how to run the voice service without the
original portfolio:

- `echo_backend.py` — a minimal conversational backend implementing the generic
  `HttpResponder` contract (`POST /respond` → SSE `token` frames).
- `voice_client.py` — creates a session, speaks the documented WebSocket
  protocol, sends text or WAV turns, prints transcripts/answers, optionally
  saves synthesized audio, and ends the session cleanly.

They are illustrative but runnable. They contain no personal recordings,
credentials, or portfolio code.

## 1. Quick text demo (no models, no API keys)

Use the built-in STT/TTS stubs so the demo runs on any machine:

```bash
cp .env.example .env
# Edit .env and set at least:
#   VOICE_HMAC_SECRET=<32+ random bytes>
#   VOICE_RESPONDER=http
#   VOICE_RESPONDER_URL=http://127.0.0.1:9000/respond
#   VOICE_USE_STUBS=1

pip install -e .          # full model stack (first run is large)
# or, for this stub demo only:
# pip install fastapi 'uvicorn[standard]' httpx

python examples/echo_backend.py --port 9000 &
uvicorn app.main:app --host 127.0.0.1 --port 8000 &

python examples/voice_client.py \
  --base http://127.0.0.1:8000 \
  --text "Tell me about this service" \
  --text "What did I just say?"
```

Expected: `transcript: Tell me about this service`, then an assistant reply from
the echo backend; the second turn's answer references the first question (the
service forwards bounded history). `turn_end: complete` ends each turn.

(An exact greeting like "Hello" is answered by the built-in greeting fast path
without calling the backend — a deliberate latency feature. Edit
`app/greeting.py` for your own persona or remove a phrase if you want every
utterance to reach your backend.)

> Stubs return deterministic audio and do not perform real recognition. Use real
> models for an audio demo.

## 2. Audio demo (real models)

Remove `VOICE_USE_STUBS` (or set it to `0`) and let the first boot download the
models. Then send a 16 kHz, mono, 16-bit PCM WAV:

```bash
python examples/voice_client.py --base http://127.0.0.1:8000 --wav my-utterance.wav
python examples/voice_client.py --base http://127.0.0.1:8000 --wav my-utterance.wav --save-audio reply.pcm
```

`reply.pcm` is raw little-endian PCM16 at the sample rate reported by the
server's audio frames (24 kHz by default). To seed conversation context, pass
`--history-file history.json` containing `[{"question": "...", "answer": "..."}]`.

Do not use someone else's voice recording without permission. The example
client never records audio itself.

## 3. Point at your own backend

The generic contract is intentionally small:

```http
POST /respond
Content-Type: application/json

{"question": "What is your refund policy?",
 "history": [{"question": "...", "answer": "..."}],
 "sessionId": "opaque-id"}
```

```text
event: token
data: {"text": "Refunds are available "}

event: token
data: {"text": "within 30 days."}

event: done
data: {}
```

Set `VOICE_RESPONDER=http` and `VOICE_RESPONDER_URL` to your endpoint. The
service checks `VOICE_RESPONDER_TOKEN` (sent as `Authorization: Bearer ...`)
when configured. See `../INTEGRATION.md` for cancellation, error, and timeout
semantics. Booking is an optional capability implemented only by the portfolio
adapter; generic backends do not need it.

## Notes

- The examples are excluded from the Docker image (`.dockerignore`).
- `voice_client.py` requires `httpx` and `websockets`; both are installed by the
  service's `uvicorn[standard]` dependency.
- Do not point the examples at a production service unless you are authorized to
  create sessions and consume its quota.
