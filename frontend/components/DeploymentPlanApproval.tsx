"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck, Terminal, XCircle } from "lucide-react";
import { approveDeployment, fetchAwsDeployTargets, planDeployment, rejectDeployment } from "@/lib/api";
import type { AwsDeployTarget, DeploymentDetail } from "@/lib/types";
import { SectionHeading } from "./ui";

const ACTION_STYLE: Record<string, string> = {
  create: "text-emerald-700 bg-emerald-50",
  update: "text-amber-700 bg-amber-50",
  replace: "text-rose-700 bg-rose-50",
  destroy: "text-rose-700 bg-rose-50",
  delete: "text-rose-700 bg-rose-50",
};

/** VERIFIED -> PLANNING: pick a registered AWS account and run a read-only terraform plan. */
export function PlanCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [targets, setTargets] = useState<AwsDeployTarget[] | null>(null);
  const [targetId, setTargetId] = useState<string>(dep.target_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAwsDeployTargets().then((items) => {
      if (cancelled) return;
      setTargets(items);
      setTargetId((current) => current || items.find((t) => t.verified_at)?.id || "");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const selected = targets?.find((t) => t.id === targetId);

  const start = async () => {
    if (!targetId) return;
    setBusy(true);
    setError(null);
    try {
      await planDeployment(dep.id, targetId);
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the plan");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-brand-200">
      <SectionHeading
        icon={Terminal}
        title="Plan against your AWS account"
        description="Runs terraform plan with the read-only plan role. Nothing is created until you approve and deploy."
      />
      {targets === null ? (
        <div className="text-xs text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading AWS accounts…</div>
      ) : targets.length === 0 ? (
        <div className="p-3 rounded-xl bg-amber-50 border border-amber-200 text-xs text-amber-900 space-y-2">
          <p>No AWS account is connected yet.</p>
          <Link href="/deployments/targets" className="btn-secondary py-1.5">Connect an AWS account</Link>
        </div>
      ) : (
        <>
          <div className="space-y-1.5">
            <label htmlFor="target" className="field-label">AWS account</label>
            <select id="target" className="field-input" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
              <option value="">Choose an account…</option>
              {targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} · {t.account_id} · {t.region}{t.verified_at ? "" : " (not verified)"}
                </option>
              ))}
            </select>
            {selected && !selected.verified_at && (
              <p className="text-2xs text-amber-700">
                This account hasn&apos;t been verified. <Link href="/deployments/targets" className="underline">Verify it first</Link>.
              </p>
            )}
            {selected && selected.region !== dep.region && (
              <p className="text-2xs text-amber-700">
                The deployment was set up for {dep.region}, but this account&apos;s region is {selected.region}.
              </p>
            )}
          </div>
          {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
          <button type="button" className="btn-primary" onClick={start} disabled={busy || !selected || !selected.verified_at}>
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Terminal className="w-4 h-4" />}
            {busy ? "Starting plan…" : "Run plan"}
          </button>
        </>
      )}
    </div>
  );
}

/** AWAITING_APPROVAL: review the exact plan, then approve (bound to its hash) or reject. */
export function ApprovalCard({ dep, onDecided }: { dep: DeploymentDetail; onDecided: () => void }) {
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [ackDestructive, setAckDestructive] = useState(false);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const summary = dep.plan_summary;
  const policy = dep.plan_policy;
  const destructive = Boolean(dep.is_destructive || summary?.is_destructive || policy?.is_destructive);
  const policyFailed = policy ? !policy.passed : false;
  const hash = dep.plan_bundle_sha256 ?? "";
  const canApprove = confirm && (!destructive || ackDestructive) && !policyFailed && Boolean(hash);

  const decide = async (action: "approve" | "reject") => {
    setBusy(action);
    setError(null);
    try {
      if (action === "approve") {
        await approveDeployment(dep.id, {
          plan_bundle_sha256: hash,
          confirm: true,
          acknowledge_destructive: destructive ? ackDestructive : false,
          reason: reason.trim() || undefined,
        });
      } else {
        await rejectDeployment(dep.id, { reason: reason.trim() || undefined });
      }
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : `Could not ${action}`);
      setBusy(null);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-amber-200 bg-amber-50/30">
      <SectionHeading icon={ShieldCheck} title="Review and approve the plan" description="This is exactly what will be created or changed in your AWS account." />

      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
          {(["create", "update", "replace", "destroy"] as const).map((k) => (
            <div key={k} className="p-2.5 rounded-xl bg-white border border-slate-100">
              <div className="text-slate-500 capitalize">{k}</div>
              <div className={`text-lg font-bold ${k === "replace" || k === "destroy" ? (summary.counts[k] ? "text-rose-700" : "") : ""}`}>
                {summary.counts[k] ?? 0}
              </div>
            </div>
          ))}
        </div>
      )}

      {summary && summary.changes.length > 0 && (
        <div className="max-h-64 overflow-y-auto rounded-xl border border-slate-100 bg-white">
          <table className="w-full text-xs">
            <thead className="text-3xs uppercase tracking-wider text-slate-500 bg-slate-50 sticky top-0">
              <tr><th className="text-left px-3 py-2">Action</th><th className="text-left px-3 py-2">Resource</th></tr>
            </thead>
            <tbody>
              {summary.changes.map((c) => (
                <tr key={`${c.address}-${c.action}`} className="border-t border-slate-100">
                  <td className="px-3 py-1.5">
                    <span className={`px-1.5 py-0.5 rounded font-semibold ${ACTION_STYLE[c.action] ?? "text-slate-700 bg-slate-100"}`}>{c.action}</span>
                  </td>
                  <td className="px-3 py-1.5 font-mono break-all">{c.address}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {policy && (
        <div className={`p-3 rounded-xl border text-xs space-y-1 ${policy.passed ? "bg-emerald-50 border-emerald-200 text-emerald-900" : "bg-rose-50 border-rose-200 text-rose-900"}`}>
          <div className="font-semibold flex items-center gap-1.5">
            {policy.passed ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
            {policy.passed ? "Plan policy checks passed" : "Plan policy checks failed. This plan can't be approved."}
          </div>
          {policy.violations.map((v) => <p key={v}>• {v}</p>)}
          {policy.warnings.map((w) => <p key={w} className="text-amber-800">• {w}</p>)}
        </div>
      )}

      {destructive && (
        <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-900 flex gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>This plan replaces or deletes existing resources. Data in them may be lost.</span>
        </div>
      )}

      <div className="text-2xs text-slate-500 break-all">
        Plan fingerprint (SHA-256): <code className="font-mono text-slate-700">{hash || "—"}</code>
      </div>

      <div className="space-y-1.5">
        <label htmlFor="reason" className="field-label">Reason (recorded in the audit trail)</label>
        <input id="reason" className="field-input" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. First production release" />
      </div>
      <label className="flex items-start gap-2 text-xs text-slate-700">
        <input type="checkbox" className="mt-0.5" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
        I reviewed this plan and approve exactly these changes.
      </label>
      {destructive && (
        <label className="flex items-start gap-2 text-xs text-rose-800">
          <input type="checkbox" className="mt-0.5" checked={ackDestructive} onChange={(e) => setAckDestructive(e.target.checked)} />
          I understand this plan replaces or deletes resources.
        </label>
      )}

      {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-primary" onClick={() => decide("approve")} disabled={!canApprove || busy !== null}>
          {busy === "approve" ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
          Approve plan
        </button>
        <button type="button" className="btn-secondary" onClick={() => decide("reject")} disabled={busy !== null}>
          {busy === "reject" ? <Loader2 className="w-4 h-4 animate-spin" /> : <XCircle className="w-4 h-4" />}
          Reject
        </button>
      </div>
      <p className="text-2xs text-slate-500">Approving doesn&apos;t deploy yet. You start the deployment in the next step.</p>
    </div>
  );
}
