"""Synthesize every voiceover line in lines.json with Kokoro-82M (Apache-2.0).

    python synth.py MODEL.onnx VOICES.npz af_heart lines.json out_dir/ [speed]

`tts` is what the model reads (spelled for pronunciation: "Spot Apply",
"résumé"); `caption` is what a viewer reads. See README.md for where the
model and voices come from and the loudness step that follows.
"""
import json
import sys

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

model, voices, voice, lines_path, outdir = sys.argv[1:6]
# Optional pace for the whole file (the reel reads a touch quicker); a line's
# own "speed" still wins.
default_speed = float(sys.argv[6]) if len(sys.argv) > 6 else 1.0
k = Kokoro(model, voices)
for line in json.load(open(lines_path)):
    audio, sr = k.create(line["tts"], voice=voice, speed=float(line.get("speed", default_speed)), lang="en-us")
    sf.write(f"{outdir}/{line['id']}.wav", audio, sr)
    print(json.dumps({"id": line["id"], "seconds": round(len(audio) / sr, 3), "peak": float(np.abs(audio).max())}))
