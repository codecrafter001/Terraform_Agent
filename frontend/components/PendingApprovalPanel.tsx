"use client";

import { useState } from "react";
import { ShieldAlert, CheckCircle2, XCircle, GitCommitHorizontal } from "lucide-react";
import { approveJob, rejectJob } from "@/lib/api";
import { ApprovalDecision, PendingApproval, PlanEquivalenceResult } from "@/lib/types";

interface PendingApprovalPanelProps {
  jobId: string;
  pendingApproval: PendingApproval | null | undefined;
  planEquivalenceResults?: PlanEquivalenceResult | null;
  approvalDecision?: ApprovalDecision | null;
  mode: "actionable" | "readonly";
  onDecision?: (decision: "approved" | "rejected") => void;
}

function tierBadge(tier: string) {
  const cls =
    tier === "destructive"
      ? "bg-rose-50 text-rose-700 border-rose-200"
      : tier === "behavior_changing"
      ? "bg-amber-50 text-amber-700 border-amber-200"
      : "bg-slate-50 text-slate-600 border-slate-200";
  return (
    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full uppercase border shadow-2xs ${cls}`}>
      {tier.replace(/_/g, " ")}
    </span>
  );
}

export default function PendingApprovalPanel({
  jobId,
  pendingApproval,
  planEquivalenceResults,
  approvalDecision,
  mode,
  onDecision,
}: PendingApprovalPanelProps) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const findings = pendingApproval?.findings || [];
  if (findings.length === 0 && !approvalDecision) return null;

  const handleDecision = async (decision: "approve" | "reject") => {
    setBusy(decision);
    setError(null);
    try {
      if (decision === "approve") await approveJob(jobId, reason || undefined);
      else await rejectJob(jobId, reason || undefined);
      onDecision?.(decision === "approve" ? "approved" : "rejected");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to submit decision");
    } finally {
      setBusy(null);
    }
  };

  const blockingActions = planEquivalenceResults?.blocking_actions || [];

  return (
    <div className="rounded-2xl border border-amber-300 bg-amber-50/60 shadow-sm overflow-hidden">
      <div className="p-5 border-b border-amber-200/70 bg-amber-100/50 flex items-start gap-3">
        <ShieldAlert className="w-5 h-5 text-amber-700 shrink-0 mt-0.5" />
        <div>
          <h3 className="text-sm font-bold text-amber-900">
            {approvalDecision
              ? `Human ${approvalDecision.decision === "approved" ? "Approved" : "Rejected"} This Configuration`
              : "Human Approval Required"}
          </h3>
          <p className="text-xs text-amber-800/90 mt-0.5">
            {findings.length} finding(s) were flagged as behavior-changing or destructive and were
            never auto-repaired.
            {approvalDecision &&
              ` Decision recorded ${new Date(approvalDecision.decided_at).toLocaleString()}${
                approvalDecision.reason ? `: "${approvalDecision.reason}"` : "."
              }`}
          </p>
        </div>
      </div>

      <div className="p-5 space-y-3">
        {blockingActions.length > 0 && (
          <div className="p-3.5 rounded-xl border border-slate-200 bg-white flex flex-wrap items-center gap-4 text-xs font-medium">
            <span className="font-bold text-slate-800 flex items-center gap-1.5">
              <GitCommitHorizontal className="w-3.5 h-3.5 text-slate-500" />
              terraform plan diff:
            </span>
            <span className="text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
              {planEquivalenceResults?.create ?? 0} create
            </span>
            <span className="text-blue-700 bg-blue-50 px-2 py-0.5 rounded border border-blue-200">
              {planEquivalenceResults?.update ?? 0} update
            </span>
            <span className="text-amber-700 bg-amber-50 px-2 py-0.5 rounded border border-amber-200">
              {planEquivalenceResults?.replace ?? 0} replace
            </span>
            <span className="text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-200">
              {planEquivalenceResults?.destroy ?? 0} destroy
            </span>
          </div>
        )}

        {findings.map((f, i) => (
          <div
            key={i}
            className="p-4 rounded-xl border border-slate-200/90 bg-white flex items-start justify-between gap-4 shadow-2xs"
          >
            <div className="space-y-1.5 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-xs font-bold text-slate-800 bg-slate-100 px-2 py-0.5 rounded border border-slate-200">
                  [{f.tool?.toUpperCase()}] {f.rule_id}
                </span>
                {tierBadge(f.tier)}
              </div>
              <p className="text-xs text-slate-700 leading-relaxed">{f.description}</p>
              {f.resource && (
                <div className="text-[11px] font-mono text-slate-500">
                  Target Resource: <code className="text-brand-700">{f.resource}</code>
                </div>
              )}
            </div>
          </div>
        ))}

        {mode === "actionable" && !approvalDecision && (
          <div className="pt-3 space-y-3 border-t border-amber-200/70">
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Optional note explaining your decision..."
              className="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white resize-none focus:outline-none focus:ring-2 focus:ring-brand-300"
              rows={2}
            />
            {error && <div className="text-xs text-rose-700 font-medium">{error}</div>}
            <div className="flex gap-3">
              <button
                onClick={() => handleDecision("approve")}
                disabled={busy !== null}
                className="flex-1 py-3 rounded-xl bg-emerald-600 hover:bg-emerald-500 disabled:opacity-60 text-white font-bold flex items-center justify-center gap-2 text-xs shadow-sm transition-all"
              >
                <CheckCircle2 className="w-4 h-4" />
                <span>{busy === "approve" ? "Approving..." : "Approve & Continue"}</span>
              </button>
              <button
                onClick={() => handleDecision("reject")}
                disabled={busy !== null}
                className="flex-1 py-3 rounded-xl bg-white hover:bg-rose-50 disabled:opacity-60 text-rose-700 border border-rose-300 font-bold flex items-center justify-center gap-2 text-xs shadow-2xs transition-all"
              >
                <XCircle className="w-4 h-4" />
                <span>{busy === "reject" ? "Rejecting..." : "Reject"}</span>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
