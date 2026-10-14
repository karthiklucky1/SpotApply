import sys, subprocess, numpy as np
from pocketsphinx import Decoder
wav = sys.argv[1]
raw = subprocess.run(["ffmpeg","-v","error","-i",wav,"-ac","1","-ar","16000","-f","s16le","-"],capture_output=True,check=True).stdout
d = Decoder(samprate=16000)
d.start_utt(); d.process_raw(raw, full_utt=True); d.end_utt()
print((d.hyp().hypstr if d.hyp() else "").lower())
