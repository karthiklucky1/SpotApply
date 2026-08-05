/** Response shapes of the SpotApply backend (app/api/server.py). */

export type ApplicationStatus =
  | "discovered"
  | "matched"
  | "shortlisted"
  | "tailored"
  | "autofilled"
  | "awaiting_user"
  | "ready_to_submit"
  | "submitted"
  | "rejected"
  | "interviewing"
  | "offer"
  | "accepted"
  | "skipped"
  | "error";

export interface ApplicationInfo {
  id: number;
  status: ApplicationStatus;
  apply_track: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface JobSummary {
  id: number;
  source: string;
  company: string;
  title: string;
  location: string | null;
  remote: boolean | null;
  url: string;
  posted: string | null;
  is_new: boolean;
  similarity: number | null;
  /** LLM fit score, 0–100. */
  rerank: number | null;
  /** Hiring-intent probability, 0–1. */
  hire_probability: number | null;
  /** Priority score, 0–100 (0.65*fit + 0.35*hire probability). */
  blended: number | null;
  reason: string | null;
  is_closed: boolean;
  closed_reason: string | null;
  application: ApplicationInfo | null;
}

export interface JobsResponse {
  jobs: JobSummary[];
  total: number;
  total_open: number;
  page: number;
  pages: number;
  limit: number;
}

export interface JobsQuery {
  page?: number;
  limit?: number;
  search?: string;
  status?: ApplicationStatus | "unprocessed";
  min_score?: number;
  sort?: "fresh";
  max_age_days?: number;
  roles_only?: "1";
  hide_aggregators?: "1";
  closed?: "true";
}

export interface NotificationItem {
  id: number;
  title: string;
  message: string;
  type: string | null;
  read: boolean;
  link: string | null;
  created_at: string | null;
}

export interface UsageTrial {
  jobs_used: number;
  jobs_quota: number;
  remaining: number;
  active: boolean;
}

export interface Usage {
  plan: string;
  tailor_used: number;
  tailor_daily_limit: number | null;
  autofill_used_week: number;
  autofill_weekly_limit: number | null;
  week_start?: string;
  trial: UsageTrial | null;
}

export interface Funnel {
  applied: number;
  interviewing: number;
  offers: number;
  accepted: number;
  rejected: number;
  ghosted: number;
  presumed_ghosted: number;
  response_rate: number;
  applied_to_interview: number;
  interview_to_offer: number;
  offer_to_accepted: number;
}

export interface Profile {
  id: number | null;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  location: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
  current_title: string | null;
  years_experience: number | null;
  key_skills: string | null;
  target_roles: string | null;
  professional_summary: string | null;
  preferred_country: string | null;
  remote_ok: boolean | null;
  [extra: string]: unknown;
}

export interface TargetRoles {
  roles: string[];
  suggestions: string[];
  has_resume: boolean;
}

export interface ResumeStatus {
  has_resume: boolean;
}

export interface VerifyResult {
  active: boolean;
  closed_reason?: string;
}

export interface StatsResponse {
  total_jobs: number;
  closed_jobs: number;
  total_companies: number;
  applications: Record<string, number>;
  scores: {
    band_85_100: number;
    band_60_84: number;
    band_40_59: number;
    band_0_39: number;
    unranked: number;
  };
  [extra: string]: unknown;
}
