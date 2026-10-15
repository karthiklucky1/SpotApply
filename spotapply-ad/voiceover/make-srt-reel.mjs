// The reel's SRT, from the forced-aligned word timings the captions use
// (src/reel/words.json) placed where the composition places each line
// (src/reel/vo-placement.json): a cue starts on its first word.
//   node voiceover/make-srt-reel.mjs -> out/spotapply-reel-60s.srt
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const FPS = 30;
const read = (p) => JSON.parse(readFileSync(new URL(p, import.meta.url), "utf8"));
const words = read("../src/reel/words.json");
const at = read("../src/reel/vo-placement.json");

const ts = (sec) => {
  const ms = Math.round(sec * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
};
// Two rows of at most 42 characters, split where they come out most even.
const wrap = (text) => {
  if (text.length <= 42) return [text];
  const w = text.split(" ");
  let best = null;
  for (let i = 1; i < w.length; i++) {
    const a = w.slice(0, i).join(" "), b = w.slice(i).join(" ");
    const worst = Math.max(a.length, b.length);
    if (a.length <= 42 && b.length <= 42 && (!best || worst < best[2])) best = [a, b, worst];
  }
  return best ? best.slice(0, 2) : [text];
};

const cues = [];
for (const [id, frame] of Object.entries(at)) {
  const t0 = frame / FPS;
  let cur = [];
  const flush = () => {
    if (!cur.length) return;
    cues.push({ from: t0 + cur[0].s, to: t0 + cur[cur.length - 1].e + 0.25, text: cur.map((x) => x.w).join(" ") });
    cur = [];
  };
  for (const w of words[id].words) {
    cur.push(w);
    const text = cur.map((x) => x.w).join(" ");
    if (/[.?!]$/.test(w.w) || text.length > 70) flush();
  }
  flush();
}
cues.sort((a, b) => a.from - b.from);
for (let i = 0; i + 1 < cues.length; i++) cues[i].to = Math.min(cues[i].to, cues[i + 1].from - 0.02);
const srt = cues.map((c, i) => `${i + 1}\n${ts(c.from)} --> ${ts(c.to)}\n${wrap(c.text).join("\n")}\n`).join("\n");
mkdirSync(new URL("../out/", import.meta.url), { recursive: true });
writeFileSync(new URL("../out/spotapply-reel-60s.srt", import.meta.url), srt);
console.log(srt);
