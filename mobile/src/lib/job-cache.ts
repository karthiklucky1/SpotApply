import type { JobSummary } from "./types";

/**
 * In-memory store of the job summaries the feed screens have already fetched,
 * so the detail screen can render instantly. The backend has no GET
 * /api/jobs/{id} — every list response is cached here instead.
 */
const cache = new Map<number, JobSummary>();

export function rememberJobs(jobs: JobSummary[]): void {
  for (const job of jobs) cache.set(job.id, job);
}

export function getCachedJob(id: number): JobSummary | undefined {
  return cache.get(id);
}

export function updateCachedJob(id: number, patch: Partial<JobSummary>): JobSummary | undefined {
  const existing = cache.get(id);
  if (!existing) return undefined;
  const next = { ...existing, ...patch };
  cache.set(id, next);
  return next;
}
