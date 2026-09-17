/**
 * API client utilities for TerraAgent frontend
 */

import { JobResults } from "./types";

export interface ScanRequestPayload {
  aws_access_key: string;
  aws_secret_key: string;
  aws_session_token?: string;
  region: string;
  operation: "generate" | "scan" | "explain" | "validate";
  resource_filters: string[];
  terraform_binary?: "terraform" | "tofu";
}

export interface ScanResponseData {
  job_id: string;
  status: string;
  created_at: string;
  operation: string;
  region: string;
  message?: string;
}

export interface JobProgressData {
  job_id: string;
  status: "PENDING" | "RUNNING" | "COMPLETE" | "FAILED" | "AWAITING_APPROVAL" | "REJECTED";
  progress_percentage: number;
  current_agent?: string;
  completed_agents: string[];
  created_at: string;
  error?: string;
}

export interface JobDecisionResponse {
  job_id: string;
  status: string;
  message: string;
}

export interface AuditJobRecord {
  job_id: string;
  operation: string;
  region: string;
  aws_account_id?: string;
  resources_discovered: number;
  agents_completed: number;
  status: string;
  validation_passed: boolean;
  security_findings_count: number;
  zip_generated: boolean;
  created_at: string;
  completed_at?: string;
}

// Server Components/SSR run inside the frontend container, where NEXT_PUBLIC_API_URL
// (a browser-facing host:port) is unreachable — the backend must be addressed over the
// Docker network instead. The browser uses NEXT_PUBLIC_API_URL (or the same-origin
// Next.js rewrite proxy) as before.
function resolveApiBase(): string {
  if (typeof window === "undefined") {
    const internal = process.env.BACKEND_API_INTERNAL_URL || "http://127.0.0.1:8000/api/:path*";
    return internal.replace(/\/:path\*$/, "");
  }
  return process.env.NEXT_PUBLIC_API_URL || "/api";
}


const API_BASE = resolveApiBase();

// Only takes effect server-side (SSR/Server Components calling the backend
// directly via BACKEND_API_INTERNAL_URL, bypassing proxy.ts's rewrite
// entirely). In the browser, process.env.TERRAAGENT_API_KEY is always
// undefined here - Next.js only inlines env vars into client bundles when
// they're prefixed NEXT_PUBLIC_, and this deliberately isn't - so this is a
// no-op there. The browser's own /api/* calls get the key from proxy.ts
// instead, added server-side before the request ever left this app.
function authHeaders(): Record<string, string> {
  const apiKey = process.env.TERRAAGENT_API_KEY;
  return apiKey ? { "x-api-key": apiKey } : {};
}

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

export async function fetchJobs(): Promise<AuditJobRecord[]> {
  try {
    const res = await fetch(`${API_BASE}/jobs`, { cache: "no-store", headers: authHeaders() });
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}

export async function initiateScan(payload: ScanRequestPayload): Promise<ScanResponseData> {
  const res = await fetch(`${API_BASE}/scan`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to initiate scan" }));
    throw new Error(err.detail || "Scan request failed");
  }

  return await res.json();
}

export async function fetchJobStatus(jobId: string): Promise<JobProgressData> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/status`, { cache: "no-store", headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch job status");
  return await res.json();
}

export async function fetchJobResults(jobId: string): Promise<JobResults> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/results`, { cache: "no-store", headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to fetch job results");
  return await res.json();
}

async function _postDecision(jobId: string, action: "approve" | "reject", reason?: string): Promise<JobDecisionResponse> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ reason: reason ?? null }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `Failed to ${action} job` }));
    throw new Error(err.detail || `${action} request failed`);
  }

  return await res.json();
}

export async function approveJob(jobId: string, reason?: string): Promise<JobDecisionResponse> {
  return _postDecision(jobId, "approve", reason);
}

export async function rejectJob(jobId: string, reason?: string): Promise<JobDecisionResponse> {
  return _postDecision(jobId, "reject", reason);
}

export interface CreatePullRequestPayload {
  github_token: string;
  repo: string;
  base_branch?: string;
  wave?: number;
}

export interface PullRequestResponseData {
  job_id: string;
  pr_url: string;
  pr_number: number;
  branch: string;
  wave?: number | null;
}

export async function createPullRequest(jobId: string, payload: CreatePullRequestPayload): Promise<PullRequestResponseData> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/pull-request`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to create pull request" }));
    throw new Error(err.detail || "Pull request creation failed");
  }

  return await res.json();
}
