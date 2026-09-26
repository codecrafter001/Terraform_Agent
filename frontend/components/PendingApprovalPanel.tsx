"use client";

import { useState } from "react";
import { ShieldAlert, CheckCircle2, XCircle, GitCommitHorizontal, KeyRound, HelpCircle } from "lucide-react";
import { approveJob, rejectJob } from "@/lib/api";
import {
  ApprovalDecision,
  ApprovalRequest,
  HumanChoice,
  PendingApproval,
  PlanEquivalenceResult,
} from "@/lib/types";

interface PendingApprovalPanelProps {
  jobId: string;
  pendingApproval: PendingApproval | null | undefined;
  approvalRequest?: ApprovalRequest | null;
  planEquivalenceResults?: PlanEquivalenceResult | null;
  approvalDecision?: ApprovalDecision | null;
  mode: "actionable" | "readonly";
  onDecision?: (decision: "approved" | "rejected") => void;
}

const CHOICE_LABEL: Record<HumanChoice, { label: string; hint: string }> = {
  manage: { label: "Manage", hint: "resource + import block" },
  reference: { label: "Reference", hint: "data block, owned elsewhere" },
  exclude: { label: "Exclude", hint: "left out of the code" },
};

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
  approvalRequest,
  planEquivalenceResults,
  approvalDecision,
  mode,
  onDecision,
}: PendingApprovalPanelProps) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [choices, setChoices] = useState<Record<string, HumanChoice>>({});
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");

  const actionable = mode === "actionable" && !approvalDecision;
  // While paused, show exactly what the gate is waiting on; afterwards, the audit trail.
  const findings = (actionable ? approvalRequest?.findings : null) ?? pendingApproval?.findings ?? [];
  const review = actionable ? approvalRequest?.review_resources ?? [] : [];
  const decided = approvalDecision?.resource_decisions ?? {};
  if (findings.length === 0 && review.length === 0 && !approvalDecision) return null;

  const allDecided = review.every((r) => choices[r.resource_id]);
  const addsCode = Object.values(choices).some((c) => c === "manage" || c === "reference");

  const handleDecision = async (decision: "approve" | "reject") => {
    setBusy(decision);
    setError(null);
    try {
      if (decision === "approve") {
        await approveJob(jobId, {
          reason: reason || undefined,
          resource_decisions: choices,
          ...(addsCode && accessKey && secretKey
            ? { aws_access_key: accessKey, aws_secret_key: secretKey, aws_session_token: sessionToken || undefined }
            : {}),
        });
      } else {
        await rejectJob(jobId, reason || undefined);
      }
      setAccessKey("");
      setSecretKey("");
      setSessionToken("");
      onDecision?.(decision === "approve" ? "approved" : "rejected");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to submit decision");
    } finally {
      setBusy(null);
    }
  };

  const blockingActions = planEquivalenceResults?.blocking_actions || [];
  const summary = [
    findings.length > 0 && `${findings.length} behavior-changing or destructive finding(s), never auto-repaired`,
    review.length > 0 && `${review.length} resource(s) with unclear ownership in Review`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="rounded-2xl border border-amber-300 bg-amber-50/60 shadow-sm overflow-hidden">
      <div className="p-5 border-b border-amber-200/70 bg-amber-100/50 flex items-start gap-3">
        <ShieldAlert className="w-5 h-5 text-amber-700 shrink-0 mt-0.5" />
        <div>
          <h3 className="text-sm font-bold text-amber-900">
            {approvalDecision
              ? `Human ${approvalDecision.decision === "approved" ? "Approved" : "Rejected"} This Configuration`
              : "Human Decision Required"}
          </h3>
          <p className="text-xs text-amber-800/90 mt-0.5">
            {summary || "The risk gate recorded a decision."}
            {approvalDecision &&
              ` Decision recorded ${new Date(approvalDecision.decided_at).toLocaleString()}${
                approvalDecision.reason ? `: "${approvalDecision.reason}"` : "."
              }`}
          </p>
          <p className="text-2xs text-amber-800/80 mt-1">
            Approving never runs terraform apply or import - it only lets TerraAgent finish the bundle and PR.
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
            key={`f-${i}`}
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

        {review.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-xs font-bold text-slate-800 flex items-center gap-1.5 pt-1">
              <HelpCircle className="w-3.5 h-3.5 text-amber-600" />
              Resources in Review - decide each one
            </h4>
            {review.map((r) => (
              <div key={r.resource_id} className="p-4 rounded-xl border border-slate-200/90 bg-white space-y-2 shadow-2xs">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-xs font-bold text-slate-800">{r.resource_id}</span>
                  <span className="text-2xs text-slate-500 font-mono">{r.resource_type}</span>
                </div>
                {r.reasons.length > 0 && <p className="text-xs text-slate-600">{r.reasons.join("; ")}</p>}
                <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={`Decision for ${r.resource_id}`}>
                  {r.choices.map((c) => {
                    const selected = choices[r.resource_id] === c;
                    return (
                      <button
                        key={c}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        onClick={() => setChoices((prev) => ({ ...prev, [r.resource_id]: c }))}
                        className={`px-3 py-1.5 rounded-lg border text-2xs font-semibold transition-colors ${
                          selected
                            ? "bg-brand-600 text-white border-brand-600"
                            : "bg-white text-slate-700 border-slate-200 hover:border-brand-300"
                        }`}
                      >
                        {CHOICE_LABEL[c].label}
                        <span className={`ml-1.5 font-normal ${selected ? "text-white/80" : "text-slate-400"}`}>
                          {CHOICE_LABEL[c].hint}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}

        {!actionable && Object.keys(decided).length > 0 && (
          <div className="p-3.5 rounded-xl border border-slate-200 bg-white text-xs text-slate-700 space-y-1">
            <div className="font-bold text-slate-800">Review decisions</div>
            {Object.entries(decided).map(([rid, c]) => (
              <div key={rid} className="font-mono text-2xs">
                {rid} → <span className="font-bold">{c}</span>
              </div>
            ))}
          </div>
        )}

        {actionable && (
          <div className="pt-3 space-y-3 border-t border-amber-200/70">
            {addsCode && (
              <details className="rounded-xl border border-slate-200 bg-white p-3 text-xs">
                <summary className="cursor-pointer font-semibold text-slate-700 flex items-center gap-1.5">
                  <KeyRound className="w-3.5 h-3.5 text-slate-500" />
                  Optional: re-enter AWS credentials for re-verification
                </summary>
                <p className="text-2xs text-slate-500 mt-2">
                  Your decisions add code, so it is verified again. Credentials are never stored, so without them the
                  drift and plan checks against live AWS can&apos;t be redone and the result is marked not fully
                  verified. Used for this one run only.
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mt-2">
                  <input className="field-input" placeholder="Access key ID" autoComplete="off"
                    value={accessKey} onChange={(e) => setAccessKey(e.target.value)} />
                  <input className="field-input" type="password" placeholder="Secret access key" autoComplete="off"
                    value={secretKey} onChange={(e) => setSecretKey(e.target.value)} />
                  <input className="field-input" type="password" placeholder="Session token (optional)" autoComplete="off"
                    value={sessionToken} onChange={(e) => setSessionToken(e.target.value)} />
                </div>
              </details>
            )}
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Optional note explaining your decision..."
              className="w-full text-xs p-3 rounded-xl border border-slate-200 bg-white resize-none focus:outline-none focus:ring-2 focus:ring-brand-300"
              rows={2}
            />
            {error && <div className="text-xs text-rose-700 font-medium">{error}</div>}
            {!allDecided && (
              <div className="text-2xs text-amber-800">Decide every resource in Review to approve.</div>
            )}
            <div className="flex gap-3">
              <button
                onClick={() => handleDecision("approve")}
                disabled={busy !== null || !allDecided}
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
