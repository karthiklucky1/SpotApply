// Every word the viewer reads, in one place. House rule: "resume", never
// "résumé", in anything a user sees.
export const COPY = {
  hook: ["Still typing", "it all again?"],
  revealLead: "Meet",
  find: ["Find roles", "that fit."],
  tailor: ["Your experience.", "Clearly presented."],
  fill: ["Less", "retyping."],
  review: ["Review.", "Then submit."],
  track: ["Keep your search", "organized."],
  taglineA: "Your experience.",
  taglineB: "Less busywork.",
  cta: "Start free", // matches the live landing page CTA (landing.html)
  url: "app.spotapply.ai",
  demoLabel: "Demo data · fictional candidate and companies",
} as const;
