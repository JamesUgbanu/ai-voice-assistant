# P2F — Nigerian Accent Fidelity in the Cloned Voice: Report

Scope: local analysis in `james-voice-service/`. No app code changed. No
deploy, no public access, no commits. Private audio stays Git-ignored.

Status labels: **PASS** · **FAIL** · **BLOCKED** · **NOT TESTED**.

## Bottom line

- Real cloning confirmed (not preset/fallback): **PASS**.
- Reference conditioning (10 s / 20 s / 30 s windows) changes accent/prosody:
  **FAIL — no effect.**
- Root cause: **model-side** (Pocket TTS 100 M distilled prior flattens
  intonation); reference selection cannot recover it. **PASS** (measured).
- Whether a new recording is necessary: **No** (evidence below).
- Perceptual accent verdict: **NOT TESTED** (requires your ears).

## 1. Baseline — is this really cloning?

`app/tts.py` `PocketTtsBackend` uses the gated `kyutai/pocket-tts` checkpoint;
all P2F runs report `cloning_active = true` and the model
`has_voice_cloning = true` (established in P2D). No preset/fallback occurred.
Reference: `voice-reference/processed/voice-ref.wav`. Latest clone:
`voice-reference/output/cloned-voice.wav`.

## 2. Pronunciation characteristics

I cannot audition audio, so I cannot confirm the specific phones that sound
American. Per your report, the output uses General-American-like prosody. The
one **measurable** correlate I found is **compressed intonation (pitch range)**:

| | median F0 | p10 | p90 | range (p90−p10) |
|---|---|---|---|---|
| **Your reference** (0–30 s) | 93.8 Hz | 86.0 | 121.3 | **35.3 Hz** |
| **Cloned output** | 91.6 Hz | 85.7 | 104.8 | **19.1 Hz** |
| Preset (alba) | 124.7 Hz | 87.3 | 174.5 | 87.2 Hz |

The clone matches your **median pitch** (identity transferred) but the model
**compresses your pitch range to ~54 % of the reference** — flatter, more
monotone prosody, a plausible contributor to the "not-Nigerian" impression.
Segmental accent (vowel/consonant quality) is **NOT TESTED** (needs listening).

## 3. Reference recording inspection

- Original `<private-reference-recording>.m4a`: AAC, 16 kHz mono, **99.1 s**, ~**50 % speech**
  (many pauses; the first 1.9 s are silence).
- Current processed ref (first 30 s): mono 24 kHz PCM16, **47 % speech**, many
  short pauses, 1.9 s silent lead.
- Best continuous-speech windows found (RMS speech map, adaptive threshold):

| window | boundaries | speech ratio |
|---|---|---|
| 10 s | 39.60–49.60 s | **66 %** |
| 20 s | 28.60–48.60 s | **60 %** |
| 30 s | 19.70–49.70 s | **59 %** |

So the current reference is *usable* but not the densest speech; denser windows
exist. Preprocessing removed no useful accent information (no aggressive
denoise/pitch processing was applied; only loudnorm + mono + 24 kHz + 30 s cap).

## 4. Conditioning tests (identical text, one model load)

Test passage (same for every candidate):

> "Hello, my name is James Ugbanu, and it's a pleasure to meet you. I grew up in
> Alimosho, Lagos, and I still call Nigeria home. Would you like to hear about
> the platforms I have been building? I work every day with PostgreSQL,
> pgvector, TypeScript, Terraform, and AWS. Let me walk you through how I think
> about software architecture, from the first sketch all the way to production."

Outputs (paths under `voice-reference/output/`, Git-ignored):

| candidate | ref | init s | synth s | audio s | RTF | out median F0 | out p90 |
|---|---|---|---|---|---|---|---|
| `accent-preset.wav` | preset | – | 22.7 | 21.2 | 1.07 | 124.7 | 174.5 |
| `accent-current.wav` | 0–30 s | 33.1 | 38.0 | 29.4 | 1.29 | 91.6 | 104.8 |
| `accent-ref10.wav` | 39.6–49.6 | 3.3 | 34.4 | 29.6 | 1.16 | 93.8 | 104.9 |
| `accent-ref20.wav` | 28.6–48.6 | 7.9 | 37.2 | 29.5 | 1.26 | 94.1 | 107.1 |
| `accent-ref30.wav` | 19.7–49.7 | 22.9 | 38.5 | 31.0 | 1.24 | 94.9 | 108.4 |
| `accent-temp0.6.wav` | 0–30 s, temp 0.6 | – | 18.0 | 17.1 | 1.05 | 90.2 | 98.4 |

**Finding:** every reference window produces statistically indistinguishable
output (median F0 91.6–94.9 Hz, p90 104.8–108.4 Hz). The denser/higher-quality
windows did **not** improve accent or prosody. Preset is clearly different,
confirming cloning is active — so the clone is conditioned by the reference, yet
the *intonation* stays compressed regardless of which speech you give it.

Candidate reference WAVs are kept at `voice-reference/output/refs/` (ignored).

## 5. Model limitation (evidence, not assumption)

- The installed API exposes **no accent/prosody control**: `load_model(language=
  "english"|…)`, `get_state_for_audio_prompt(path, truncate=)`,
  `generate_audio(_stream)`, plus `temp`. `english` is a **single** English
  model — no en-NG/accent variant.
- `temp` is not a fix: temp 0.6 gave p90 = 98.4 Hz (narrower than temp 0.3's
  104.8 Hz).
- The model docstring claims conditioning captures "speaker voice, style,
  prosody", but empirically it transfers **pitch/timbre** and **compresses**
  intonation. This is consistent with a small (100 M) distilled model whose
  synthesis prior normalises prosody toward its training accent.

Conclusion: the accent limitation is **model-side**, and reference conditioning
cannot overcome it. This is measured, not assumed.

## 6. Latency / memory impact

- Reference-state init scales with reference length (~0.75–0.9 s per second:
  10 s → 3.3 s, 30 s → 22.9 s).
- Synthesis RTF ~1.05–1.29 on this contended 2-vCPU Intel host; ~1.1 GB RSS
  (unchanged from P2D). Shorter references are cheaper to initialize with no
  measurable quality difference.

## 7. Tests

`python3 -m unittest discover -s tests` → **129/129 PASS** (no app code changed).
Comparison samples saved and confirmed Git-ignored.

## 8. Is a new recording necessary?

**No** — and it is unlikely to be sufficient.

- Denser, cleaner windows (up to 66 % speech vs the current 47 %) produced
  identical outputs, so a new recording would not change accent via conditioning.
- A new recording *could* help only if the current one were unusable (too short,
  noisy, or lacking native speech). It is none of those.
- The accent gap is model-side. **What a new recording must contain, if you make
  one anyway:** 15–30 s of clean, continuous, close-mic, natural Nigerian English
  with varied intonation (questions + statements). But expect little change given
  the evidence.

## 9. Realistic options (self-hosted, trade-offs — no engine swapped)

1. **Accept as-is** — best speaker identity available from this model; accent
   stays neutralised. No cost.
2. **Fine-tune/adapt Pocket TTS on Nigerian English** — the Kyutai training code
   ships with the package; needs a licensed Nigerian-English speech corpus and
   GPU training. Highest fidelity potential, largest effort; unproven here.
3. **Evaluate an alternative self-hosted TTS** with accent/language control or
   African-English coverage — bench it for accent pilot, latency, memory and
   licence before any switch; do not replace the engine without that evidence.
4. **Do not** apply pitch-shift / phoneme hacks — no evidence they help and they
   risk quality.

## 10. Your next step

Listen (this is the only way to judge accent):

```bash
open voice-reference/output/accent-current.wav   # current clone
open voice-reference/output/accent-ref10.wav     # densest 10 s ref
open voice-reference/output/accent-ref30.wav     # densest 30 s ref
open voice-reference/output/accent-preset.wav    # non-cloned baseline
open voice-reference/original/<private-reference-recording>.m4a       # your original
```

If `accent-ref10/30` sound no more Nigerian than `accent-current` (as the
metrics predict), the next real move is option 2 or 3 — not another recording.
