"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  CheckCircle2,
  Cloud,
  Loader2,
  PlusCircle,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { checkHealth, fetchJobs, AuditJobRecord } from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  COMPLETE: "bg-emerald-50 text-emerald-700 border-emerald-200/80",
  RUNNING: "bg-blue-50 text-blue-700 border-blue-200/80",
  PENDING: "bg-slate-100 text-slate-600 border-slate-200/80",
  FAILED: "bg-rose-50 text-rose-700 border-rose-200/80",
};

export default function HomePage() {
  const [jobs, setJobs] = useState<AuditJobRecord[]>([]);
  const [isHealthy, setIsHealthy] = useState<boolean | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const mountedRef = useRef(true);

  const loadData = useCallback(async () => {
    const [jobList, healthy] = await Promise.all([fetchJobs(), checkHealth()]);
    if (!mountedRef.current) return;
    setJobs(jobList);
    setIsHealthy(healthy);
    setIsLoading(false);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    loadData();
    const interval = setInterval(loadData, 15000);
    return () => {
      mountedRef.current = false;
      clearInterval(interval);
    };
  }, [loadData]);

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-8">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-slate-900 flex items-center gap-2">
            <Cloud className="w-6 h-6 text-brand-600" />
            TerraAgent Dashboard
          </h1>
          <p className="text-sm text-slate-500">
            Zero-mutation AWS discovery, backed by an 8-agent LangGraph pipeline.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={loadData}
            className="p-2.5 rounded-xl bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 shadow-xs transition-all"
            title="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>

          <div
            className={`inline-flex items-center gap-2 px-3 py-2 rounded-xl border text-xs font-semibold shadow-xs ${
              isHealthy === null
                ? "bg-slate-100 text-slate-500 border-slate-200"
                : isHealthy
                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                : "bg-rose-50 text-rose-700 border-rose-200"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                isHealthy === null ? "bg-slate-400" : isHealthy ? "bg-emerald-500 animate-pulse" : "bg-rose-500"
              }`}
            />
            {isHealthy === null ? "Checking..." : isHealthy ? "API Healthy" : "API Unreachable"}
          </div>

          <Link
            href="/scan"
            className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white font-semibold text-sm shadow-md shadow-brand-500/20 hover:shadow-lg transition-all"
          >
            <PlusCircle className="w-4 h-4" />
            <span>New Scan</span>
          </Link>
        </div>
      </div>

      {/* Safety Banner */}
      <div className="p-4 rounded-xl bg-brand-50/70 border border-brand-200 text-xs text-brand-900 flex items-center gap-3 shadow-xs">
        <ShieldCheck className="w-5 h-5 text-brand-600 shrink-0" />
        <span>
          Read-only discovery only. TerraAgent never runs <code className="bg-brand-100/80 px-1.5 py-0.5 rounded font-mono text-brand-900 font-semibold">terraform apply</code> or{" "}
          <code className="bg-brand-100/80 px-1.5 py-0.5 rounded font-mono text-brand-900 font-semibold">destroy</code>, and never mutates AWS resources.
        </span>
      </div>

      {/* Job History */}
      <div className="rounded-2xl border border-slate-200/90 bg-white shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100 bg-slate-50/70 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-2 h-2 rounded-full bg-brand-600" />
            <h2 className="text-sm font-bold text-slate-900">Job History</h2>
          </div>
          <span className="text-xs text-slate-500 font-medium">
            {jobs.length} {jobs.length === 1 ? "record" : "records"}
          </span>
        </div>

        {isLoading ? (
          <div className="p-12 flex flex-col items-center justify-center text-slate-400 gap-3 text-sm">
            <Loader2 className="w-6 h-6 animate-spin text-brand-600" />
            <span>Loading job history...</span>
          </div>
        ) : jobs.length === 0 ? (
          <div className="p-12 text-center text-sm text-slate-500 space-y-3">
            <p className="font-medium text-slate-600">No scans found yet.</p>
            <Link
              href="/scan"
              className="inline-flex items-center gap-1.5 text-brand-600 hover:text-brand-700 font-semibold"
            >
              Launch your first scan <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500 bg-slate-50/60 border-b border-slate-100">
                  <th className="px-6 py-3.5 font-semibold">Job ID</th>
                  <th className="px-6 py-3.5 font-semibold">Operation</th>
                  <th className="px-6 py-3.5 font-semibold">Region</th>
                  <th className="px-6 py-3.5 font-semibold">Status</th>
                  <th className="px-6 py-3.5 font-semibold">Created</th>
                  <th className="px-6 py-3.5 font-semibold text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {jobs.map((job) => (
                  <tr key={job.job_id} className="hover:bg-blue-50/40 transition-colors group">
                    <td className="px-6 py-4 font-mono text-xs font-medium text-slate-800">
                      <span className="bg-slate-100 group-hover:bg-white transition-colors px-2 py-1 rounded-md border border-slate-200/80 shadow-2xs">
                        {job.job_id}
                      </span>
                    </td>
                    <td className="px-6 py-4 capitalize text-slate-700 font-medium text-xs">{job.operation}</td>
                    <td className="px-6 py-4 font-mono text-xs text-slate-600">
                      <span className="bg-slate-50 px-2 py-0.5 rounded border border-slate-200/60">
                        {job.region}
                      </span>
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[11px] font-semibold ${
                          STATUS_STYLES[job.status] || STATUS_STYLES.PENDING
                        }`}
                      >
                        {job.status === "COMPLETE" && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />}
                        {job.status === "RUNNING" && <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-600" />}
                        {job.status === "FAILED" && <XCircle className="w-3.5 h-3.5 text-rose-600" />}
                        {job.status}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-xs text-slate-500 font-normal">
                      {new Date(job.created_at).toLocaleString(undefined, {
                        month: 'short',
                        day: 'numeric',
                        year: 'numeric',
                        hour: 'numeric',
                        minute: '2-digit',
                        second: '2-digit'
                      })}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <Link
                        href={job.status === "COMPLETE" ? `/results/${job.job_id}` : `/scan/${job.job_id}`}
                        className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600 hover:text-brand-700 hover:bg-brand-50 px-3 py-1.5 rounded-lg border border-transparent hover:border-brand-200 transition-all"
                      >
                        <span>{job.status === "COMPLETE" ? "View Results" : "View Progress"}</span>
                        <ArrowRight className="w-3.5 h-3.5 opacity-70 group-hover:translate-x-0.5 transition-transform" />
                      </Link>
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
