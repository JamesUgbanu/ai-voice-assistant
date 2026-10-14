# P2G — Self-Hosted TTS Alternatives for Nigerian Accent + Voice Cloning

Scope: local evaluation in `james-voice-service/`. The Pocket TTS adapter is
unchanged (baseline preserved). No deploy, no DNS/proxy/secret changes, no
commits, no external audio uploads. Private audio stays Git-ignored.

Status labels: **PASS** · **FAIL** · **BLOCKED** · **NOT TESTED**.

## 1. Bottom line

- Several self-hostable models clone a voice; several have Nigerian English; but
  **no permissively-licensed, CPU/ARM-feasible self-hosted model currently gives
  Nigerian-accented cloning of a specific speaker** within 3 vCPU / 4 GB.
- One candidate (Uphando Naija VITS, Apache-2.0) **passed** the hardware test and
  produces genuine Nigerian-accent speech — but it is a **fixed single voice, not
  a clone** of James.
- Recommendation: **retain Pocket TTS** for cloning identity; **investigate
  fine-tuning** for accent (details §7). Do **not** swap the engine now.
- Perceptual accent/similarity: **NOT TESTED** (needs your ears).

## 2. Ranked shortlist (≤3) with verified licensing

| # | Model | License (verified) | Cloning | Nigerian English | Verdict |
|---|---|---|---|---|---|
| 1 | `Uphando-ng/naija_eng_male_accent_tts` | **Apache-2.0** | **No** (VITS, `num_speakers=1`) | **Yes** (Naija male, 16 kHz) | **PASS feasibility** — tested |
| 2 | `PlotweaverAI/nigerian-english-ft-tts` | Apache-2.0 (declared) | unverified | declared | **FAIL hardware** — ~4.7 GB (3.4 GB ckpt) > 4 GB RAM |
| 3 | `coqui/XTTS-v2` | **CPML (non-commercial)** | Yes (strong) | via reference (accent-transfer possible) | **FAIL license** — non-commercial |

Eliminated before install:

- `SWivid/F5-TTS` — **CC-BY-NC-4.0** (non-commercial) → rejected on licensing.
- `SparkAudio/Spark-TTS-0.5B` and `mradermacher/spark-tts-naijalingo-GGUF` —
  **CC-BY-NC-SA-4.0** → rejected on licensing (despite NaijaLingo fine-tune).
- `hexgrad/Kokoro-82M` (Apache-2.0, fast CPU) — no cloning and US/GB voices only;
  kept out of the shortlist because it addresses none of the two objectives
  (noted as a latency/expressiveness reference only).
- `rhasspy/piper-voices` (MIT, very CPU/ARM-friendly) — **no `en_NG` voice** and
  no arbitrary cloning → cannot help here.
- `myshell-ai/OpenVoiceV2` (MIT, 131 MB) — timbre conversion; its accent comes
  from the MeloTTS base (US/BR/AU/IN), **no Nigerian** → not an accent fix alone.

## 3. Evidence for cloning and Nigerian-English support

- **Uphando VITS**: HF Transformers `VitsModel` + `VitsTokenizer`,
  `architectures=['VitsModelForPreTraining']`, `sampling_rate=16000`,
  `num_speakers=1`. Single-speaker ⇒ **no zero-shot cloning**. Nigerian English
  target is its stated purpose; corroborated empirically (§4) — the US-tuned
  Whisper mis-heard its output (`Alimosho Lagos` → "a limo show legos"), a sign of
  a genuinely different accent, not of English-in-general.
- **Pocket TTS** (baseline): clones (gated `kyutai/pocket-tts`, MIT); **no**
  locale/accent control (`english` is one model). P2F measured a **flat**
  intonation (p90−p10 ≈ 19 Hz) vs the reference ≈ 35 Hz.
- No candidate combines both. `PlotweaverAI/nigerian-english-ft-tts` *might* (GPT
  TTS fine-tune with a `pretrained_model.pt` base) but is unverifiable within the
  RAM budget; it was not downloaded.

## 4. Controlled comparison (identical passage, one passage for all)

Passage (same text used for Pocket preset/clone in P2F and for Uphando here):

> "Hello, my name is James Ugbanu, and it's a pleasure to meet you. I grew up in
> Alimosho, Lagos, and I still call Nigeria home. Would you like to hear about
> the platforms I have been building? I work every day with PostgreSQL,
> pgvector, TypeScript, Terraform, and AWS. Let me walk you through how I think
> about software architecture, from the first sketch all the way to production."

| Engine | Cloning | audio s | synth s | RTF | peak RSS | F0 med / p10 / p90 (Hz) |
|---|---|---|---|---|---|---|
| Pocket TTS **clone** (baseline) | **Yes** | 29.4 | 38.0 | 1.29 | ~1.10 GB | 91.6 / 85.7 / 104.8 (**range 19**) |
| Pocket TTS preset (alba) | No | 21.2 | 22.7 | 1.07 | ~1.0 GB | 124.7 / 87.3 / 174.5 |
| **Uphando Naija VITS (male)** | **No** | 23.1 | 17.0 | **0.74** | **0.70 GB** | 131.1 / 111.1 / 173.9 (**range 63**) |

Reading: Uphando is **~2× faster than real time and 40 % lighter** than Pocket,
with a **3× wider pitch range** (more natural intonation) — but it speaks in its
own fixed voice, not James's. Pocket preserves James's **median pitch** (91.6 vs
reference 93.8) but **compresses his range** (19 vs 35 Hz).

Audio paths (all Git-ignored, private):

```
voice-reference/output/p2g-naija-male.wav    # Uphando Nigerian-accent VITS (16 kHz)
voice-reference/output/accent-current.wav    # Pocket clone, same passage (P2F)
voice-reference/output/accent-preset.wav     # Pocket preset baseline (P2F)
voice-reference/original/Alimosho.m4a        # your recording
```

Objective content check: Whisper transcribes `p2g-naija-male.wav` to the passage
(with accent-induced substitutions), so it is real speech of the intended text.

## 5. Hardware constraint test (explicit 3 vCPU / 4 GB)

Uphando VITS was run in a container limited to **`--cpus=3 --memory=4g`**
(cgroup `memory.max=4294967296` confirmed), CPU-only:

- Peak RSS **0.70 GB** (fits 4 GB with headroom), RTF **0.735**, 16 kHz mono.
- Model 332 MB; inference deps: `torch` (CPU), `transformers`; VITS is
  ONNX-exportable and known to run on ARM/Raspberry-Pi-class CPUs.
- Cold start 198 s **including the 332 MB download**; load-only thereafter is a
  few seconds.

Pocket TTS baseline: ~1.1 GB RSS, RTF ~1.1–1.3 on this 2-vCPU Intel host
(contended) — also fits, unchanged from P2D. `PlotweaverAI` (~4.7 GB on disk)
**exceeds** the 4 GB RAM budget → rejected.

**Caveat:** the test host is **x86_64 Intel**, not the target ARM VPS. The numbers
show *fit*, not ARM throughput. Re-measure on ARM before relying on them.

## 6. Human-listening results

**NOT TESTED.** I cannot audition audio. Accent fidelity, timbre similarity and
naturalness must be judged by ear. Listen to:

```bash
open voice-reference/output/p2g-naija-male.wav   # clearly Nigerian-accent voice (not James)
open voice-reference/output/accent-current.wav   # James's cloned voice (neutralised accent)
open voice-reference/original/Alimosho.m4a
```

The question to answer: is a **Nigerian-accented voice that is not James** better
or worse for the portfolio than **James's voice with a neutralised accent**?

## 7. Recommendation

**Retain Pocket TTS** as the production engine (best CPU/ARM streaming clone, MIT,
no external service) and pursue accent via training, in this order:

1. **Investigate fine-tuning (primary).** Pocket TTS ships its own training code
   (`training/`, from the package). Fine-tuning on James's Nigerian-English speech
   is the only path that could deliver **accent + identity together** while
   keeping the current architecture. Needs: a licensed Nigerian-English corpus
   (James's recording is too small/sparse at ~50 % speech; collect more) and GPU
   time. This is research, not a drop-in.
2. **Two-stage experiment (secondary).** Uphando VITS for the Nigerian accent +
   a voice converter (e.g., OpenVoiceV2, MIT) to map timbre onto James. Adds a
   model and latency; result quality unproven. Only if (1) is out of reach.
3. **Accept** Pocket's neutralised accent (status quo) — best identity today.

Do **not** replace the engine, and do **not** use XTTS-v2 / F5-TTS / Spark-TTS
(non-commercial licences) on a public portfolio.

If a new James recording is made, it must be **15–30 s of clean, continuous,
close-mic, natural sentences with varied intonation** — the current memo is
sparse and yields no measurable accent benefit (P2F).

## 8. What was not done (and why)

- No alternative engine adapter was implemented: no candidate passed **all** of
  {commercial licence, cloning, Nigerian English, ≤4 GB/CPU} simultaneously, so
  swapping would trade one objective for another.
- No large checkpoints (PlotweaverAI 4.7 GB, CosyVoice2 4.9 GB, Chatterbox 14 GB,
  Zonos 3.2 GB) were downloaded — rejected on size/licence/CPU before spending
  bandwidth.
- No ARM measurement (no ARM host available locally).

## 9. Tests

`python3 -m unittest discover -s tests` → **129/129 PASS** (no app code changed).
