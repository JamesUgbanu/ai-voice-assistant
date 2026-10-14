# P2T — Real-Microphone Diagnostics and Deployment Audit: Report

## 1. Deployment status (§1)

- **Local voice container == working tree**: `docker cp` + `cmp` on
  `app/stt.py`, `main.py`, `pipeline.py`, `config.py` — all identical. Local
  runs the P2O guard + turn fixes.
- **Local browser** tests the dev portfolio (`:3000`, hot-reloads
  `TalkToJames.tsx`/`voice.ts` on save, flags `ENABLED=1`,
  `BASE_URL=http://localhost:8000` confirmed in server env).
- **Production is unreachable and its version unverifiable from here**:
  `voice.jamesugbanu.com` → NXDOMAIN, HTTPS 000; SSH blocked on changed host
  keys (awaiting owner confirmation). Nothing has ever been deployed by these
  sessions. No production service was restarted.
- Rebuild map: portfolio client changes hot-reload in dev but need `next
  build` + redeploy for production; voice-service code is baked into the image
  (`COPY app`), so fixes need `docker compose up -d --build` + recreate —
  a bare restart is insufficient. Model weights (`voice-models` volume), DB
  (`voice-data`), and `.env` (read at import) persist across recreates.

## 2. Real-mic diagnostic mode (§2, the only code change)

`TalkToJames.tsx` gains a temporary panel rendered only with `?voice-diag=1`
(`DiagSnapshot`): state, mic level (+active flag), frames above the 0.15
threshold (`hi=above/total`), consecutive qualifying frames, guard ms left,
barge fired Y/n, interrupt count, last turn_end reason, current turn (6 hex
chars), playback queue length, AudioContext state. Counters cost a few integer
ops per frame and always run; the panel + 500 ms snapshot pump exist only in
diag mode. **No audio, transcripts, or secrets** — numbers and random IDs only.
Verified live in harness (`lvl=0.316* hi=267/679 … int=4 end=interrupted`).
Remove after hardware verification.

## 3. Manual procedure (§3) — please run once, ~3 minutes

Open `http://localhost:3000/?voice-diag=1` → Ask James → Voice → Start talking
(allow mic; headphones first if available, then speakers):

1. Say **"Hello"** — note the `You:` transcript and the diag `lvl` while speaking.
2. Wait for the spoken answer — note whether it is audible.
3. Mid-answer, say **"Stop"** clearly — watch `consec` climb, `fired=Y`,
   `int` increment, state return to listening.
4. Repeat with **"Hi, James"** and one normal-length sentence.
5. Repeat steps 1–4 on laptop speakers; note any `fired=Y` while YOU are silent
   (echo false-trigger) and whether `lvl` during silence stays near 0.
6. For each step record: device (speakers/headphones), diag line at the key
   moment, transcript shown, whether audio played/stopped, end state.

Report back the six rows (a phone photo of the diag panel at each key moment
is ideal). The server also saves each utterance's PCM locally for direct
Whisper comparison (§4).

## 4. What the evidence already proves (§4–§5)

- Browser captures of real attempts decoded as `Alo.` (-0.93), `Blo.` (-0.89),
  `Welcome home.` (-0.87), `Alimosho, Alimosho,` (loop, runaway timestamps);
  envelopes show quiet onsets + mid-word dips — capture-side degradation, not
  decoder failure (clean audio decodes exactly, incl. names).
- Detector matrix (in-browser, fake mic): fires ≤400 ms exactly-once at 1.0x
  and 0.18x; silent at 0.05x, clicks, silence. Threshold/guard unchanged on
  this evidence; fluctuation-tolerance rejected (reporter's loud patterns
  already satisfy consecutive-3).
- Full lifecycle acceptance 5/5 (`verify-barge.mjs`); playback gate fix
  verified (169 scheduled sources); suites: portfolio 607 (2 pre-existing
  unrelated), voice 162/162; `tsc`/`eslint`/`next build` clean.

## 5. Honest status and limitations

- **Not claimed**: real-mic barge-in reliability, Nigerian-accent accuracy,
  AEC quality on speakers — all require the §3 run above; fake-mic evidence
  does not transfer to acoustics.
- No defect fix this round beyond the diagnostic mode: no measurement yet
  demonstrates a code defect in detection or transcription on the current tree.
  If the diag run shows case A (level never reaches 0.15), the next step is
  input-device/OS-gain investigation, not threshold changes. Cases B–E each
  have a mapped fix location, to be applied only on observation.
