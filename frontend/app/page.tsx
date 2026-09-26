"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  Activity,
  Archive,
  ArrowRight,
  Boxes,
  CheckCircle2,
  GitPullRequest,
  Inbox,
  Loader2,
  PauseCircle,
  PlusCircle,
  RefreshCw,
  Search,
  SearchX,
  ShieldCheck,
} from "lucide-react";
import { archiveJob, fetchJobs, AuditJobRecord } from "@/lib/api";
import { PageHeader, StatCard, StatusBadge } from "@/components/ui";

type StatusFilter = "ALL" | "ACTIVE" | "COMPLETE" | "ATTENTION" | "FAILED";

const FILTERS: { id: StatusFilter; label: string; statuses: string[] | null }[] = [
  { id: "ALL", label: "All", statuses: null },
  { id: "ACTIVE", label: "Running", statuses: ["RUNNING", "PENDING"] },
  { id: "ATTENTION", label: "Needs approval", statuses: ["AWAITING_APPROVAL"] },
  { id: "COMPLETE", label: "Complete", statuses: ["COMPLETE"] },
  { id: "FAILED", label: "Failed / rejected", statuses: ["FAILED", "REJECTED"] },
];

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(diff)) return "—";
  const sec = Math.round(diff / 1000);
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function jobHref(job: AuditJobRecord): string {
  return job.status === "COMPLETE" ? `/results/${job.job_id}` : `/scan/${job.job_id}`;
}

const FINISHED = ["COMPLETE", "FAILED", "REJECTED"];

// A completed scan that discovered nothing usually means the wrong region,
// filters or role - worth calling out rather than looking like a success.
function nothingFound(job: AuditJobRecord): boolean {
  return job.status === "COMPLETE" && (job.resources_discovered ?? 0) === 0;
}

function NothingFoundBadge() {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-3xs font-semibold text-slate-600 whitespace-nowrap"
      title="The scan finished but discovered no resources. Check the region, the resource filters and the role's permissions."
    >
      <SearchX className="w-3 h-3" />
      Nothing found
    </span>
  );
}

const SAFETY_TONE: Record<string, string> = {
  SAFE: "text-emerald-700",
  CHANGES: "text-amber-700",
  DESTRUCTIVE: "text-rose-700",
  UNVERIFIED: "text-slate-500",
};

const SAFETY_LABEL: Record<string, string> = {
  SAFE: "no changes on adoption",
  CHANGES: "in-place changes on adoption",
  DESTRUCTIVE: "destroy/replace on adoption",
  UNVERIFIED: "not verified against live AWS",
};

function LatestScanCard({ job }: { job: AuditJobRecord }) {
  const score = job.migration_safety_score;
  const safetyStatus = job.migration_safety_status ?? null;
  const measured = score !== null && score !== undefined;
  return (
    <div className="card p-4 sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 space-y-1.5">
          <div className="text-2xs font-semibold uppercase tracking-wider text-slate-500">Latest scan</div>
          <div className="flex items-center gap-2 flex-wrap">
            <Link href={jobHref(job)} className="font-mono text-sm font-semibold text-slate-900 hover:text-brand-700 truncate">
              {job.job_id}
            </Link>
            <StatusBadge status={job.status} />
            {nothingFound(job) && <NothingFoundBadge />}
          </div>
          <div className="text-2xs text-slate-500">
            <span className="capitalize">{job.operation}</span> · <span className="font-mono">{job.region}</span> ·{" "}
            <span title={new Date(job.created_at).toLocaleString()}>{relativeTime(job.created_at)}</span>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 lg:gap-8">
          <div>
            <div className="text-2xs font-semibold uppercase tracking-wider text-slate-500">Resources</div>
            <div className="text-lg font-bold tabular-nums text-slate-900">{job.resources_discovered ?? 0}</div>
          </div>
          <div>
            <div className="text-2xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1">
              <ShieldCheck className="w-3 h-3" /> Migration Safety
            </div>
            <div
              className={`text-lg font-bold tabular-nums ${
                measured ? SAFETY_TONE[safetyStatus ?? ""] ?? "text-slate-900" : "text-slate-400"
              }`}
            >
              {measured ? `${score}%` : "Not measured"}
            </div>
            {safetyStatus && <div className="text-3xs text-slate-500">{SAFETY_LABEL[safetyStatus] ?? safetyStatus}</div>}
          </div>
          <div className="col-span-2 sm:col-span-1">
            <div className="text-2xs font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1">
              <GitPullRequest className="w-3 h-3" /> Pull request
            </div>
            {job.github_pr_url ? (
              <a
                href={job.github_pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm font-semibold text-brand-600 hover:text-brand-700"
              >
                Adoption PR #{job.github_pr_number}
              </a>
            ) : (
              <div className="text-sm text-slate-400">Not opened yet</div>
            )}
            {job.github_hardening_pr_url && (
              <a
                href={job.github_hardening_pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="block text-2xs font-semibold text-brand-600 hover:text-brand-700"
              >
                Hardening PR #{job.github_hardening_pr_number}
              </a>
            )}
          </div>
        </div>

        <Link href={jobHref(job)} className="btn-secondary shrink-0 self-start lg:self-center">
          {jobHref(job).startsWith("/results") ? "Results" : "Progress"}
          <ArrowRight className="w-3.5 h-3.5" />
        </Link>
      </div>
    </div>
  );
}

export default function HomePage() {
  const [jobs, setJobs] = useState<AuditJobRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [filter, setFilter] = useState<StatusFilter>("ALL");
  const [query, setQuery] = useState("");
  const [archiving, setArchiving] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const loadData = useCallback(async () => {
    const jobList = await fetchJobs();
    if (!mountedRef.current) return;
    setJobs(jobList);
    setIsLoading(false);
    setIsRefreshing(false);
  }, []);

  const refresh = async () => {
    setIsRefreshing(true);
    await loadData();
  };

  const archive = async (job: AuditJobRecord) => {
    if (!window.confirm(`Archive ${job.job_id}? It will be hidden from the dashboard. Its audit record, bundle and PR links are kept.`)) {
      return;
    }
    setArchiving(job.job_id);
    setArchiveError(null);
    try {
      await archiveJob(job.job_id);
      await loadData();
    } catch (e) {
      setArchiveError(e instanceof Error ? e.message : "Failed to archive job");
    } finally {
      setArchiving(null);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    loadData();
    const interval = setInterval(loadData, 15000);
    return () => {
      mountedRef.current = false;
      clearInterval(interval);
    };
  }, [loadData]);

  const stats = useMemo(() => {
    const complete = jobs.filter((j) => j.status === "COMPLETE").length;
    const finished = jobs.filter((j) => ["COMPLETE", "FAILED", "REJECTED"].includes(j.status)).length;
    return {
      total: jobs.length,
      running: jobs.filter((j) => j.status === "RUNNING" || j.status === "PENDING").length,
      awaiting: jobs.filter((j) => j.status === "AWAITING_APPROVAL").length,
      successRate: finished > 0 ? Math.round((complete / finished) * 100) : null,
      resources: jobs.reduce((sum, j) => sum + (j.resources_discovered || 0), 0),
    };
  }, [jobs]);

  const filterCounts = useMemo(() => {
    const counts: Record<StatusFilter, number> = { ALL: jobs.length, ACTIVE: 0, ATTENTION: 0, COMPLETE: 0, FAILED: 0 };
    for (const f of FILTERS) {
      if (f.statuses) counts[f.id] = jobs.filter((j) => f.statuses!.includes(j.status)).length;
    }
    return counts;
  }, [jobs]);

  const visibleJobs = useMemo(() => {
    const active = FILTERS.find((f) => f.id === filter);
    const q = query.trim().toLowerCase();
    return jobs.filter((j) => {
      if (active?.statuses && !active.statuses.includes(j.status)) return false;
      if (!q) return true;
      return [j.job_id, j.region, j.operation, j.aws_account_id ?? ""].some((v) => v.toLowerCase().includes(q));
    });
  }, [jobs, filter, query]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        description="Read-only AWS discovery, four agents with a self-correcting generate/verify loop. Nothing in your AWS account is ever changed."
        actions={
          <>
            <button onClick={refresh} className="btn-secondary px-3" title="Refresh" aria-label="Refresh">
              <RefreshCw className={`w-4 h-4 ${isRefreshing ? "animate-spin" : ""}`} />
            </button>
            <Link href="/scan" className="btn-primary">
              <PlusCircle className="w-4 h-4" />
              New scan
            </Link>
          </>
        }
      />

      {!isLoading && jobs.length > 0 && <LatestScanCard job={jobs[0]} />}

      {/* KPI tiles */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
        <StatCard label="Total scans" value={isLoading ? "—" : stats.total} icon={Activity} tone="brand" />
        <StatCard
          label="Success rate"
          value={isLoading || stats.successRate === null ? "—" : `${stats.successRate}%`}
          icon={CheckCircle2}
          tone="emerald"
          hint={stats.running > 0 ? `${stats.running} running now` : "of finished scans"}
        />
        <StatCard
          label="Resources discovered"
          value={isLoading ? "—" : stats.resources.toLocaleString()}
          icon={Boxes}
          tone="indigo"
          hint="across all scans"
        />
        <StatCard
          label="Pending approvals"
          value={isLoading ? "—" : stats.awaiting}
          icon={PauseCircle}
          tone={stats.awaiting > 0 ? "amber" : "slate"}
          hint={stats.awaiting > 0 ? "need a human decision" : "nothing waiting"}
        />
      </div>

      {/* Job History */}
      <div className="card overflow-hidden">
        <div className="card-header flex-col items-stretch sm:flex-row sm:items-center">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Job history</h2>
            <p className="text-2xs text-slate-500 mt-0.5">Refreshes every 15 seconds</p>
          </div>
          <div className="relative sm:w-64">
            <Search className="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search job ID, region, account..."
              className="field-input pl-9 py-2"
            />
          </div>
        </div>

        {archiveError && (
          <div className="px-5 py-2 border-b border-rose-100 bg-rose-50 text-2xs text-rose-700">{archiveError}</div>
        )}

        <div className="px-5 py-2.5 border-b border-slate-100 flex gap-1.5 overflow-x-auto">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`px-3 py-1.5 rounded-lg text-2xs font-semibold whitespace-nowrap transition-colors flex items-center gap-1.5 ${
                filter === f.id ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              {f.label}
              <span
                className={`tabular-nums px-1.5 rounded-md ${
                  filter === f.id ? "bg-white/20" : "bg-slate-100 text-slate-500"
                }`}
              >
                {filterCounts[f.id]}
              </span>
            </button>
          ))}
        </div>

        {isLoading ? (
          <div className="p-12 flex flex-col items-center justify-center text-slate-400 gap-3 text-sm">
            <Loader2 className="w-6 h-6 animate-spin text-brand-600" />
            <span>Loading job history...</span>
          </div>
        ) : jobs.length === 0 ? (
          <div className="p-12 flex flex-col items-center text-center gap-3">
            <div className="p-3 rounded-2xl bg-brand-50 text-brand-600">
              <Inbox className="w-6 h-6" />
            </div>
            <div>
              <p className="text-sm font-semibold text-slate-800">No scans yet</p>
              <p className="text-xs text-slate-500 mt-1">Start a read-only discovery to generate your first Terraform bundle.</p>
            </div>
            <Link href="/scan" className="btn-primary mt-1">
              Launch your first scan <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
        ) : visibleJobs.length === 0 ? (
          <div className="p-10 text-center text-xs text-slate-500">
            No jobs match this filter.{" "}
            <button
              onClick={() => {
                setFilter("ALL");
                setQuery("");
              }}
              className="text-brand-600 font-semibold hover:text-brand-700"
            >
              Clear filters
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-3xs uppercase tracking-wider text-slate-500 bg-slate-50/70 border-b border-slate-100">
                  <th className="px-5 py-3 font-semibold">Job</th>
                  <th className="px-5 py-3 font-semibold">Status</th>
                  <th className="px-5 py-3 font-semibold">Region</th>
                  <th className="px-5 py-3 font-semibold text-right">Resources</th>
                  <th className="px-5 py-3 font-semibold text-right">Findings</th>
                  <th className="px-5 py-3 font-semibold">Created</th>
                  <th className="px-5 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {visibleJobs.map((job) => (
                  <tr key={job.job_id} className="hover:bg-slate-50/80 transition-colors group">
                    <td className="px-5 py-3.5">
                      <Link href={jobHref(job)} className="block">
                        <div className="font-mono text-xs font-semibold text-slate-900 group-hover:text-brand-700 transition-colors">
                          {job.job_id}
                        </div>
                        <div className="text-2xs text-slate-500 capitalize mt-0.5">{job.operation}</div>
                      </Link>
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <StatusBadge status={job.status} />
                        {nothingFound(job) && <NothingFoundBadge />}
                      </div>
                    </td>
                    <td className="px-5 py-3.5 font-mono text-xs text-slate-600">{job.region}</td>
                    <td className="px-5 py-3.5 text-xs text-slate-700 text-right tabular-nums">
                      {job.resources_discovered ?? 0}
                    </td>
                    <td className="px-5 py-3.5 text-xs text-right tabular-nums">
                      <span className={job.security_findings_count > 0 ? "text-amber-700 font-semibold" : "text-slate-400"}>
                        {job.security_findings_count ?? 0}
                      </span>
                    </td>
                    <td
                      className="px-5 py-3.5 text-xs text-slate-500 whitespace-nowrap"
                      title={new Date(job.created_at).toLocaleString()}
                    >
                      {relativeTime(job.created_at)}
                    </td>
                    <td className="px-5 py-3.5 text-right">
                      <div className="inline-flex items-center gap-3">
                        {FINISHED.includes(job.status) && (
                          <button
                            onClick={() => archive(job)}
                            disabled={archiving === job.job_id}
                            className="p-1 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                            title="Archive (hide from the dashboard)"
                            aria-label={`Archive ${job.job_id}`}
                          >
                            {archiving === job.job_id ? (
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                              <Archive className="w-3.5 h-3.5" />
                            )}
                          </button>
                        )}
                        <Link
                          href={jobHref(job)}
                          className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600 hover:text-brand-700 whitespace-nowrap"
                        >
                          {jobHref(job).startsWith("/results") ? "Results" : "Progress"}
                          <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-0.5 transition-transform" />
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
