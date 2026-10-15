// The 60-second vertical reel (Instagram Reels / YouTube Shorts), 30 fps.
// Same flow as the other cuts: the problem, meet SpotApply, find, why it fits,
// tailor, fill + review, then the close.
//
//   0-11.8   the frustration (illustrated): full-time job · 12 tabs · resume
//            rewrites · "Uploaded your resume? Now type it again."
//   11.8     the drop: meet SpotApply
//   14.4-49.6  REAL screen recordings: find (phone) · why + gaps (phone) ·
//            tailor (laptop) · the extension fills, you review + submit
//   49.6-54  less repetitive work, more meaningful applications
//   54-60    "Make your next application count." (lands by 54.8 s, holds)

import placement from "./vo-placement.json";
import words from "./words.json";

export const REEL = { width: 1080, height: 1920, fps: 30, frames: 1800 } as const;

/** [first frame, length] of each scene. */
export const SCENES = {
  hook: [0, 102],
  tabs: [102, 51],
  rewrite: [153, 81],
  retype: [234, 120],
  meet: [354, 78],
  find: [432, 192],
  why: [624, 300],
  tailor: [924, 270],
  fill: [1194, 294],
  less: [1488, 132],
  end: [1620, 180],
} as const satisfies Record<string, readonly [number, number]>;

export type SceneId = keyof typeof SCENES;
export const startOf = (id: SceneId) => SCENES[id][0];
export const lengthOf = (id: SceneId) => SCENES[id][1];

// Voiceover placements (global frames) live in vo-placement.json, so the
// caption file (voiceover/make-srt-reel.mjs) reads the same numbers. Clip
// lengths come from words.json (voiceover/align_reel.py, from the same WAVs).
// r_gaps lands as the missing keywords come into view, r_fill as the form
// fills, r_review so "You click Submit." meets the click.
const AT = placement as Record<string, number>;

type WordsFile = Record<string, { seconds: number; words: { w: string; s: number; e: number }[] }>;
const W = words as WordsFile;

export type VoLine = { id: string; from: number; frames: number };
export const VOICEOVER: VoLine[] = Object.keys(AT).map((id) => ({
  id,
  from: AT[id],
  frames: Math.ceil(W[id].seconds * REEL.fps) + 1,
}));

/** Every caption word with global start/end frames, in order. */
export type CapWord = { w: string; s: number; e: number; line: string };
export const CAPTION_WORDS: CapWord[] = VOICEOVER.flatMap((v) =>
  W[v.id].words.map((x) => ({
    w: x.w,
    s: v.from + Math.round(x.s * REEL.fps),
    e: v.from + Math.round(x.e * REEL.fps),
    line: v.id,
  })),
);

/** Frames during which a voice is speaking (music ducks under these). */
export const speaking = (frame: number) => VOICEOVER.some((v) => frame >= v.from - 4 && frame < v.from + v.frames + 4);

// Sound effects (public/reel/sfx/*.wav): global frame, file, gain.
export const SFX: { at: number; id: string; gain: number }[] = [
  // the frustration
  { at: 20, id: "typing_long", gain: 0.35 },
  { at: 60, id: "typing_short", gain: 0.3 },
  { at: 102, id: "whoosh", gain: 0.35 },
  ...[0, 5, 9, 13, 16, 19, 22, 25, 27, 29, 31, 33].map((d) => ({ at: 108 + d, id: "pop", gain: 0.32 })),
  { at: 153, id: "whoosh", gain: 0.35 },
  ...[0, 9, 17, 24, 31].map((d) => ({ at: 160 + d, id: "pop", gain: 0.28 })),
  { at: 196, id: "typing_short", gain: 0.35 },
  { at: 234, id: "whoosh", gain: 0.35 },
  { at: 262, id: "typing_long", gain: 0.4 },
  { at: 314, id: "thud", gain: 0.55 },
  // meet
  { at: 362, id: "ding", gain: 0.45 },
  // product
  { at: 432, id: "whoosh", gain: 0.3 },
  { at: 627, id: "tap", gain: 0.7 }, // the tap on the Lumen card (footage 4.57 s)
  { at: 845, id: "whoosh", gain: 0.22 },
  { at: 924, id: "whoosh", gain: 0.3 },
  { at: 1194, id: "whoosh", gain: 0.3 },
  { at: 1212, id: "click", gain: 0.7 }, // Auto-Fill on the dashboard (footage 1.90 s)
  { at: 1282, id: "chime", gain: 0.28 }, // the extension reports what it filled
  { at: 1375, id: "typing_answer", gain: 0.5 }, // "Company website", 85 ms a key
  { at: 1466, id: "click", gain: 0.7 }, // Submit, by the person
  { at: 1470, id: "chime", gain: 0.32 },
  { at: 1488, id: "whoosh", gain: 0.3 },
  { at: 1620, id: "whoosh", gain: 0.3 },
];
