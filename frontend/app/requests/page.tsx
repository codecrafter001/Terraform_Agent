"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, FileDiff, Info, Loader2, PlusCircle } from "lucide-react";
import { AuditJobRecord, fetchJobs } from "@/lib/api";
import { PageHeader, StatusBadge } from "@/components/ui";

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function ChangeRequestsPage() {
  const [requests, setRequests] = useState<AuditJobRecord[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchJobs(100, ["modify", "fix"]).then((jobs) => {
      if (!cancelled) setRequests(jobs);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Change Requests" }]}
        title="Change requests"
        description="Natural-language modify and fix requests, with the changes TerraAgent parsed from each."
        actions={
          <Link href="/scan" className="btn-primary">
            <PlusCircle className="w-4 h-4" />
            New change request
          </Link>
        }
      />

      <div className="flex items-start gap-2.5 rounded-xl border border-blue-200 bg-blue-50/60 px-4 py-3 text-xs text-blue-900">
        <Info className="w-4 h-4 shrink-0 mt-0.5 text-blue-600" />
        <p>
          Requests are parsed and recorded, but not applied yet: each run adopts your infrastructure exactly as it is
          (zero changes). Change-request PRs that edit the adopted Terraform are the next phase.
        </p>
      </div>

      {requests === null ? (
        <div className="card p-12 flex items-center justify-center gap-3 text-sm text-slate-400">
          <Loader2 className="w-5 h-5 animate-spin text-brand-600" /> Loading change requests...
        </div>
      ) : requests.length === 0 ? (
        <div className="card p-12 text-center space-y-2">
          <FileDiff className="w-6 h-6 mx-auto text-slate-400" />
          <p className="text-sm font-semibold text-slate-800">No change requests yet</p>
          <p className="text-xs text-slate-500">
            Start a request in Modify or Fix mode from New Request and it will appear here.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {requests.map((req) => {
            const changes = req.requested_changes ?? [];
            return (
              <div key={req.job_id} className="card p-5 space-y-3">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <span className="font-mono text-xs font-semibold text-slate-900">{req.job_id}</span>
                    <StatusBadge status={req.status} />
                    <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">{req.operation}</span>
                  </div>
                  <span className="text-2xs text-slate-500 font-mono">
                    {req.region}
                    {req.environment ? ` · ${req.environment}` : ""} · {when(req.created_at)}
                  </span>
                </div>

                {req.user_request ? (
                  <p className="text-xs text-slate-700 italic bg-slate-50 p-3 rounded-xl border border-slate-100 break-words">
                    &ldquo;{req.user_request}&rdquo;
                  </p>
                ) : (
                  <p className="text-xs text-slate-400">No request text was recorded for this job.</p>
                )}

                {changes.length > 0 ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-3xs uppercase tracking-wider text-slate-500 border-b border-slate-100">
                          <th className="py-2 pr-3 font-semibold">Resource</th>
                          <th className="py-2 pr-3 font-semibold">Attribute</th>
                          <th className="py-2 pr-3 font-semibold">Current</th>
                          <th className="py-2 pr-3 font-semibold">Requested</th>
                          <th className="py-2 font-semibold">Action</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {changes.map((c, i) => (
                          <tr key={i}>
                            <td className="py-2 pr-3 text-slate-800">{c.resource ?? "—"}</td>
                            <td className="py-2 pr-3 font-mono text-slate-600">{c.attribute ?? "—"}</td>
                            <td className="py-2 pr-3 font-mono text-slate-500">{c.current_value ?? "—"}</td>
                            <td className="py-2 pr-3 font-mono font-semibold text-slate-900">{c.target_value ?? "—"}</td>
                            <td className="py-2 text-slate-600">{c.action ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-2xs text-slate-500">No specific attribute changes were parsed from this request.</p>
                )}

                <div className="flex justify-end pt-2 border-t border-slate-100">
                  <Link
                    href={req.status === "COMPLETE" ? `/results/${req.job_id}` : `/scan/${req.job_id}`}
                    className="text-xs font-bold text-brand-600 hover:text-brand-700 flex items-center gap-1"
                  >
                    {req.status === "COMPLETE" ? "View results" : "View progress"}
                    <ArrowRight className="w-3.5 h-3.5" />
                  </Link>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
