// The ONE fictional candidate, role and application the whole ad follows.
//
// The live scenes (public/live/*.png) are captures of the running app seeded
// with the same story, so these values and the captures must agree:
//   Maya Chen, Backend Engineer, Austin TX; Backend Engineer at Brightwave
//   Logistics, Aug 2020 – Present ("Built FastAPI APIs supporting 50k
//   requests/day", "Ran a Kafka event pipeline for order updates"); Python,
//   FastAPI, Kafka, PostgreSQL; B.S. Computer Science, 2019.
//   Hero role: Senior Backend Engineer, Lumen Data, Remote, fit 94.
// See LIVE_CAPTURES.md for how the captures were made.

export const CANDIDATE = {
  name: "Maya Chen",
  initials: "MC",
  title: "Backend Engineer",
  email: "maya.chen@example.com", // .example is reserved for documentation
  phone: "(555) 014-2290", // 555-01xx is reserved for fiction
  years: 6, // Aug 2020 – Present, as of late 2026
  skills: ["Python", "FastAPI", "Kafka", "PostgreSQL"],
  lookingFor: "Backend roles · Remote, US",
} as const;

export const BASE_RESUME = {
  // The app names tailored files FirstName_LastName_Resume (commit 05e83fe).
  fileName: "Maya_Chen_Resume.pdf",
} as const;

export const JOB = {
  title: "Senior Backend Engineer",
  company: "Lumen Data",
  initial: "L",
} as const;

// The repetitive forms in the hook belong to unrelated fictional companies.
export const HOOK_FORMS = [
  { company: "Northpoint Labs", initial: "N" },
  { company: "Cadence Robotics", initial: "C" },
  { company: "Meridian Freight", initial: "M" },
] as const;
