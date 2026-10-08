// Scene lengths in frames at 30fps. The storyboard's seconds are the
// source of truth; the main composition places scenes back to back.
//   0-3s hook · 3-6s reveal · 6-11s find · 11-16s tailor
//   16-22s fill + review · 22-25s track · 25-30s end card
export const SCENES = {
  hook: 90,
  reveal: 90,
  find: 150,
  tailor: 150,
  apply: 180,
  track: 90,
  cta: 150,
} as const;

export const TOTAL_FRAMES = Object.values(SCENES).reduce((a, b) => a + b, 0);
