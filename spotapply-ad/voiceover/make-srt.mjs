// Writes out/spotapply-social-30s.srt from the SAME timeline the video uses
// (src/timeline.ts) and the caption text in lines.json, so the captions can
// never drift from the audio. Run: node voiceover/make-srt.mjs
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { SCENE_START, VOICEOVER } from "../src/timeline.ts";

const FPS = 30;
const lines = JSON.parse(readFileSync(new URL("./lines.json", import.meta.url), "utf8"));
const caption = Object.fromEntries(lines.map((l) => [l.id, l.caption]));

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
writeFileSync(new URL("../out/spotapply-social-30s.srt", import.meta.url), srt);
console.log(srt);
