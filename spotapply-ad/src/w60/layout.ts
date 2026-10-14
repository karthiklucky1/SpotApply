// The 60-second explainer is a DIFFERENT layout from the vertical ad, not a
// stretched copy: widescreen 1920x1080, a left editorial column (chapter
// number, headline, one supporting sentence) beside a product stage that
// holds the real desktop app, with a chapter rail along the bottom.

export const W = { width: 1920, height: 1080, fps: 30 } as const;

// 16:9 title-safe: 120 px sides, ~90 px top, rail + label sit at ~1000.
export const COL = { x: 120, w: 600 } as const;
export const STAGE = { x: 780, y: 172, w: 1020 } as const;
export const WINDOW_BAR = 44;
// The app captures are 2560x1600 (1280x800 @2x): a 16:10 viewport.
export const SRC = { w: 2560, h: 1600 } as const;
export const STAGE_VIEW_H = Math.round((STAGE.w * SRC.h) / SRC.w); // 638
export const RAIL_Y = 1004;

export const TYPE = {
  number: 128, // chapter number, Instrument Serif italic
  eyebrow: 24,
  headline: 78,
  support: 31,
} as const;
