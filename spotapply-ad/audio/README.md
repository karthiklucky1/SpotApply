# Music and sound effects (the reel)

Everything in `public/reel/music.wav` and `public/reel/sfx/` is synthesized
by the two scripts here from sine/saw waves and noise. No samples, loops,
presets or stock audio from anywhere, so there is nothing to license.

| File | Script | What it is |
|---|---|---|
| `public/reel/music.wav` | `make_music.py` | 60 s original instrumental, 120 BPM, vi–IV–I–V in C. A muted, low-passed groove under the frustration; a riser into a drop exactly on "Meet SpotApply" (11.8 s); a lighter breakdown under the closing line (49.6–54 s); the last chord rings out over the end card. EQ'd as a bed: lows trimmed, the 1.5–3.5 kHz voice band dipped. |
| `public/reel/sfx/*.wav` | `make_sfx.py` | Keystrokes (`typing_*`; `typing_answer` uses the recording's own 85 ms per key), tab `pop`, notification `ding`, `whoosh` for cuts, phone `tap`, mouse `click`, success `chime`, a soft `thud` for the deadpan stare. |

```bash
python3 -m venv .venv && .venv/bin/pip install numpy scipy soundfile
.venv/bin/python make_music.py ../public/reel/music.wav
.venv/bin/python make_sfx.py ../public/reel/sfx
```

Placement and levels live in the composition, not the files:
`src/reel/timeline.ts` (`SFX`: frame, file, gain) and `src/reel/Reel60.tsx`
(`musicVolume`: the bed ducks to ~0.1 while anyone speaks and comes up to
~0.34 between lines, 0.42 on the end card).

To use a licensed track instead, replace `music.wav` with a 60 s, 48 kHz
file: the ducking still follows the voice.
