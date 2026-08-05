import { API_URL } from "./config";
import { supabase } from "./supabase";
import type {
  Funnel,
  JobsQuery,
  JobsResponse,
  NotificationItem,
  Profile,
  ResumeStatus,
  StatsResponse,
  TargetRoles,
  Usage,
  VerifyResult,
} from "./types";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body — keep the generic message
    }
    if (res.status === 401) detail = "Session expired — please sign in again.";
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export function getJobs(query: JobsQuery = {}): Promise<JobsResponse> {
  return request<JobsResponse>(`/api/jobs${qs({ ...query })}`);
}

export function getStats(): Promise<StatsResponse> {
  return request<StatsResponse>("/api/stats");
}

export function getNotifications(): Promise<{ notifications: NotificationItem[] }> {
  return request("/api/notifications");
}

export function markNotificationRead(id: number): Promise<{ ok: boolean }> {
  return request(`/api/notifications/${id}/read`, { method: "POST" });
}

export function markAllNotificationsRead(): Promise<{ ok: boolean }> {
  return request("/api/notifications/read-all", { method: "POST" });
}

export function getUsage(): Promise<Usage> {
  return request<Usage>("/api/usage");
}

export function getProfile(): Promise<Profile> {
  return request<Profile>("/api/profile");
}

export function getFunnel(): Promise<Funnel> {
  return request<Funnel>("/api/funnel");
}

export function getTargetRoles(): Promise<TargetRoles> {
  return request<TargetRoles>("/api/target-roles");
}

export function updateTargetRoles(roles: string[]): Promise<{ success: boolean; roles: string[] }> {
  return request("/api/target-roles", {
    method: "PUT",
    body: JSON.stringify({ roles }),
  });
}

export function getResumeStatus(): Promise<ResumeStatus> {
  return request<ResumeStatus>("/api/resume/status");
}

export function verifyJob(jobId: number): Promise<VerifyResult> {
  return request<VerifyResult>(`/api/jobs/${jobId}/verify`, { method: "POST" });
}
