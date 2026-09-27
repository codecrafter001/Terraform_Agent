"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, CheckCircle2, Loader2, MinusCircle, ShieldCheck, Terminal, XCircle } from "lucide-react";
import { fetchRuns } from "@/lib/api";
import { JobRunsEntry, RunStage } from "@/lib/types";
import { PageHeader, StatusBadge } from "@/components/ui";

const STAGE_LABEL: Record<RunStage["stage"], string> = {
  validation: "Validation sandbox",
  plan: "Plan with import blocks (live AWS, read-only)",
  generate_config: "Config cross-check",
};

const VERDICT_CLS: Record<string, string> = {
  PASS: "text-emerald-700",
  FAIL: "text-rose-700",
  INCOMPLETE: "text-amber-700",
  NEEDS_APPROVAL: "text-amber-700",
};

function when(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function StageBlock({ stage, engine }: { stage: RunStage; engine: string }) {
  const counts = stage.counts;
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/60 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-slate-800">{STAGE_LABEL[stage.stage]}</span>
        <span className="text-2xs text-slate-500 tabular-nums">
          {stage.seconds !== null && stage.seconds !== undefined ? `${stage.seconds}s` : ""}
        </span>
      </div>
      {stage.commands.length === 0 ? (
        <p className="text-2xs text-slate-500 flex items-center gap-1.5">
          <MinusCircle className="w-3.5 h-3.5" />
          {stage.skipped ? `Skipped${stage.reason ? `: ${stage.reason}` : ""}` : "No command recorded"}
        </p>
      ) : (
        <ul className="space-y-1">
          {stage.commands.map((c, i) => (
            <li key={i} className="flex items-center gap-2 text-2xs font-mono">
              {c.passed ? (
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
              ) : (
                <XCircle className="w-3.5 h-3.5 text-rose-600 shrink-0" />
              )}
              <span className="text-slate-700 break-all">
                {engine} {c.command}
              </span>
            </li>
          ))}
        </ul>
      )}
      {counts && (
        <p className="text-2xs text-slate-600">
          Plan: {counts.imported ?? 0} to import · {counts.create ?? 0} to add · {counts.update ?? 0} to change ·{" "}
          <span className={(counts.replace ?? 0) + (counts.destroy ?? 0) > 0 ? "font-bold text-rose-700" : ""}>
            {counts.replace ?? 0} to replace · {counts.destroy ?? 0} to destroy
          </span>
        </p>
      )}
      {stage.mismatches !== undefined && (
        <p className="text-2xs text-slate-600">{stage.mismatches} attribute mismatch(es) with live AWS</p>
      )}
    </div>
  );
}

export default function TerraformRunsPage() {
  const [entries, setEntries] = useState<JobRunsEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRuns()
      .then((r) => !cancelled && setEntries(r))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : "Failed to load runs"));
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Terraform Runs" }]}
        title="Terraform runs"
        description="Every terraform / tofu command each job ran in its sandbox, with the result of each verification pass."
      />

      <div className="flex items-start gap-2.5 rounded-xl border border-emerald-200 bg-emerald-50/60 px-4 py-3 text-xs text-emerald-900">
        <ShieldCheck className="w-4 h-4 shrink-0 mt-0.5 text-emerald-600" />
        <p>
          Only read-only subcommands ever run (fmt, init, validate, plan, show). apply, destroy and import are blocked
          in the runner itself - your pipeline applies after the PR is reviewed.
        </p>
      </div>

      {error ? (
        <div className="card p-6 text-xs text-rose-700">{error}</div>
      ) : entries === null ? (
        <div className="card p-12 flex items-center justify-center gap-3 text-sm text-slate-400">
          <Loader2 className="w-5 h-5 animate-spin text-brand-600" /> Loading runs...
        </div>
      ) : entries.length === 0 ? (
        <div className="card p-12 text-center space-y-2">
          <Terminal className="w-6 h-6 mx-auto text-slate-400" />
          <p className="text-sm font-semibold text-slate-800">No runs recorded yet</p>
          <p className="text-xs text-slate-500">
            Runs are recorded when a scan finishes or pauses for approval. Jobs from before this was added have none.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {entries.map((e) => (
            <div key={e.job_id} className="card p-5 space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                <div className="flex items-center gap-2.5 flex-wrap">
                  <span className="font-mono text-xs font-semibold text-slate-900">{e.job_id}</span>
                  <StatusBadge status={e.status} />
                  <span className="text-2xs font-mono text-slate-500">{e.runs.engine}</span>
                </div>
                <span className="text-2xs text-slate-500 font-mono">
                  {e.region} · {when(e.completed_at ?? e.created_at)}
                </span>
              </div>

              {e.runs.iterations.length > 0 && (
                <div className="flex items-center gap-2 flex-wrap text-2xs">
                  <span className="font-semibold text-slate-600">Verification passes:</span>
                  {e.runs.iterations.map((it) => (
                    <span
                      key={it.iteration}
                      className={`px-2 py-0.5 rounded-full border border-slate-200 bg-white font-semibold ${VERDICT_CLS[it.verdict] ?? "text-slate-600"}`}
                    >
                      #{it.iteration} {it.verdict}
                    </span>
                  ))}
                  {e.runs.repair_attempts > 0 && (
                    <span className="text-slate-500">
                      · {e.runs.repair_attempts} repair cycle{e.runs.repair_attempts === 1 ? "" : "s"}
                    </span>
                  )}
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                {e.runs.stages.map((s) => (
                  <StageBlock key={s.stage} stage={s} engine={e.runs.engine} />
                ))}
              </div>

              <div className="flex justify-end pt-2 border-t border-slate-100">
                <Link
                  href={e.status === "COMPLETE" ? `/results/${e.job_id}#verification` : `/scan/${e.job_id}`}
                  className="text-xs font-bold text-brand-600 hover:text-brand-700 flex items-center gap-1"
                >
                  {e.status === "COMPLETE" ? "Verification details" : "View progress"}
                  <ArrowRight className="w-3.5 h-3.5" />
                </Link>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
