# P2R — Total Silence in Browser (Playback Gate): Report

## 1. Confirmed root cause

`components/TalkToJames.tsx` never established the current turn, so the P2M
stale-audio gate discarded the entire answer. The server sends
`transcript_final`'s turn id **in the payload** (`{"text", "turn_id"}`); only
audio frames carry it at envelope level (`app/main.py` `emit_frame` vs
`make_envelope`, which defaults the envelope field to null). The client only
read the envelope field, so `turnRef.current` stayed null and
`acceptsAudioFrame(null, …)` dropped every frame.

Proven in headless Chromium (default autoplay policy, fake mic) before the
fix: **493 answer frames arrived, 0 buffer sources scheduled**, UI stuck in
"processing", AudioContext running with advancing clock. Server-side probes
(p2j/p2m) never caught it — they count frames, not turn ids.

## 2. Smallest fix

- `TalkToJames.tsx` `transcript_final`: accept the turn id from the envelope
  **or** the payload (`payloadTurnId`), establishing the turn before its audio
  arrives. Nothing else changed; the gate, barge-in, endpointing, and all
  server code are untouched.

## 3. Verification (all in a real browser unless noted)

- After fix: 19+ buffer sources scheduled on a running context with advancing
  clock; UI enters "speaking" with the Interrupt button (`browser-speaking.mjs`
  passes); looped mic speech then triggers auto-barge-in → clean interrupt →
  listening (full lifecycle in one run).
- AudioContext autoplay exonerated (running in-browser under default policy).
- Unit/wiring suites 64/64; full portfolio 603 pass (2 pre-existing unrelated
  failures: `cv-detailed` missing `/tmp` fixture, `agent-sdr` #8); `tsc`,
  `eslint`, `next build` clean; voice service 162/162 unchanged (no service
  change); live p2j/p2m/p2n probes pass.

## 4. Explicit status

- Normal voice conversations: **restored, including audible playback** — the
  reported "can't hear any response" is fixed at its root.
- Auto-barge-in: **remains enabled** and demonstrated working end to end.
- Remaining limitations: first audio still trails RAG (upstream, ~3–11 s
  measured); a clipped mic-file start in the harness ("James Built with AWS")
  is a fake-mic startup artifact, not product behavior; production still needs
  `docker compose up -d --build voice`; local turn capture stays on until the
  reporter confirms audibility.
