// Brand tokens, mirrored from app/templates/landing.html (:root) so the ad
// and the live landing page cannot drift apart silently. If the site's
// palette changes, change it here — every scene reads from this file.
export const COLORS = {
  canvas: "#FFFFFF",
  surface: "#F5FAFF",
  surface2: "#EAF4FD",
  ink: "#0C2A3E",
  inkSoft: "#21455C",
  muted: "#5B7186",
  border: "#DCEAF6",
  borderStrong: "#BBD4E8",
  accent: "#0077C2",
  accent700: "#0369A1",
  accentSky: "#0EA5E9", // the logo gradient's light end (favicon.svg)
  accentTint: "#E0F2FE",
  accentLine: "#BAE0F8",
  ok: "#047857",
  okBright: "#10B981", // the score circle in the real match report
  okTint: "#ECFDF5",
  okBorder: "#A7F3D0",
} as const;

export const FONTS = {
  sans: "'Outfit', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
  // The landing page uses Instrument Serif italic once, for emphasis. The ad
  // does the same: only on the end-card tagline.
  serif: "'Instrument Serif', Georgia, 'Times New Roman', serif",
  mono: "ui-monospace, SFMono-Regular, Menlo, monospace",
} as const;

export const VIDEO = {
  width: 1080,
  height: 1920,
  fps: 30,
} as const;

// Vertical social safe area: essential content stays 120px from the sides,
// 220px from the top and 320px from the bottom (platform UI overlays).
export const SAFE = {
  x: 120,
  top: 220,
  bottom: 320,
  width: VIDEO.width - 2 * 120, // 840
} as const;

// Shared vertical rhythm, so every scene's headline, context chip and
// product stage sit in the same place and the eye never has to hunt.
export const LAYOUT = {
  headlineTop: 236,
  chipTop: 496,
  chipHeight: 76,
  stageTop: 604,
  stageBottom: 1520,
  labelTop: 1548,
} as const;

export const SHADOW = {
  card: "0 48px 96px -48px rgba(12,42,62,0.30), 0 6px 20px rgba(12,42,62,0.06)",
  soft: "0 24px 48px -24px rgba(12,42,62,0.22), 0 2px 8px rgba(12,42,62,0.05)",
  lift: "0 64px 120px -48px rgba(12,42,62,0.38), 0 10px 28px rgba(12,42,62,0.10)",
} as const;

export const EASE = {
  // Expo-out: fast start, long settle. Used for every entrance.
  out: [0.16, 1, 0.3, 1] as const,
  // Symmetric for moves between two resting positions.
  inOut: [0.65, 0, 0.35, 1] as const,
  // Exits accelerate away.
  in: [0.7, 0, 0.84, 0] as const,
};
