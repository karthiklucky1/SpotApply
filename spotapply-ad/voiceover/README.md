# Voiceover

The narration in `public/voiceover/*.wav` is synthetic speech from
**Kokoro-82M** (Apache-2.0), voice **`af_heart`**, generated locally. No paid
voice service, no account and no network call at synthesis time.

| File | What it is |
|---|---|
| `lines.json`, `lines60.json` | One entry per scene of the 30-second ad / the 60-second explainer (ids `w_*`). `tts` is what the model reads (spelled for pronunciation); `caption` is what a viewer reads. |
| `synth.py` | Synthesizes every line with `kokoro-onnx`. |
| `build_voices.py` | Packs the per-voice `.bin` tensors into the `.npz` `kokoro-onnx` loads. |
| `asr_check.py` | Offline speech-to-text (pocketsphinx) used to confirm each clip says its line. |
| `make-srt.mjs` | Writes `out/spotapply-social-30s.srt` from `src/timeline.ts` + `lines.json`; with `60`, `out/spotapply-explainer-60s.srt` from `src/w60/timeline60.ts` + `lines60.json` (long lines split in two at the comma or colon nearest the middle). |

Placement lives in `src/timeline.ts` (`VOICEOVER`) and `src/w60/timeline60.ts`
(`VOICEOVER60`): each line starts `lead` frames into its scene. Change a
line's length there if you regenerate it.

## Pronunciation

Two spellings in `tts` exist only for the model:

- **"Spot Apply"**: written as one word, the model runs it together ("spah-dapply").
- **"résumé"**: plain "resume" is read as the verb (re-ZOOM). Captions and
  everything on screen still say "resume" (house rule).

## Where the model and voices come from

Hugging Face is not reachable from the build environment, so both came from
npm (downloaded with `npm pack`; no install scripts run):

| Asset | Package | Check |
|---|---|---|
| Model: `model_quantized.onnx` of onnx-community/Kokoro-82M-v1.0-ONNX, in 6 shards | `kokoro-q8-shards@1.0.0` | joined file sha256 `fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478` (as the package states), 92,361,116 bytes |
| Voice tensors (`voices/af_heart.bin` …) | `kokoro-js@1.2.1` (maintainer: xenova, repo hexgrad/kokoro) | identical bytes to the copies in `kokoro-local-runtime@0.1.0` |

Both are Apache-2.0. Kokoro's output may be used commercially; keep this
attribution with the project.

## Regenerate

```bash
python3 -m venv .venv && .venv/bin/pip install kokoro-onnx soundfile pocketsphinx
npm pack kokoro-q8-shards@1.0.0 kokoro-js@1.2.1    # then untar both
cat package/kokoro-q8.part{0,1,2,3,4,5}.bin > kokoro-q8.onnx   # from kokoro-q8-shards
.venv/bin/python build_voices.py <kokoro-js>/package/voices voices-en.npz
.venv/bin/python synth.py kokoro-q8.onnx voices-en.npz af_heart lines.json raw/
.venv/bin/python synth.py kokoro-q8.onnx voices-en.npz af_heart lines60.json raw/   # the explainer
# loudness: every clip to -16 LUFS, 48 kHz mono, into the project
for f in raw/*.wav; do ffmpeg -y -i $f -af "loudnorm=I=-16:TP=-2:LRA=7" -ar 48000 -ac 1 ../public/voiceover/$(basename $f); done
.venv/bin/python asr_check.py ../public/voiceover/find.wav   # spot-check the words
npm run captions && npm run captions:60
```

To swap the voice, change `af_heart` (e.g. `am_michael`, `bf_emma`) and
regenerate. To use a human recording instead, drop WAVs with the same ids into
`public/voiceover/` and update `frames` in `src/timeline.ts`.
