// The ONE fictional candidate, role and application the whole ad follows.
// Every scene reads from here, so the name, employer, dates and job can
// never disagree between scenes.
//
// Rule for the tailoring scene: the tailored summary may only use facts that
// already appear in BASE_RESUME below (title, years from the dates, the
// bullets). Employers, dates and education are shown as unchanged.

export const CANDIDATE = {
  name: "Maya Chen",
  initials: "MC",
  title: "Backend Engineer",
  homeCity: "Austin, TX",
  email: "maya.chen@example.com", // .example is reserved for documentation
  phone: "(555) 014-2290", // 555-01xx is reserved for fiction
  years: 6, // = BASE_RESUME.role.dates (2020 – Present), as of 2026
  skills: ["Python", "FastAPI", "Kafka", "PostgreSQL"],
  lookingFor: "Backend roles · Remote, US",
} as const;

export const BASE_RESUME = {
  summaryBefore: "Software engineer who works on APIs and data.",
  // Every claim below is grounded in the role, dates and bullets:
  // "six years" = 2020 – Present; Python/FastAPI = bullet 1 + skills;
  // Kafka event pipeline = bullet 2.
  summaryAfter: [
    { text: "Backend engineer: six years building " },
    { text: "Python", key: true },
    { text: " APIs with " },
    { text: "FastAPI", key: true },
    { text: ", plus a " },
    { text: "Kafka", key: true },
    { text: " event pipeline." },
  ],
  role: {
    title: "Backend Engineer",
    employer: "Brightwave Logistics",
    dates: "2020 – Present",
    bullets: [
      [
        { text: "Built " },
        { text: "FastAPI", key: true },
        { text: " APIs supporting 50k requests/day" },
      ],
      [
        { text: "Ran a " },
        { text: "Kafka", key: true },
        { text: " event pipeline for order updates" },
      ],
    ],
  },
  skillsLine: [
    { text: "Python", key: true },
    { text: " · FastAPI · Kafka · PostgreSQL" },
  ],
  education: "B.S. Computer Science · 2019",
  fileName: "Maya_Chen_Resume.pdf",
} as const;

// The hero role is the top card in the real board screenshot
// (app/static/shots/qualified-board-narrow.png) and the role in the real
// match-report screenshot, so the screenshot and the illustrated scenes
// show the same job.
export const JOB = {
  title: "Senior Backend Engineer",
  company: "Lumen Data",
  initial: "L",
  location: "Remote",
  fit: 94,
  reasons: [
    {
      label: "Skills",
      text: "Python, FastAPI and Kafka",
    },
    {
      label: "Experience",
      text: "6 years, the level this role asks for",
    },
    {
      label: "Location",
      text: "Remote in the US, as you prefer",
    },
  ],
} as const;

// Other fictional roles (also from the real board screenshot) that share the
// tracker with the hero role.
export const OTHER_JOBS = [
  { title: "Backend Engineer, Platform", company: "Harbor Analytics", initial: "H" },
  { title: "Senior Software Engineer, APIs", company: "Vector Health", initial: "V" },
] as const;

// The repetitive forms in the hook belong to unrelated fictional companies.
export const HOOK_FORMS = [
  { company: "Northpoint Labs", initial: "N" },
  { company: "Cadence Robotics", initial: "C" },
  { company: "Meridian Freight", initial: "M" },
] as const;
