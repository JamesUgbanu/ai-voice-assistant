# P2H — Pocket TTS Nigerian English Fine-Tuning Feasibility Report

Scope: audit only. No training run, no production changes, no commits, no
recordings/checkpoints added to Git or images. Engineering baseline (Pocket TTS
adapter, RAG path, streaming/cancellation/barge-in, quotas, auth, privacy) is
unchanged.

Status labels: **PASS** · **FAIL** · **BLOCKED** · **NOT TESTED**.

## Bottom line

**Decision: feasible only with substantial changes.**
The training pipeline genuinely supports fine-tuning the English model, and its
checkpoints load in the existing inference API. But the current 99-second
recording is **~3,600× below** the documented data minimum, and the large
Nigerian-English speech corpora are **non-commercial-licensed**, so the corpus
must be created or licensed before any training is possible. There is **no
small-data / LoRA / single-speaker adaptation path** in this architecture.

| Item | Status |
|---|---|
| Training code present and usable (source distribution) | **PASS** |
| Fine-tune-from-pretrained config (`finetune.yaml`) | **PASS** |
| Output checkpoint loadable by existing inference API | **PASS** |
| Small-data / single-speaker adaptation supported | **FAIL** (not offered) |
| 99 s recording sufficient | **FAIL** (~0.03 h vs 100 h minimum) |
| Commercially-licensed ≥100 h Nigerian-English corpus available | **BLOCKED** (large sets are NC) |
| GPU compute within a realistic small budget | **PASS** (~$10–30) |
| Overall | **Feasible only with substantial changes** |

## 1. Verified training capabilities and limitations

Audited `pocket-tts 3.3.0` source dist (`training/` ships the code; the installed
wheel only packages `pocket_tts`). Key files: `training/README.md`,
`configs/{scratch,finetune,finetune_language,depth_distill}.yaml`, `train.py`,
`args.py`, `dataloader/*`.

What is trainable / supported:

- **Fine-tune from the pretrained model is explicitly supported.**
  `finetune.yaml` (comment: "Finetune a released model on more data in the SAME
  language — new voices, a new domain") sets
  `model_config: pocket_tts/config/english_2026-04_24l.yaml` and
  `start_from_pretrained: true`. `finetune_language.yaml` is the same for a new
  language/tokenizer.
- **Only the FlowLM trains; Mimi (the codec) is frozen.** `train.py` builds the
  optimizer from `model` only (`build_optimizer(model, …)`,
  `clip_grad_norm_(model.parameters(), …)`); Mimi is used to encode latents. The
  single optional freeze is `freeze_head`.
- **Accent is learned into the model prior; speaker identity is zero-shot.** The
  model conditions on a `voice_prompt` at inference (`get_state_for_audio_prompt`)
  with `voice_dropout: 0.2` during training. So fine-tuning shifts the *accent
  prior*, and James's identity is still supplied at inference from his recording —
  the corpus does **not** need to be James.
- **Pipeline is two-step for CPU deployment:** fine-tune the 24-layer teacher
  (`finetune.yaml`, `max_steps: 20000`), then depth-distil to the 6-layer student
  (`depth_distill.yaml`, `max_steps: 200000`) which is what runs on CPU.
- **Checkpoints load in inference.** Training writes `model.safetensors` (EMA,
  "the format the inference code expects") and resumable `checkpoint_<N>.pt`;
  `TTSModel.load_model(config=…, checkpoint=…)` and
  `load_training_checkpoint()` accept them. The **adapter would need a small
  change** to point at a custom config/checkpoint (today it calls `load_model()`
  with defaults) — not a rewrite.
- **Licensing of code:** MIT (Pocket TTS); training deps are in the `dev` group
  (`torch`, `torchaudio`, `transformers`, `soundfile`, `sphn`, `utmos-pytorch`,
  `whisper-normalizer`, …). Training is documented for **Linux + NVIDIA GPU**.

Limitations (why fine-tuning is not a drop-in):

- **No speaker-adaptation mode.** No LoRA, no adapter layers, no per-speaker
  recipe, no "few minutes of one voice" path. The documented use is *more data,
  new voices, new domain*.
- Accent coverage is a **dataset property**: the README states a model does poorly
  on "a specific accent" when "those voices are under-represented in the dataset"
  → to gain Nigerian English the corpus must contain substantial Nigerian English.
- Output must be **re-distilled to 6 layers** for the CPU VPS; you cannot simply
  fine-tune the 6-layer released student (no such config).

## 2. Data sufficiency

Documented minimums (training/README.md): "100 hours is a minimum" / "aim for
1000+ hours"; the quickstart's 200 h produces "intelligible" but not good speech.
Training also requires **word alignments** (forced alignment, Wav2Vec2 CTC) and
clean ~30 s max segments.

`<private-reference-recording>.m4a` = **99.1 s ≈ 0.0275 h** — roughly **3,600× short of the 100 h
minimum**, and it is sparse (~50 % speech; P2F). There is **no documented method
that adapts this architecture from 99 s.** Single-speaker fine-tuning of a
100 M+ flow-matching LM on 99 s would overfit/catastrophically forget, and no LoRA
escape hatch exists.

**Defensible estimate:** to shift the accent prior, the fine-tune corpus needs at
least the documented floor of **~100 h of Nigerian-English speech** (multi-speaker,
transcripts + alignments), and realistically **300–1,000 h** to avoid degrading
intelligibility/other voices. A single speaker (even James recording for weeks)
cannot supply this; a multi-speaker Nigerian-English corpus is required — which is
a licensing/scale problem, not a technical one.

## 3. Dataset rights and recording plan

Checked on Hugging Face (licenses are as tagged):

| Corpus | License | Size | Usable commercially? |
|---|---|---|---|
| `benjaminogbonna/nigerian_accented_english_dataset` | **Apache-2.0** | ~121 MB (a few hours) | **Yes** |
| `naijavoices/naijavoices-dataset` | **CC-BY-NC-SA-4.0** | ~446 GB | **No** (NC) |
| `intronhealth/afrispeech-200` | **CC-BY-NC-SA-4.0** | ~62 GB | **No** (NC) |
| `Donmonc/nigerian_english_tts`, `olua/Nigerian_accented_English*` | none stated | ~56–208 MB | Unclear (no license) |

The only clearly commercially-usable Nigerian-accented English set is **Apache-2.0
and only a few hours** — an order of magnitude below the 100 h floor. The large,
high-quality corpora are **non-commercial**, so they cannot be used for a public
portfolio offering services.

**Rights/recording plan (if pursued):** either (a) commission/license ~100 h of
Nigerian-English speech with signed speaker consent and a commercial-use clause,
plus transcripts (and run forced alignment locally), or (b) use the Apache-2.0 set
to prototype at small scale and accept it cannot meet the documented floor. Do not
scrape or use third-party voices without rights; do not upload James's recording
anywhere.

## 4. Compute and cost

From the README timing tables and configs (NVIDIA, single GPU):

- Memory: default batch 64 ≈ **54 GiB VRAM** (H100-80GB); a consumer 24 GB GPU
  uses `batch_size: 16` with `grad_accum_steps: 4` in ~16 GB.
- Fine-tune: **20,000 steps** (`finetune.yaml`). ≈ 53 min on 1×H100 (6.3 steps/s);
  ≈ 3–4 h on a 24 GB consumer GPU.
- Distil to 6 L: **200,000 steps** (`depth_distill.yaml`). ≈ 5.3 h on 1×H100;
  ≈ 18–24 h on a 24 GB consumer GPU.
- Data prep: HiFiTTS-2 example downloads ~15 GB for 200 h; prepared latents
  ~6 GB/1,000 h → for 100 h, a few GB. Storage modest.
- New alignment step (local) needs a GPU-capable Wav2Vec2 run over the corpus.

**Experiment cost:** ~**6–8 h on 1×H100 (~$15–25)** or ~**20–28 h on 1×RTX 4090
(~$8–15)** at current spot pricing, plus data acquisition. **Inference stays
CPU-only** (6-layer model) — the VPS requirement does not change.

Caveat: these are published/derived estimates, **not measured here**; no GPU was
used in this audit.

## 5. Proposed controlled experiment (only after a corpus exists)

Smallest test of "does adaptation improve Nigerian prosody while preserving
James's identity?"

- **Baseline:** current Pocket TTS clone (gated checkpoint), same reference.
- **Data split:** partition the corpus **by recording/session** into train/valid
  (never split near-identical audio across the boundary); hold out a fixed
  evaluation script.
- **Evaluation sentences:** fixed, never in training — include ordinary speech,
  Nigerian-pronunciation-sensitive words/phrases, questions, long sentences, and
  technical terms from the portfolio.
- **Metrics:** WER (Whisper), speaker similarity (Wav2Vec2/WavLM-SV), UTMOS,
  pitch distribution (F0 range, P2F method), **and human listening** (primary for
  accent). Success = accent improves **without** speaker-similarity/UTMOS
  regression beyond a fixed threshold.
- **Failure threshold & rollback:** if WER rises >0.5 pp or speaker-sim drops
  >0.02 vs baseline, reject and keep the released checkpoint. Keep
  `runs/*/checkpoint_*.pt` + `model.safetensors` and the released model for
  rollback; never overwrite the production config.

## 6. Final decision

**Feasible only with substantial changes — not with the current data or as a
small-data adaptation.** Concretely, to proceed you must:

1. Acquire/commission **≥100 h (ideally 300–1,000 h) of commercially-licensed,
   consented Nigerian-English speech** with transcripts and local alignments. The
   public large corpora are non-commercial; only a few Apache-2.0 hours exist.
2. Run the **24 L fine-tune → 6 L distill** pipeline on a rented GPU
   (~$10–30, ~1 day), then load the resulting checkpoint via a small adapter
   change (custom config/checkpoint path).
3. Validate with listening + metrics, keeping rollback to the released model.

Until (1) exists, fine-tuning has no realistic chance of improving both accent and
identity, so **do not start training**. The pragmatic interim remains the P2G
recommendation: retain the current clone (identity), and optionally offer a
Nigerian-accent preset voice (Uphando VITS) as a clearly-labelled alternative.
