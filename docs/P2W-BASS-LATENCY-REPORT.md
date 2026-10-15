# P2W — Bass-Heavy Captures, Filtering Experiments, and Latency: Report

## 1. Are the captures objectively bass-heavy? Yes.

FFT band energies (share of total power) on reporter captures vs references:

| file | <80 Hz | 80–300 | 300–3400 | >3400 |
|---|---|---|---|---|
| reporter skill take (1.7 s) | **41.8%** | 54.8% | **2.7%** | 0.7% |
| reporter OMG take (1.1 s) | 16.9% | 81.4% | 1.4% | 0.3% |
| reporter Hello take (0.6 s) | **47.2%** | 44.0% | 8.8% | 0.0% |
| hello.wav ref | 0.0% | 51.3% | **48.5%** | 0.1% |
| nimoh.wav ref | 0.0% | 57.1% | **40.9%** | 2.0% |

No DC offset (≤2 units), no clipping, complete durations, correct 16 kHz
mono PCM16 throughout. The bass dominance is real signal content (room
rumble + proximity/AGC behavior at capture), not playback coloration — and
our pipeline cannot create it (decimation, base64, concatenation, and
int16→float32 scaling are all spectrally neutral; verified by construction).
The 300–3400 Hz speech band is nearly vacant in reporter audio: consonant
identity (skill/skin/kill, hello/alo) lives exactly there.

## 2. Filtering experiments (offline copies only — live pipeline untouched)

Zero-phase FFT high-pass, RMS-preserved, small.en, identical decoding:

| input | original | hp80 (60–100 ramp) | hp100 (80–120 ramp) |
|---|---|---|---|
| skill take ("Alimosho skill.") | "Alimosho skill." | "Alimosho skill." | "Hello, I am SirSkew." (worse) |
| OMG take | "Oh my goodness." | unchanged | unchanged |
| Hello take ("And more.") | "And more." | unchanged | unchanged |
| hello / nimoh / q-aws refs | exact | exact | exact ("Hello!") |

Verdict: filtering makes **no meaningful accuracy difference**; hp100
actively degrades (cuts into voicing). The rumble is a symptom of a
low-treble capture, not a mask removable by EQ — removing it cannot restore
the vacant speech band. **No live filtering change made**, per mandate.
I cannot listen to the outputs (stated limitation); spectra confirm the
processing did what was specified.

## 3. Latency breakdown (fresh, small.en, load ~4)

| turn | eou→transcript | RAG 1st | RAG tail | TTS 1st | eou→audible | total |
|---|---|---|---|---|---|---|
| hello (fast path, no RAG) | 3.7 s (STT) | — | — | 0.3–0.6 s | **3.96 s** | 7.7 s |
| q-aws (full RAG) | 4.2 s | 4.5 s | 31.3 s | 0.28 s | **8.9 s** | 42.6 s |

Greeting-vs-RAG comparison (§4): the existing fast path saves the entire
~35 s RAG leg with no new provider and no hard-coded knowledge answers.
Audio is streamed (first audible 8.9 s ≪ 42.6 s total); browser overhead
~ms (measured previously); TTS signal healthy; container 1.89 GiB post-turn;
swap still pressured (host-wide, slows stages, corrupts nothing).

## 4. Smallest evidence-supported next change

None for accuracy or latency in code: the rumble cause sits before our
pipeline (mic placement/gain/room — needs the pending close-mic comparison
recording from the reporter, same phrase/style), and latency is RAG-tail +
single-CPU bound. If a closer, louder take still mistranscribes, re-run this
exact experiment set on the new capture before considering anything else.
