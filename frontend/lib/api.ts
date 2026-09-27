/**
 * API client utilities for TerraAgent frontend
 */

import {
  HumanChoice,
  IntentAnalysisResult,
  IntentRequestedChange,
  JobProgress,
  JobResults,
  JobRunsEntry,
  MigrationSafety,
  OperationType,
  WorkspaceDefaults,
  WorkspaceSettings,
} from "./types";

export interface ScanRequestPayload {
  aws_access_key: string;
  aws_secret_key: string;
  aws_session_token?: string;
  role_arn?: string;
  external_id?: string;
  region: string;
  environment?: string;
  user_request?: string;
  analyzed_intent?: IntentAnalysisResult;
  operation: OperationType;
  resource_filters: string[];
  terraform_binary?: "terraform" | "tofu";
  use_resource_explorer?: boolean;
}

export interface ScanResponseData {
  job_id: string;
  status: string;
  created_at: string;
  operation: string;
  region: string;
  message?: string;
}

export type JobProgressData = JobProgress;

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
  github_pr_url?: string | null;
  github_pr_number?: number | null;
  github_hardening_pr_url?: string | null;
  github_hardening_pr_number?: number | null;
  // backend/tools/scores.py::migration_safety, kept on the audit record; null without evidence
  migration_safety_score?: number | null;
  migration_safety_status?: MigrationSafety["status"] | null;
  archived?: boolean;
  // Natural-language request and its parsed changes (Change Requests page)
  user_request?: string | null;
  environment?: string | null;
  requested_changes?: IntentRequestedChange[];
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

export async function fetchJobs(limit = 20, operations: string[] = []): Promise<AuditJobRecord[]> {
  try {
    const params = new URLSearchParams({ limit: String(limit) });
    operations.forEach((op) => params.append("operation", op));
    const res = await fetch(`${API_BASE}/jobs?${params}`, { cache: "no-store", headers: authHeaders() });
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}

// backend/tools/run_summary.py - the terraform commands recent jobs ran.
export async function fetchRuns(limit = 30): Promise<JobRunsEntry[]> {
  const res = await fetch(`${API_BASE}/jobs/runs?limit=${limit}`, { cache: "no-store", headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to load Terraform runs");
  return await res.json();
}

export async function fetchSettings(): Promise<WorkspaceSettings> {
  const res = await fetch(`${API_BASE}/settings`, { cache: "no-store", headers: authHeaders() });
  if (!res.ok) throw new Error("Failed to load settings");
  return await res.json();
}

export async function saveWorkspaceDefaults(defaults: WorkspaceDefaults): Promise<WorkspaceDefaults> {
  const res = await fetch(`${API_BASE}/settings/defaults`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(defaults),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to save settings" }));
    throw new Error(_errorText(err.detail, "Failed to save settings"));
  }
  return (await res.json()).defaults;
}

// Soft delete: the job leaves the default list; its audit record is kept.
export async function archiveJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to archive job" }));
    throw new Error(_errorText(err.detail, "Failed to archive job"));
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

export interface DecisionPayload {
  reason?: string;
  // Approve only: one allowed choice for every resource the gate listed as in Review.
  resource_decisions?: Record<string, HumanChoice>;
  // Optional, approve only: lets a re-verification redo live-AWS checks.
  // Sent for that one resumed run; the backend never stores them.
  aws_access_key?: string;
  aws_secret_key?: string;
  aws_session_token?: string;
}

function _errorText(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  // FastAPI request validation: [{loc, msg, ...}, ...]
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : ""))
      .filter(Boolean)
      .map((m) => m.replace(/^Value error, /, ""));
    return msgs.length ? msgs.join("; ") : fallback;
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    const d = detail as { message: string; missing?: string[]; not_allowed?: string[] };
    const extra = [...(d.missing ?? []), ...(d.not_allowed ?? [])];
    return extra.length ? `${d.message}: ${extra.join(", ")}` : d.message;
  }
  return fallback;
}

async function _postDecision(jobId: string, action: "approve" | "reject", payload: DecisionPayload): Promise<JobDecisionResponse> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ ...payload, reason: payload.reason ?? null }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `Failed to ${action} job` }));
    throw new Error(_errorText(err.detail, `${action} request failed`));
  }

  return await res.json();
}

export async function approveJob(jobId: string, payload: DecisionPayload = {}): Promise<JobDecisionResponse> {
  return _postDecision(jobId, "approve", payload);
}

export async function rejectJob(jobId: string, reason?: string): Promise<JobDecisionResponse> {
  return _postDecision(jobId, "reject", { reason });
}

export interface CreatePullRequestPayload {
  github_token: string;
  repo: string;
  base_branch?: string;
  wave?: number;
  kind?: "adoption" | "hardening";
}

export interface PullRequestResponseData {
  job_id: string;
  pr_url: string;
  pr_number: number;
  pr_title?: string;
  branch: string;
  repo?: string;
  base_branch?: string;
  commit_sha?: string;
  changed_files?: string[];
  status?: string;
  wave?: number | null;
  kind?: "adoption" | "hardening";
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
    throw new Error(_errorText(err.detail, "Pull request creation failed"));
  }

  return await res.json();
}

export async function fetchPullRequestDetails(
  jobId: string,
  kind: import("./types").PrKind,
  githubToken?: string,
): Promise<import("./types").GithubPrDetails> {
  // The token travels only in this dedicated header, for this one request.
  const headers: Record<string, string> = { ...authHeaders() };
  if (githubToken) headers["X-GitHub-Token"] = githubToken;

  const res = await fetch(`${API_BASE}/scan/${jobId}/pull-request?kind=${kind}`, { cache: "no-store", headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to fetch pull request details" }));
    throw new Error(err.detail || "Could not fetch pull request details");
  }
  return await res.json();
}

export async function mergePullRequest(
  jobId: string,
  payload: import("./types").MergePrPayload,
): Promise<import("./types").MergePrResponse> {
  const res = await fetch(`${API_BASE}/scan/${jobId}/pull-request/merge`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Failed to merge pull request" }));
    throw new Error(err.detail || "PR merge failed");
  }

  return await res.json();
}

export async function analyzeIntent(payload: {
  user_request: string;
  region?: string;
  environment?: string;
  resource_filters?: string[];
}): Promise<IntentAnalysisResult> {
  const res = await fetch(`${API_BASE}/scan/analyze-intent`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({
      user_request: payload.user_request,
      region: payload.region || "us-east-1",
      environment: payload.environment || "production",
      resource_filters: payload.resource_filters || ["EC2", "VPC", "S3", "RDS", "IAM", "SG"],
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Intent analysis request failed" }));
    throw new Error(err.detail || "Intent analysis failed");
  }

  return await res.json();
}
