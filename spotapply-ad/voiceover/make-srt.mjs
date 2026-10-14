// Writes the SRT for a cut from the SAME timeline the video uses and the
// caption text in its lines file, so captions can never drift from the audio.
//   node voiceover/make-srt.mjs        -> out/spotapply-social-30s.srt
//   node voiceover/make-srt.mjs 60     -> out/spotapply-explainer-60s.srt
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { SCENE_START as START30, VOICEOVER as VO30 } from "../src/timeline.ts";
import { START60, VOICEOVER60 } from "../src/w60/timeline60.ts";

const FPS = 30;
const cut60 = process.argv[2] === "60";
const SCENE_START = cut60 ? START60 : START30;
const VOICEOVER = cut60 ? VOICEOVER60 : VO30;
const OUT = cut60 ? "spotapply-explainer-60s.srt" : "spotapply-social-30s.srt";
const lines = JSON.parse(readFileSync(new URL(cut60 ? "./lines60.json" : "./lines.json", import.meta.url), "utf8"));
// lines60.json ids carry a "w_" prefix (the audio file names).
const caption = Object.fromEntries(lines.map((l) => [l.id.replace(/^w_/, ""), l.caption]));

const ts = (frame) => {
  const ms = Math.round((frame / FPS) * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(Math.floor(ms / 3600000))}:${p(Math.floor(ms / 60000) % 60)}:${p(Math.floor(ms / 1000) % 60)},${p(ms % 1000, 3)}`;
};
// Two lines of at most 42 characters per cue.
const wrap = (text) => {
  const words = text.split(" ");
  const rows = [""];
  for (const w of words) {
    const row = rows[rows.length - 1];
    if ((row + " " + w).trim().length > 42) rows.push(w);
    else rows[rows.length - 1] = (row + " " + w).trim();
  }
  return rows;
};

const cues = [];
for (const v of VOICEOVER) {
  const start = SCENE_START[v.id] + v.lead;
  const end = start + v.frames;
  // Split a line into sentences; time each by its share of the characters.
  const sentences = caption[v.id].match(/[^.?!]+[.?!]*/g).map((s) => s.trim()).filter(Boolean);
  // Keep short sentences together so no cue flashes by.
  const groups = [];
  for (const s of sentences) {
    const last = groups[groups.length - 1];
    if (last && wrap(last + " " + s).length <= 2 && (last + " " + s).length <= 84) groups[groups.length - 1] = last + " " + s;
    else groups.push(s);
  }
  // A sentence too long for two rows splits at the comma or colon nearest
  // its middle.
  for (let i = 0; i < groups.length; i++) {
    const g = groups[i];
    if (wrap(g).length <= 2) continue;
    const marks = [...g.matchAll(/[,:;]\s/g)].map((m) => m.index + 1);
    if (!marks.length) continue;
    const cut = marks.reduce((best, m) => (Math.abs(m - g.length / 2) < Math.abs(best - g.length / 2) ? m : best));
    groups.splice(i, 1, g.slice(0, cut).trim(), g.slice(cut).trim());
  }
  const total = groups.reduce((a, g) => a + g.length, 0);
  let at = start;
  groups.forEach((g, i) => {
    const len = i === groups.length - 1 ? end - at : Math.round(((end - start) * g.length) / total);
    cues.push({ from: at, to: at + len, rows: wrap(g) });
    at += len;
  });
}

const srt = cues.map((c, i) => `${i + 1}\n${ts(c.from)} --> ${ts(c.to)}\n${c.rows.join("\n")}\n`).join("\n");
mkdirSync(new URL("../out/", import.meta.url), { recursive: true });
writeFileSync(new URL(`../out/${OUT}`, import.meta.url), srt);
console.log(srt);
