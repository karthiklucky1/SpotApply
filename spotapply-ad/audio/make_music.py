"""An original, upbeat instrumental for the reel, synthesized from scratch.

    python make_music.py ../public/reel/music.wav

No samples, loops or presets from anywhere: every sound below is built from
sine/saw waves and noise here, so the track is ours outright. 120 BPM, a
vi-IV-I-V loop. The first 12 s (the frustration) are a muted, filtered groove;
the drop lands exactly on the "Meet SpotApply" beat (DROP_AT, video seconds)
and opens the full track; a lighter breakdown sits under the closing line and
the last bar rings out over the end card. Ducking under the voice happens in
the composition (src/reel/audio.ts), not here.
"""
from __future__ import annotations

import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

SR = 48000
BPM = 120
BEAT = 60 / BPM
BAR = 4 * BEAT
LENGTH = 60.0              # video seconds
DROP_AT = 11.8             # video second of the drop (scene "meet")
BREAK_AT = (49.6, 54.0)    # lighter section under "Less time on repetitive work..."
END_HIT = 57.8             # last downbeat: the final chord rings out to 60 s
OFFSET = DROP_AT - 6 * BAR  # video second of bar 0 (the drop is bar 6)

rng = np.random.default_rng(7)
N = int(LENGTH * SR)


def t_of(bar: int, beat: float = 0.0) -> float:
    return OFFSET + (bar * 4 + beat) * BEAT


def place(buf, start_s, sig, gain=1.0):
    i = int(round(start_s * SR))
    if i >= len(buf) or i + len(sig) <= 0:
        return
    a, b = max(0, i), min(len(buf), i + len(sig))
    buf[a:b] += gain * sig[a - i:b - i]


def lp(x, hz, order=2):
    return sosfilt(butter(order, hz, "lowpass", fs=SR, output="sos"), x)


def hp(x, hz, order=2):
    return sosfilt(butter(order, hz, "highpass", fs=SR, output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], "bandpass", fs=SR, output="sos"), x)


def env(n, a=0.002, d=0.3, s=0.0, r=0.05, hold=None):
    """Attack / exponential decay to sustain / release, in seconds."""
    t = np.arange(n) / SR
    e = np.where(t < a, t / max(a, 1e-6), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-6)))
    if hold is not None:
        rel = t > hold
        e[rel] *= np.exp(-(t[rel] - hold) / max(r, 1e-6))
    return e


def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


# ── instruments ──────────────────────────────────────────────────────────────
def kick(level=1.0):
    n = int(0.42 * SR)
    t = np.arange(n) / SR
    f = 46 + 110 * np.exp(-t * 32)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5)
    click = hp(rng.standard_normal(n), 3000) * np.exp(-t * 400) * 0.25
    return level * np.tanh(1.6 * (body + click))


def clap():
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    noise = bp(rng.standard_normal(n), 900, 4200)
    e = np.zeros(n)
    for k, off in enumerate((0.0, 0.011, 0.022)):
        m = t >= off
        e[m] += (0.7 + 0.15 * k) * np.exp(-(t[m] - off) / 0.007)
    m = t >= 0.03
    e[m] += 0.55 * np.exp(-(t[m] - 0.03) / 0.09)
    return noise * e * 0.6


def hat(open_=False):
    n = int((0.16 if open_ else 0.05) * SR)
    t = np.arange(n) / SR
    noise = hp(rng.standard_normal(n), 7500, 3)
    return noise * np.exp(-t / (0.06 if open_ else 0.012)) * (0.35 if open_ else 0.28)


def shaker():
    n = int(0.06 * SR)
    t = np.arange(n) / SR
    return bp(rng.standard_normal(n), 5000, 11000) * np.sin(np.pi * t / t[-1]) ** 2 * 0.12


def ep_note(freq, dur, bright=1.0):
    """Electric-piano-ish FM tone: a bell attack that settles into a sine."""
    n = int((dur + 0.6) * SR)
    t = np.arange(n) / SR
    idx = bright * 1.4 * np.exp(-t * 7)
    tone = np.sin(2 * np.pi * freq * t + idx * np.sin(2 * np.pi * 2 * freq * t))
    tone += 0.18 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * 3)
    return tone * env(n, a=0.003, d=0.55, s=0.12, r=0.12, hold=dur)


def saw(freq, n, detune=0.0):
    t = np.arange(n) / SR
    ph = (freq * (1 + detune) * t) % 1.0
    return 2 * ph - 1


def bass_note(freq, dur):
    n = int((dur + 0.05) * SR)
    sig = saw(freq, n) * 0.6 + np.sin(2 * np.pi * freq * np.arange(n) / SR) * 0.7
    sig = lp(sig, 520)
    return sig * env(n, a=0.004, d=0.22, s=0.55, r=0.03, hold=dur)


def pad_chord(freqs, dur):
    n = int((dur + 0.8) * SR)
    sig = np.zeros(n)
    for f in freqs:
        for dt in (-0.004, 0.0, 0.005):
            sig += saw(f, n, dt)
    sig = lp(sig / (3 * len(freqs)), 1400)
    t = np.arange(n) / SR
    e = np.minimum(1, t / 0.35) * np.where(t > dur, np.exp(-(t - dur) / 0.4), 1.0)
    return sig * e


def pluck(freq, dur=0.18):
    n = int((dur + 0.25) * SR)
    t = np.arange(n) / SR
    tri = 2 * np.abs(2 * ((freq * t) % 1.0) - 1) - 1
    return lp(tri, 3800) * np.exp(-t / 0.11) * 0.5


def riser(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    steps = 24
    for k in range(steps):                     # a band-pass sweeping upward
        a, b = k * n // steps, (k + 1) * n // steps
        c = 400 * (16 ** (k / steps))
        out[a:b] = bp(noise[max(0, a - 2000):b], c * 0.7, min(c * 1.4, 20000))[-(b - a):]
    tone = np.sin(2 * np.pi * np.cumsum(220 * 2 ** (2 * t / dur)) / SR) * 0.15
    return (out * 0.35 + tone) * (t / dur) ** 2


def impact():
    n = int(1.6 * SR)
    t = np.arange(n) / SR
    crash = hp(rng.standard_normal(n), 4000) * np.exp(-t / 0.45) * 0.22
    boom = np.sin(2 * np.pi * np.cumsum(38 + 60 * np.exp(-t * 20)) / SR) * np.exp(-t * 3.5) * 0.8
    return crash + boom


# ── the song ────────────────────────────────────────────────────────────────
# vi - IV - I - V in C (A minor, F, C, G): chord tones (MIDI) and bass roots.
PROG = [([57, 60, 64], 45), ([53, 57, 60], 41), ([48, 52, 55, 60], 36), ([55, 59, 62], 43)]


def build():
    drums, bass, keys, pad, lead, fx = (np.zeros(N) for _ in range(6))
    kick_env = np.zeros(N)
    last_bar = int((LENGTH - OFFSET) / BAR) + 1
    for bar in range(0, last_bar):
        t0 = t_of(bar)
        if t0 >= LENGTH:
            break
        intro = t0 < DROP_AT - 1e-6
        brk = BREAK_AT[0] - 0.01 <= t0 < BREAK_AT[1] - 0.01
        final = abs(t0 - END_HIT) < 0.01
        if t0 > END_HIT + 0.01:
            break
        chord, root = PROG[bar % 4]
        if final:
            chord, root = [48, 52, 55, 60, 64], 36           # resolve to C
            place(keys, t0, sum(ep_note(midi(m), 1.9) for m in chord) / len(chord), 0.55)
            place(pad, t0, pad_chord([midi(m) for m in chord], 2.0), 0.5)
            place(bass, t0, bass_note(midi(root), 1.8), 0.9)
            place(drums, t0, kick(), 0.9)
            place(drums, t0, impact(), 0.5)
            continue
        # drums
        for beat in range(4):
            tb = t0 + beat * BEAT
            if intro:
                if beat in (0, 2):
                    place(drums, tb, kick(0.6))
                    place(kick_env, tb, np.exp(-np.arange(int(0.25 * SR)) / SR / 0.08), 0.6)
            else:
                place(drums, tb, kick(0.95 if not brk else 0.55))
                place(kick_env, tb, np.exp(-np.arange(int(0.25 * SR)) / SR / 0.08))
                if beat in (1, 3) and not brk:
                    place(drums, tb, clap(), 0.9)
            for eighth in (0, 1):
                te = tb + eighth * BEAT / 2
                if intro:
                    if eighth == 1:
                        place(drums, te, hat(), 0.5)
                elif not brk or eighth == 1:
                    place(drums, te, hat(open_=(eighth == 1 and beat == 3)), 0.9 if eighth else 0.6)
            if not intro and not brk:
                for s16 in range(4):
                    place(drums, tb + s16 * BEAT / 4, shaker(), 0.6 + 0.4 * (s16 % 2))
        # bass: root on 1, syncopated 8ths, octave pop on the "and" of 4
        if not intro:
            pattern = [(0, 0.45, 0), (1.5, 0.3, 0), (2, 0.45, 0), (3, 0.25, 0), (3.5, 0.3, 12)]
            for beat, dur, octv in pattern:
                place(bass, t0 + beat * BEAT, bass_note(midi(root + octv), dur * (0.8 if brk else 1)),
                      0.55 if brk else 0.85)
        # keys: off-beat stabs (the bouncy part), a held chord in the intro
        if intro:
            place(keys, t0, sum(ep_note(midi(m), BAR * 0.95, 0.6) for m in chord) / len(chord), 0.5)
        else:
            for beat in range(4):
                place(keys, t0 + (beat + 0.5) * BEAT,
                      sum(ep_note(midi(m + 12), 0.16) for m in chord) / len(chord), 0.42)
        # pad under everything after the drop
        if not intro:
            place(pad, t0, pad_chord([midi(m) for m in chord], BAR), 0.32 if not brk else 0.45)
        # lead arpeggio in the second half of the product section
        if 30.5 <= t0 < BREAK_AT[0] - 0.01:
            tones = [m + 24 for m in chord[:3]] + [chord[1] + 12]
            for s16 in range(16):
                if s16 % 4 == 3:
                    continue
                place(lead, t0 + s16 * BEAT / 4, pluck(midi(tones[s16 % len(tones)])), 0.2)
    # the riser into the drop, and the hit on it
    rz = riser(1.6)
    place(fx, DROP_AT - 1.6, rz, 0.55)
    place(fx, DROP_AT, impact(), 0.55)
    # the intro is muffled (a low-pass on the music bus), then it opens
    intro_n = int(DROP_AT * SR)
    for stem, hz in ((keys, 1100), (drums, 2600)):
        stem[:intro_n] = lp(stem[:intro_n], hz)
    # side-chain pump: the kick ducks bass, pad and keys
    pump = 1 - 0.45 * np.clip(kick_env, 0, 1)
    mix_l = drums + (bass * pump) + 0.9 * (keys * pump) + 1.05 * (pad * pump) + lead + fx
    mix_r = drums + (bass * pump) + 1.1 * (keys * pump) + 0.95 * (pad * pump) + lead + fx
    # a short slap delay on the lead for width
    d = int(0.012 * SR)
    mix_r[d:] += 0.5 * lead[:-d]
    # EQ for a bed under speech: trim the sub and low end, and dip the
    # 1.5-3.5 kHz presence band the voice lives in (about -3 dB each).
    def eq(x):
        x = hp(x, 32)
        x = x - 0.3 * lp(x, 140)
        return x - 0.3 * bp(x, 1500, 3500)
    stereo = np.stack([eq(mix_l), eq(mix_r)], axis=1)
    # gentle bus glue, soft clip, fade the tail out to 60 s
    stereo = np.tanh(stereo * 0.9) / np.tanh(0.9)
    t = np.arange(N) / SR
    fade = np.clip((LENGTH - t) / 1.2, 0, 1)
    stereo *= fade[:, None]
    stereo /= np.max(np.abs(stereo)) / 0.89          # peak -1 dBFS
    return stereo.astype(np.float32)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "music.wav"
    sf.write(out, build(), SR, subtype="PCM_16")
    print("wrote", out)
