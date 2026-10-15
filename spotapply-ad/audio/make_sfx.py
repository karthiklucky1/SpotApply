"""Subtle sound effects for the reel, synthesized from scratch (no samples).

    python make_sfx.py ../public/reel/sfx/

Keystrokes, tab pops, a notification ding, a whoosh for cuts, a phone tap, a
mouse click, a success chime and a soft thud. The typing runs are built with
the same key spacing as the recordings they sit under (85 ms per key for the
"Company website" answer, record_extension.py), so each key lands on a letter.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

SR = 48000
rng = np.random.default_rng(11)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], "bandpass", fs=SR, output="sos"), x)


def lp(x, hz, order=2):
    return sosfilt(butter(order, hz, "lowpass", fs=SR, output="sos"), x)


def t_(sec):
    return np.arange(int(sec * SR)) / SR


def norm(x, peak_db=-6.0):
    return (x / (np.max(np.abs(x)) + 1e-9) * 10 ** (peak_db / 20)).astype(np.float32)


def key(var=0):
    """One laptop keystroke: a bright tick plus a small low 'thock'."""
    t = t_(0.07)
    tick = bp(rng.standard_normal(len(t)), 1800 + 300 * var, 6500) * np.exp(-t / 0.006)
    thock = np.sin(2 * np.pi * (160 + 25 * var) * t) * np.exp(-t / 0.012) * 0.6
    return tick + thock


def typing(times):
    out = np.zeros(int((times[-1] + 0.1) * SR))
    for i, at in enumerate(times):
        k = key(rng.integers(0, 4)) * rng.uniform(0.7, 1.0)
        a = int(at * SR)
        out[a:a + len(k)] += k[: len(out) - a]
    return out


def pop():
    t = t_(0.09)
    f = 520 + 900 * np.exp(-t * 60)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.025)


def bell(freq, dur=0.9, idx=2.0):
    t = t_(dur)
    return np.sin(2 * np.pi * freq * t + idx * np.exp(-t * 6) * np.sin(2 * np.pi * freq * 3.5 * t)) * np.exp(-t / 0.28)


def ding():
    out = np.zeros(int(1.2 * SR))
    for at, f in ((0.0, 1318.5), (0.12, 1760.0)):
        b = bell(f)
        a = int(at * SR)
        out[a:a + len(b)] += b[: len(out) - a]
    return out


def whoosh(dur=0.38):
    t = t_(dur)
    noise = rng.standard_normal(len(t))
    out = np.zeros_like(noise)
    seg = 12
    for k in range(seg):
        a, b = k * len(t) // seg, (k + 1) * len(t) // seg
        c = 500 * (6 ** (k / seg))
        out[a:b] = bp(noise[max(0, a - 1500):b], c * 0.6, c * 1.6)[-(b - a):]
    return out * np.sin(np.pi * t / dur) ** 1.5


def tap():
    t = t_(0.06)
    return bp(rng.standard_normal(len(t)), 900, 4000) * np.exp(-t / 0.004) + \
        np.sin(2 * np.pi * 120 * t) * np.exp(-t / 0.015) * 0.8


def click():
    out = np.zeros(int(0.12 * SR))
    for at, g in ((0.0, 1.0), (0.065, 0.6)):
        t = t_(0.03)
        c = bp(rng.standard_normal(len(t)), 2500, 7000) * np.exp(-t / 0.002) * g
        a = int(at * SR)
        out[a:a + len(c)] += c
    return out


def chime():
    out = np.zeros(int(1.4 * SR))
    for at, f in ((0.0, 1046.5), (0.08, 1318.5), (0.16, 1568.0)):
        b = bell(f, 1.2, 1.2)
        a = int(at * SR)
        out[a:a + len(b)] += b[: len(out) - a]
    return out


def thud():
    t = t_(0.5)
    body = np.sin(2 * np.pi * np.cumsum(55 + 40 * np.exp(-t * 18)) / SR) * np.exp(-t / 0.12)
    return body + lp(rng.standard_normal(len(t)), 400) * np.exp(-t / 0.03) * 0.3


def make(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    jitter = lambda n, gap, spread: np.cumsum([0] + list(np.clip(rng.normal(gap, spread, n - 1), 0.05, None)))
    sounds = {
        "typing_short": typing(jitter(9, 0.085, 0.02)),
        "typing_long": typing(jitter(18, 0.09, 0.025)),
        "typing_answer": typing(np.arange(15) * 0.085),     # "Company website", 85 ms a key
        "pop": pop(),
        "ding": ding(),
        "whoosh": whoosh(),
        "tap": tap(),
        "click": click(),
        "chime": chime(),
        "thud": thud(),
    }
    for name, x in sounds.items():
        sf.write(os.path.join(out_dir, f"{name}.wav"), norm(x), SR, subtype="PCM_16")
        print(f"  {name}: {len(x) / SR:.2f}s")


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "sfx")
