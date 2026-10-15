# P2Z — Local `small.en` Evaluation: Report

## Selection (explicit, reversible, production untouched)

- `docker-compose.local.yml` sets `VOICE_STT_MODEL: small.en`, overriding the
  local `.env` for dev runs only. `config.py` default (`base.en`) and the
  local `.env` are unchanged — production behavior is identical.
- Running model is verifiable: `models_warm` now logs `stt_model` (observed:
  `"stt_model": "small.en"`), and every turn sidecar records full STT config.
- Revert: delete the two lines, `docker compose up -d` (recreate needed —
  env applies at container start, not on restart).

## Found live: empty utterances hung forever (fixed)

A 1 s-silence turn received **no reply at all** (client waited out its full
300 s timeout): the P2N empty-buffer guard `continue`d without answering.
Fixed in `app/main.py`: empty `end_utterance` with a turn in flight stays
silent (protection preserved, p2n re-verified: transcript delivered); with
no turn running it sends `turn_end: empty` and resets to idle. Silence now
answers in **0.01 s**. No new unit path was addable without FastAPI (absent
on host); covered by live probes instead.

## Nine-row side-by-side (identical files, identical decoding)

| attempt capture | base.en (live) | small.en (offline) |
|---|---|---|
| skill ×1 (1.5 s) | rejected (-1.41) | "Low Azure Skill." (-1.02) |
| skill ×2 (1.7 s) | rejected (-0.90) | **exact** (-0.45) |
| skill ×3 (1.7 s) | "Alimosho skill." (-0.26) | **exact** (-0.58) |
| OMG ×1 (1.3 s) | "Welcome, Miss." (-0.68) | **exact** (-0.46) |
| OMG ×2 (1.1 s) | rejected (-0.87) | **exact** (-0.49) |
| OMG ×3 (1.1 s) | exact (-0.21) | exact (-0.55) |
| Hello ×1 (0.6 s) | "And more." (-0.43) | "Alou!" (-0.99) |
| Hello ×2 (0.8 s) | exact (-0.37) | exact (-0.82) |
| Hello ×3 (0.7 s) | "No." (-0.47) | "Hello!" (-0.94) |

Reference set intact on small.en (hello/nimoh/q-aws exact; names at -0.17).
Prompt and all decoding settings preserved throughout.

## Measured costs (small.en vs base.en, warmed, same audio)

- STT: 1.2 s → 3.9 s (~3.3×); live turns 4.9–7.5 s under host load 5–10.
- RAM: +900 MB in-process (base +422 MB); live container 1.56–1.70 GiB
  (VmRSS) vs ~1.4 on base.en. Projected production floor ~1.8 GiB → **2 GB
  VPS still excluded** (measured, not estimated); 4 GB later is comfortable.
- Full q-aws turn completes (482 frames); barge-in 5/5; empty recovery instant.
- Host pressure during session: swap 4.1/5.1 GB used, load 5–10 — slows
  stages, never corrupts transcripts (deterministic reruns match exactly).

## Live-mic steps (for the reporter)

Say each 3×, assistant idle: "Hello, how is your skill?", "Oh my goodness.",
"Hello." Expect exact transcripts (or "Hello!"/retry on the weakest takes),
greeting fast-path replies, and "I didn't catch that" only on true silence.
New captures land in `whisper-captures/` with the active model's config —
compare `model:` fields across runs. Captures preserved; do not delete yet.

## Tests

Voice 171/171 (incl. rag_total + boundary tests); portfolio untouched this
round (voice.test 84/84 + build green from prior round). No thresholds,
models-as-default, contracts, or dependencies changed.
