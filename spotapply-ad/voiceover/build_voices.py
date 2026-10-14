import sys, pathlib, numpy as np
src = pathlib.Path(sys.argv[1]); out = sys.argv[2]
voices = {p.stem: np.fromfile(p, dtype=np.float32).reshape(-1, 1, 256) for p in sorted(src.glob("*.bin")) if p.stem[:2] in ("af","am","bf","bm")}
np.savez(out, **voices); print(len(voices), "voices ->", out, voices["af_heart"].shape)
