// 60-second explainer: scene lengths in frames at 30 fps (1800 total), sized
// around the voiceover in voiceover/lines60.json.
//   0-5.4 problem · 5.4-11 intro · 11-18.8 find + why · 18.8-24.2 sponsorship
//   24.2-33.6 tailor · 33.6-43.6 fill, review, submit (+ one-click)
//   43.6-50 track · 50-53 recap · 53-60 end card (7 s: lands by 1.3 s, then
//   holds perfectly still for 5.7 s)
export const SCENES60 = {
  problem: 162,
  intro: 168,
  find: 234,
  sponsor: 162,
  tailor: 282,
  apply: 300,
  track: 192,
  recap: 90,
  cta: 210,
} as const;

export type Scene60 = keyof typeof SCENES60;

export const TOTAL60 = Object.values(SCENES60).reduce((a, b) => a + b, 0);

export const START60 = (() => {
  const out = {} as Record<Scene60, number>;
  let at = 0;
  for (const k of Object.keys(SCENES60) as Scene60[]) {
    out[k] = at;
    at += SCENES60[k];
  }
  return out;
})();

// public/voiceover/w_<id>.wav; each starts `lead` frames into its scene.
export const VOICEOVER60 = [
  { id: "problem", lead: 6, frames: 149 },
  { id: "intro", lead: 8, frames: 154 },
  { id: "find", lead: 10, frames: 171 },
  { id: "sponsor", lead: 6, frames: 150 },
  { id: "tailor", lead: 8, frames: 202 },
  { id: "apply", lead: 10, frames: 216 },
  { id: "track", lead: 8, frames: 159 },
  { id: "recap", lead: 4, frames: 86 },
  { id: "cta", lead: 10, frames: 109 },
] as const satisfies readonly { id: Scene60; lead: number; frames: number }[];

// The chapter rail: the global frame at which each step lights. "Understand"
// starts when the match report opens, part-way through the find scene.
export const CHAPTERS = [
  { n: "01", label: "Find", at: START60.find },
  { n: "02", label: "Understand", at: START60.find + 118 },
  { n: "03", label: "Prepare", at: START60.tailor },
  { n: "04", label: "Review", at: START60.apply },
  { n: "05", label: "Track", at: START60.track },
] as const;
