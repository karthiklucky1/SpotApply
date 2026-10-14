// Scene lengths in frames at 30fps, sized around the voiceover: each scene
// is long enough for its line plus a breath, and the whole ad is 30.0 s.
//   0-3.2s hook · 3.2-5.6s reveal · 5.6-10s find · 10-14.4s tailor
//   14.4-20.2s fill + review · 20.2-25.2s track · 25.2-30s end card
export const SCENES = {
  hook: 96,
  reveal: 72,
  find: 132,
  tailor: 132,
  apply: 174,
  track: 150,
  cta: 144,
} as const;

export const TOTAL_FRAMES = Object.values(SCENES).reduce((a, b) => a + b, 0);

/** Global start frame of each scene. */
export const SCENE_START = (() => {
  const out = {} as Record<keyof typeof SCENES, number>;
  let at = 0;
  for (const k of Object.keys(SCENES) as (keyof typeof SCENES)[]) {
    out[k] = at;
    at += SCENES[k];
  }
  return out;
})();

// Voiceover lines (public/voiceover/<id>.wav, generated from
// voiceover/lines.json). Each starts `lead` frames into its scene.
// `frames` is the clip length, rounded up, so captions and audio agree.
export const VOICEOVER = [
  { id: "hook", lead: 2, frames: 95 },
  { id: "reveal", lead: 6, frames: 36 },
  { id: "find", lead: 6, frames: 107 },
  { id: "tailor", lead: 6, frames: 111 },
  { id: "apply", lead: 6, frames: 149 },
  { id: "track", lead: 6, frames: 130 },
  { id: "cta", lead: 6, frames: 109 },
] as const satisfies readonly { id: keyof typeof SCENES; lead: number; frames: number }[];
