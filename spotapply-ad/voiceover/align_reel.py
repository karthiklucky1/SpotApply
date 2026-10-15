"""Word timings for the reel's word-by-word captions (offline, pocketsphinx).

    python align_reel.py lines_reel.json WAV_DIR ../src/reel/words.json

Kokoro returns audio only, so each clip is force-aligned against its own text
(pocketsphinx 5 `set_align_text`): the recognizer is told the words and only
finds WHEN each is said, so a mishearing cannot change a caption. Caption
words are what the viewer reads ("SpotApply", "resume"); each maps to the
spoken words it covers ("spot apply", "resume").

If a clip ever fails to align, its words are spread by length across the
clip's speech (the cue stays inside the audio, just less exact) and the
fallback is named in the output.
"""
import json
import re
import subprocess
import sys

from pocketsphinx import Decoder

FRAME_S = 0.01   # pocketsphinx reports 100 frames a second


def spoken(token: str) -> list[str]:
    """The dictionary words a caption token is pronounced as."""
    t = token.lower().replace("…", " ").replace("’", "'")
    t = t.replace("résumé", "resume").replace("spotapply", "spot apply")
    t = re.sub(r"[^a-z0-9' -]", " ", t).replace("-", " ")
    return [w.strip("'") for w in t.split() if w.strip("'")]


def pcm16k(wav: str) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-ac", "1", "-ar", "16000",
                           "-f", "s16le", "-"], capture_output=True, check=True).stdout


def align(raw: bytes, words: list[str]) -> list[tuple[str, float, float]] | None:
    d = Decoder(samprate=16000, bestpath=False)
    try:
        d.set_align_text(" ".join(words))
        d.start_utt()
        d.process_raw(raw, full_utt=True)
        d.end_utt()
        got = [(seg.word, seg.start_frame * FRAME_S, (seg.end_frame + 1) * FRAME_S) for seg in d.seg()]
    except Exception as e:                       # OOV word, no path found, ...
        print(f"    align failed: {e}", file=sys.stderr)
        return None
    got = [g for g in got if g[0] not in ("<s>", "</s>", "<sil>", "[NOISE]", "(NULL)")]
    names = [re.sub(r"\(\d+\)$", "", g[0]) for g in got]
    if names != words:
        print(f"    alignment words differ: {names}", file=sys.stderr)
        return None
    return got


def spread(raw: bytes, tokens: list[str]) -> list[tuple[float, float]]:
    seconds = len(raw) / 2 / 16000
    lens = [max(1, len(t)) for t in tokens]
    total, at, out = sum(lens), 0.08, []
    span = seconds - 0.16
    for n in lens:
        d = span * n / total
        out.append((at, at + d))
        at += d
    return out


def main(lines_path, wav_dir, out_path):
    result = {}
    for line in json.load(open(lines_path)):
        tokens = line["caption"].split()
        per = [spoken(t) for t in tokens]
        words = [w for p in per for w in p]
        raw = pcm16k(f"{wav_dir}/{line['id']}.wav")
        got = align(raw, words)
        cues, method = [], "aligned"
        if got:
            i = 0
            for tok, p in zip(tokens, per):
                if not p:                        # punctuation-only token
                    continue
                segs = got[i:i + len(p)]
                i += len(p)
                cues.append({"w": tok, "s": round(segs[0][1], 3), "e": round(segs[-1][2], 3)})
        else:
            method = "spread"
            for tok, (s, e) in zip(tokens, spread(raw, tokens)):
                cues.append({"w": tok, "s": round(s, 3), "e": round(e, 3)})
        result[line["id"]] = {"method": method, "seconds": round(len(raw) / 2 / 16000, 3), "words": cues}
        print(f"  {line['id']:10} {method:8} " + " ".join(f"{c['w']}@{c['s']:.2f}" for c in cues))
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main(*sys.argv[1:4])
