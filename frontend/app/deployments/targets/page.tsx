"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Cloud, Copy, Loader2, Plus, ShieldCheck, Trash2, XCircle } from "lucide-react";
import { createAwsDeployTarget, deleteAwsDeployTarget, fetchAwsDeployTargets, verifyAwsDeployTarget } from "@/lib/api";
import { DEPLOY_REGIONS } from "@/lib/deployments";
import type { AwsDeployTarget, AwsDeployTargetVerifyResult } from "@/lib/types";
import { PageHeader, SectionHeading } from "@/components/ui";

// Names created by backend/deploy/bootstrap/cloudformation.yaml
const PLAN_ROLE = "TerraAgentDeployPlan";
const APPLY_ROLE = "TerraAgentDeployApply";
const BOUNDARY_POLICY = "TerraAgentWorkloadBoundary";

interface Draft {
  name: string;
  account_id: string;
  region: string;
  state_bucket: string;
}

function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-slate-100 last:border-0 text-xs">
      <span className="text-slate-500 shrink-0">{label}</span>
      <span className="flex items-center gap-1.5 min-w-0">
        <code className="font-mono text-slate-900 truncate">{value}</code>
        <button
          type="button"
          aria-label={`Copy ${label}`}
          className="p-1 rounded text-slate-400 hover:text-slate-700"
          onClick={() => {
            navigator.clipboard?.writeText(value).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5" />}
        </button>
      </span>
    </div>
  );
}

function TargetCard({ target, onChanged }: { target: AwsDeployTarget; onChanged: () => void }) {
  const [busy, setBusy] = useState<"verify" | "delete" | null>(null);
  const [result, setResult] = useState<AwsDeployTargetVerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const verify = async () => {
    setBusy("verify");
    setError(null);
    try {
      const r = await verifyAwsDeployTarget(target.id);
      setResult(r);
      if (r.verified) onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Verification failed");
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Remove "${target.name}" from TerraAgent? Nothing in AWS is deleted.`)) return;
    setBusy("delete");
    try {
      await deleteAwsDeployTarget(target.id);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove the account");
      setBusy(null);
    }
  };

  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-bold text-slate-900">{target.name}</div>
          <div className="text-2xs text-slate-500">{target.account_id} · {target.region}</div>
        </div>
        {target.verified_at ? (
          <span className="inline-flex items-center gap-1 text-2xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-full px-2 py-0.5">
            <CheckCircle2 className="w-3 h-3" /> Verified
          </span>
        ) : (
          <span className="text-2xs font-semibold text-amber-800 bg-amber-50 border border-amber-200 rounded-full px-2 py-0.5">Not verified</span>
        )}
      </div>

      {!target.verified_at && (
        <div className="p-3 rounded-xl bg-slate-50 border border-slate-100 text-xs text-slate-700 space-y-2">
          <p className="font-semibold">Finish setup in your AWS account</p>
          <ol className="list-decimal ml-4 space-y-1">
            <li>In CloudFormation, create a stack from <code>backend/deploy/bootstrap/cloudformation.yaml</code>.</li>
            <li>Use these parameter values:</li>
          </ol>
          <div className="bg-white rounded-lg border border-slate-100 px-3">
            <CopyValue label="ExternalId" value={target.external_id} />
            <CopyValue label="StateBucketName" value={target.state_bucket} />
            <div className="py-1.5 text-xs text-slate-500">
              TerraAgentPrincipalArn: the IAM role or user whose credentials the TerraAgent workers run with.
            </div>
          </div>
          <p>Then come back and click Verify.</p>
        </div>
      )}

      {result && (
        <div className={`p-3 rounded-xl border text-xs space-y-1 ${result.verified ? "bg-emerald-50 border-emerald-200" : "bg-rose-50 border-rose-200"}`}>
          {([["Plan role", result.plan_role_ok], ["Apply role", result.apply_role_ok], ["State bucket", result.bucket_ok]] as const).map(([label, ok]) => (
            <div key={label} className="flex items-center gap-1.5">
              {ok ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" /> : <XCircle className="w-3.5 h-3.5 text-rose-600" />}
              {label}
            </div>
          ))}
          <p className="text-slate-700 whitespace-pre-wrap">{result.message}</p>
        </div>
      )}
      {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}

      <div className="flex gap-2">
        <button type="button" className="btn-primary py-2" onClick={verify} disabled={busy !== null}>
          {busy === "verify" ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
          Verify
        </button>
        <button type="button" className="btn-secondary py-2" onClick={remove} disabled={busy !== null}>
          <Trash2 className="w-4 h-4" />
          Remove
        </button>
      </div>
    </div>
  );
}

export default function DeployTargetsPage() {
  const [targets, setTargets] = useState<AwsDeployTarget[] | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [draft, setDraft] = useState<Draft>({ name: "", account_id: "", region: "us-east-1", state_bucket: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;
    fetchAwsDeployTargets().then((items) => !cancelled && setTargets(items));
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const accountOk = /^\d{12}$/.test(draft.account_id);
  const bucketOk = /^[a-z0-9.-]{3,63}$/.test(draft.state_bucket);
  const ready = draft.name.trim().length > 0 && accountOk && bucketOk;

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setSaving(true);
    setError(null);
    const acct = draft.account_id;
    try {
      await createAwsDeployTarget({
        name: draft.name.trim(),
        account_id: acct,
        region: draft.region,
        plan_role_arn: `arn:aws:iam::${acct}:role/${PLAN_ROLE}`,
        apply_role_arn: `arn:aws:iam::${acct}:role/${APPLY_ROLE}`,
        permissions_boundary_arn: `arn:aws:iam::${acct}:policy/${BOUNDARY_POLICY}`,
        state_bucket: draft.state_bucket,
      });
      setDraft({ name: "", account_id: "", region: draft.region, state_bucket: "" });
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the account");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <PageHeader
        breadcrumbs={[{ label: "Deployments", href: "/deployments" }, { label: "AWS accounts" }]}
        title="AWS accounts"
        description="Connect the AWS account TerraAgent deploys into. TerraAgent never stores AWS keys: it assumes two roles you create, one read-only for planning and one limited to TerraAgent's own resources for deploying."
      />

      <form onSubmit={add} className="card p-5 space-y-4">
        <SectionHeading icon={Plus} title="Connect an AWS account" description="Step 1 of 2: register it here to get your ExternalId." />
        <div className="grid sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label htmlFor="t-name" className="field-label">Name</label>
            <input id="t-name" className="field-input" placeholder="Production" maxLength={100}
              value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="t-acct" className="field-label">AWS account ID</label>
            <input id="t-acct" className="field-input font-mono" placeholder="123456789012" inputMode="numeric" maxLength={12}
              value={draft.account_id} onChange={(e) => setDraft({ ...draft, account_id: e.target.value.replace(/\D/g, "") })} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="t-region" className="field-label">Region</label>
            <select id="t-region" className="field-input" value={draft.region} onChange={(e) => setDraft({ ...draft, region: e.target.value })}>
              {DEPLOY_REGIONS.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="t-bucket" className="field-label">State bucket name (new, globally unique)</label>
            <input id="t-bucket" className="field-input font-mono" placeholder="acme-terraagent-state" maxLength={63}
              value={draft.state_bucket} onChange={(e) => setDraft({ ...draft, state_bucket: e.target.value.toLowerCase() })} />
          </div>
        </div>
        {draft.account_id && accountOk && (
          <p className="text-2xs text-slate-500">
            Uses the roles the setup template creates: <code>{PLAN_ROLE}</code>, <code>{APPLY_ROLE}</code> and the{" "}
            <code>{BOUNDARY_POLICY}</code> permissions boundary.
          </p>
        )}
        {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
        <button type="submit" className="btn-primary" disabled={!ready || saving}>
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
          Add account
        </button>
      </form>

      {targets === null ? (
        <div className="card p-6 text-xs text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
      ) : targets.length === 0 ? (
        <div className="card p-8 text-center text-xs text-slate-500 space-y-2">
          <Cloud className="w-6 h-6 mx-auto text-slate-400" />
          <p>No AWS accounts connected yet.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {targets.map((t) => <TargetCard key={`${t.id}-${t.verified_at}`} target={t} onChanged={reload} />)}
        </div>
      )}
    </div>
  );
}
