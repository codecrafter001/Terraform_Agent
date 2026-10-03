"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock,
  Database,
  ExternalLink,
  FileCode2,
  GitMerge,
  GitPullRequest,
  Hammer,
  KeyRound,
  Loader2,
  Lock,
  Rocket,
  RotateCcw,
  Search,
  ShieldCheck,
  Terminal,
  Trash2,
  Upload,
  Wallet,
  XCircle,
  Zap,
} from "lucide-react";
import {
  createDeploymentPullRequest,
  deployDeployment,
  estimateDeployment,
  fetchDeployment,
  fetchDeploymentArtifacts,
  fetchDeploymentLogs,
  fetchDeploymentTerraform,
  JobLogLine,
  mergeDeploymentPullRequest,
  planDestroyDeployment,
  prepareDeployment,
  rollbackDeployment,
  updateDeploymentFromGithub,
  updateDeploymentSource,
} from "@/lib/api";
import { IN_PROGRESS_STATUSES, TARGET_LABELS } from "@/lib/deployments";
import { ApprovalCard, PlanCard } from "./DeploymentPlanApproval";
import type {
  BuildHistoryItem,
  DeploymentDetail,
  DeploymentEstimateResponse,
  DeploymentStatus,
  DeploymentTarget,
  EcsSettings,
  FullstackLayout,
  FullstackPreset,
  FullstackSettings,
  LambdaSettings,
  PrepareDeploymentPayload,
  StaticSiteSettings,
} from "@/lib/types";
import { SectionHeading, StatusBadge } from "./ui";

const POLL_MS = 800;
// backend/deploy/decision_engine.py: the reason codes that make a target eligible.
const POSITIVE_REASONS = new Set([
  "lambda.handler_detected",
  "static.output_present",
  "container.dockerfile_detected",
  "server.container_detected",
  "fullstack.detected",
]);

const STAGES: {
  id: string;
  step: string;
  label: string;
  subtitle: string;
  icon: LucideIcon;
  doneAfter: DeploymentStatus[];
  active: DeploymentStatus[];
}[] = [
  {
    id: "build",
    step: "1",
    label: "Build & Package",
    subtitle: "Analyze & generate Terraform",
    icon: Hammer,
    active: ["SOURCE_RECEIVED", "ANALYZING", "BUILDING", "VERIFYING"],
    doneAfter: ["VERIFIED", "PLANNING", "AWAITING_APPROVAL", "APPROVED", "PR_OPEN", "MERGED", "APPLYING", "DEPLOYED"],
  },
  {
    id: "plan",
    step: "2",
    label: "Plan & Approve",
    subtitle: "Terraform plan & cost estimate",
    icon: Terminal,
    active: ["PLANNING", "AWAITING_APPROVAL"],
    doneAfter: ["APPROVED", "PR_OPEN", "MERGED", "APPLYING", "DEPLOYED"],
  },
  {
    id: "deploy",
    step: "3",
    label: "Deploy to AWS",
    subtitle: "Apply resources & live endpoint",
    icon: Rocket,
    active: ["APPLYING"],
    doneAfter: ["DEPLOYED", "MERGED"],
  },
];

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function StageTracker({ status }: { status: DeploymentStatus }) {
  return (
    <div className="card p-3 sm:p-4 grid grid-cols-1 sm:grid-cols-3 gap-3 shadow-xs">
      {STAGES.map((s) => {
        const done = s.doneAfter.includes(status);
        const active = s.active.includes(status);
        const Icon = done ? CheckCircle2 : active ? Loader2 : s.icon;
        return (
          <div
            key={s.id}
            className={`flex items-center gap-3 p-3 rounded-xl border transition-all ${
              done
                ? "border-emerald-200 bg-emerald-50/60 text-emerald-950"
                : active
                ? "border-brand-500 bg-brand-50/70 text-brand-950 ring-2 ring-brand-400/20 shadow-xs"
                : "border-slate-100 bg-slate-50/50 text-slate-400"
            }`}
          >
            <div
              className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
                done
                  ? "bg-emerald-600 text-white"
                  : active
                  ? "bg-brand-600 text-white shadow-xs"
                  : "bg-slate-200 text-slate-400"
              }`}
            >
              <Icon className={`w-5 h-5 ${active ? "animate-spin" : ""}`} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 font-bold text-xs">
                <span>{s.step}.</span>
                <span className="truncate">{s.label}</span>
                {done && <span className="text-3xs font-semibold text-emerald-700 ml-auto">Done</span>}
                {active && <span className="text-3xs font-semibold text-brand-700 animate-pulse ml-auto">In Progress</span>}
              </div>
              <p className="text-3xs text-slate-500 truncate mt-0.5">{s.subtitle}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 border-b border-slate-100 last:border-0 text-xs">
      <span className="text-slate-500">{label}</span>
      <span className="font-medium text-slate-900 text-right break-all">{children}</span>
    </div>
  );
}

function ConfigureForm({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const eligible = dep.decision?.eligible ?? [];
  const [target, setTarget] = useState<DeploymentTarget | null>(dep.target_type ?? dep.decision?.recommended ?? eligible[0] ?? null);
  const [staticSettings, setStaticSettings] = useState<StaticSiteSettings>({
    price_class: (dep.settings?.price_class as StaticSiteSettings["price_class"]) ?? "PriceClass_100",
    spa_mode: dep.settings?.spa_mode ?? false,
  });
  const [lambdaSettings, setLambdaSettings] = useState<LambdaSettings>({
    memory_mb: dep.settings?.memory_mb ?? 256,
    timeout_s: dep.settings?.timeout_s ?? 30,
    public_url: dep.settings?.public_url ?? true,
  });
  const [ecsSettings, setEcsSettings] = useState<EcsSettings>({
    container_port: dep.settings?.container_port ?? dep.profile?.listens_on_port ?? 8080,
    cpu: dep.settings?.cpu ?? 256,
    memory_mb: dep.settings?.memory_mb ?? 512,
    desired_count: dep.settings?.desired_count ?? 1,
    certificate_arn: dep.settings?.certificate_arn ?? "",
  });
  const layout = dep.profile?.fullstack ?? null;
  const [fullstackSettings, setFullstackSettings] = useState<FullstackSettings>({
    preset: dep.settings?.preset ?? null,
    cdn_enabled: dep.settings?.cdn_enabled ?? true,
    container_port: dep.settings?.container_port ?? layout?.backend.port ?? 8080,
    cpu: dep.settings?.cpu ?? 256,
    memory_mb: dep.settings?.memory_mb ?? 512,
    desired_count: dep.settings?.desired_count ?? 1,
    health_check_path: dep.settings?.health_check_path ?? "/",
    price_class: (dep.settings?.price_class as FullstackSettings["price_class"]) ?? "PriceClass_100",
    database: dep.settings?.database ?? (layout?.database?.rds_supported ? "rds" : "none"),
    db_instance_class: dep.settings?.db_instance_class ?? "db.t4g.micro",
    db_allocated_storage_gb: dep.settings?.db_allocated_storage_gb ?? 20,
    db_multi_az: dep.settings?.db_multi_az ?? false,
    db_backup_retention_days: dep.settings?.db_backup_retention_days ?? 7,
    db_final_snapshot: dep.settings?.db_final_snapshot ?? true,
    run_migrations: dep.settings?.run_migrations ?? true,
    secret_env_keys: dep.settings?.secret_env_keys ?? layout?.env_keys ?? [],
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (eligible.length === 0 || !target) return null;

  const start = async () => {
    setBusy(true);
    setError(null);
    let payload: PrepareDeploymentPayload;
    if (target === "static_site") {
      payload = { target, settings: staticSettings };
    } else if (target === "lambda_http") {
      payload = { target, settings: lambdaSettings };
    } else if (target === "fullstack_app") {
      payload = { target, settings: fullstackSettings };
    } else {
      payload = {
        target: "ecs_service",
        settings: {
          ...ecsSettings,
          certificate_arn: ecsSettings.certificate_arn?.trim() || undefined,
        },
      };
    }
    try {
      await prepareDeployment(dep.id, payload);
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the build");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4">
      <SectionHeading icon={Hammer} title="Target and settings" description="Choose one of the eligible targets, then build and verify." />
      <div className="grid sm:grid-cols-3 gap-2">
        {eligible.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTarget(t)}
            aria-pressed={target === t}
            className={`text-left rounded-xl border px-3 py-2.5 text-xs transition-colors ${
              target === t ? "border-brand-600 bg-brand-50 text-brand-800" : "border-slate-200 hover:bg-slate-50"
            }`}
          >
            <div className="font-semibold">{TARGET_LABELS[t]}</div>
            {dep.decision?.recommended === t && <div className="text-2xs text-brand-700 mt-0.5">Recommended</div>}
          </button>
        ))}
      </div>

      {target === "static_site" ? (
        <div className="grid sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label htmlFor="price" className="field-label">CloudFront price class</label>
            <select id="price" className="field-input" value={staticSettings.price_class}
              onChange={(e) => setStaticSettings({ ...staticSettings, price_class: e.target.value as StaticSiteSettings["price_class"] })}>
              <option value="PriceClass_100">100 · North America &amp; Europe</option>
              <option value="PriceClass_200">200 · + Asia, Middle East, Africa</option>
              <option value="PriceClass_All">All edge locations</option>
            </select>
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-700 sm:mt-6">
            <input type="checkbox" checked={staticSettings.spa_mode}
              onChange={(e) => setStaticSettings({ ...staticSettings, spa_mode: e.target.checked })} />
            Single-page app (serve index.html for unknown paths)
          </label>
        </div>
      ) : target === "lambda_http" ? (
        <div className="grid sm:grid-cols-3 gap-4">
          <div className="space-y-1.5">
            <label htmlFor="mem" className="field-label">Memory (MB)</label>
            <input id="mem" type="number" min={128} max={10240} step={64} className="field-input" value={lambdaSettings.memory_mb}
              onChange={(e) => setLambdaSettings({ ...lambdaSettings, memory_mb: Number(e.target.value) })} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="timeout" className="field-label">Timeout (s)</label>
            <input id="timeout" type="number" min={1} max={900} className="field-input" value={lambdaSettings.timeout_s}
              onChange={(e) => setLambdaSettings({ ...lambdaSettings, timeout_s: Number(e.target.value) })} />
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-700 sm:mt-6">
            <input type="checkbox" checked={lambdaSettings.public_url}
              onChange={(e) => setLambdaSettings({ ...lambdaSettings, public_url: e.target.checked })} />
            Public URL (unchecked: callers must sign with IAM)
          </label>
        </div>
      ) : target === "fullstack_app" ? (
        <FullstackSettingsForm depId={dep.id} applySuggestedPreset={!dep.settings} layout={layout}
          settings={fullstackSettings} onChange={setFullstackSettings} />
      ) : (
        <div className="space-y-4">
          <div className="grid sm:grid-cols-4 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="port" className="field-label">Container Port</label>
              <input id="port" type="number" min={1} max={65535} className="field-input" value={ecsSettings.container_port}
                onChange={(e) => setEcsSettings({ ...ecsSettings, container_port: Number(e.target.value) })} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor="cpu" className="field-label">CPU Units</label>
              <select id="cpu" className="field-input" value={ecsSettings.cpu}
                onChange={(e) => setEcsSettings({ ...ecsSettings, cpu: Number(e.target.value) })}>
                <option value={256}>256 (0.25 vCPU)</option>
                <option value={512}>512 (0.5 vCPU)</option>
                <option value={1024}>1024 (1 vCPU)</option>
                <option value={2048}>2048 (2 vCPU)</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="ecs_mem" className="field-label">Memory (MB)</label>
              <select id="ecs_mem" className="field-input" value={ecsSettings.memory_mb}
                onChange={(e) => setEcsSettings({ ...ecsSettings, memory_mb: Number(e.target.value) })}>
                <option value={512}>512 MB</option>
                <option value={1024}>1024 MB (1 GB)</option>
                <option value={2048}>2048 MB (2 GB)</option>
                <option value={4096}>4096 MB (4 GB)</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="count" className="field-label">Task Replicas</label>
              <input id="count" type="number" min={1} max={10} className="field-input" value={ecsSettings.desired_count}
                onChange={(e) => setEcsSettings({ ...ecsSettings, desired_count: Number(e.target.value) })} />
            </div>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="cert" className="field-label">ACM Certificate ARN (optional for HTTPS)</label>
            <input id="cert" type="text" placeholder="arn:aws:acm:region:account:certificate/..." className="field-input text-xs" value={ecsSettings.certificate_arn || ""}
              onChange={(e) => setEcsSettings({ ...ecsSettings, certificate_arn: e.target.value })} />
            <p className="text-2xs text-slate-500">If omitted, ALB provisions an HTTP listener on port 80.</p>
          </div>
        </div>
      )}

      {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
      <button type="button" className="btn-primary" onClick={start} disabled={busy || !dep.can_prepare}>
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Hammer className="w-4 h-4" />}
        {dep.status === "VERIFIED" ? "Rebuild & verify" : "Build & verify"}
      </button>
    </div>
  );
}

const ENV_KEY_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;

const PRESET_TITLES: Record<FullstackPreset, string> = { dev: "Dev", staging: "Staging", production: "Production" };

function FullstackSettingsForm({
  depId,
  applySuggestedPreset,
  layout,
  settings,
  onChange,
}: {
  depId: string;
  applySuggestedPreset: boolean;
  layout: FullstackLayout | null;
  settings: FullstackSettings;
  onChange: (s: FullstackSettings) => void;
}) {
  const [newKey, setNewKey] = useState("");
  const [estimate, setEstimate] = useState<DeploymentEstimateResponse | null>(null);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const presetApplied = useRef(!applySuggestedPreset);
  const settingsKey = JSON.stringify(settings);

  // Live estimate (backend/deploy/estimates.py), debounced while the user edits.
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      estimateDeployment(depId, JSON.parse(settingsKey) as FullstackSettings)
        .then((res) => {
          if (cancelled) return;
          setEstimate(res);
          setEstimateError(null);
          // A deployment that was never configured starts from the preset for its environment.
          if (!presetApplied.current) {
            presetApplied.current = true;
            const current = JSON.parse(settingsKey) as FullstackSettings;
            onChange({ ...current, ...res.presets[res.suggested_preset], preset: res.suggested_preset });
          }
        })
        .catch((e) => !cancelled && setEstimateError(e instanceof Error ? e.message : "Estimate unavailable"));
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [depId, settingsKey, onChange]);

  const applyPreset = (name: FullstackPreset) => {
    if (!estimate) return;
    onChange({ ...settings, ...estimate.presets[name], preset: name });
  };
  const hasFrontend = Boolean(layout?.frontend);
  const db = layout?.database ?? null;
  const detectedKeys = layout?.env_keys ?? [];
  const allKeys = Array.from(new Set([...detectedKeys, ...settings.secret_env_keys])).sort();
  const set = <K extends keyof FullstackSettings>(key: K, value: FullstackSettings[K]) => onChange({ ...settings, [key]: value });
  const toggleKey = (key: string) =>
    set(
      "secret_env_keys",
      settings.secret_env_keys.includes(key)
        ? settings.secret_env_keys.filter((k) => k !== key)
        : [...settings.secret_env_keys, key].sort(),
    );
  const addKey = () => {
    const key = newKey.trim().toUpperCase();
    if (!ENV_KEY_PATTERN.test(key) || key.startsWith("AWS_")) return;
    if (!settings.secret_env_keys.includes(key)) set("secret_env_keys", [...settings.secret_env_keys, key].sort());
    setNewKey("");
  };
  const newKeyValid = ENV_KEY_PATTERN.test(newKey.trim().toUpperCase()) && !newKey.trim().toUpperCase().startsWith("AWS_");

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <div className="field-label flex items-center gap-1.5"><Zap className="w-3.5 h-3.5" /> Preset</div>
        <div className="grid sm:grid-cols-3 gap-2">
          {(["dev", "staging", "production"] as FullstackPreset[]).map((name) => (
            <button key={name} type="button" disabled={!estimate} onClick={() => applyPreset(name)} aria-pressed={settings.preset === name}
              className={`text-left rounded-xl border px-3 py-2.5 text-xs transition-colors disabled:opacity-60 ${
                settings.preset === name ? "border-brand-600 bg-brand-50 text-brand-800" : "border-slate-200 hover:bg-slate-50"
              }`}>
              <div className="font-semibold">{PRESET_TITLES[name]}</div>
              <div className="text-2xs text-slate-500 mt-0.5">{estimate?.preset_descriptions[name] ?? "Loading…"}</div>
            </button>
          ))}
        </div>
        <p className="text-2xs text-slate-500">A preset fills in the settings below; anything you change afterwards is kept.</p>
      </div>

      <label className={`flex items-start gap-2 text-xs ${hasFrontend ? "text-slate-400" : "text-slate-700"}`}>
        <input type="checkbox" className="mt-0.5" disabled={hasFrontend} checked={settings.cdn_enabled || hasFrontend}
          onChange={(e) => onChange({ ...settings, cdn_enabled: e.target.checked })} />
        <span>
          CloudFront in front of the app (HTTPS URL, caching)
          <span className="block text-2xs text-slate-500">
            {hasFrontend
              ? "Always on here: the separate frontend is served from a private S3 bucket through CloudFront."
              : "Off: the app is served over plain HTTP from the load balancer URL, and the first deploy is 4–8 minutes faster."}
          </span>
        </span>
      </label>

      <div className="grid sm:grid-cols-4 gap-4">
        <div className="space-y-1.5">
          <label htmlFor="fs_port" className="field-label">Backend port</label>
          <input id="fs_port" type="number" min={1} max={65535} className="field-input" value={settings.container_port ?? ""}
            onChange={(e) => set("container_port", Number(e.target.value))} />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="fs_cpu" className="field-label">CPU units</label>
          <select id="fs_cpu" className="field-input" value={settings.cpu} onChange={(e) => set("cpu", Number(e.target.value))}>
            <option value={256}>256 (0.25 vCPU)</option>
            <option value={512}>512 (0.5 vCPU)</option>
            <option value={1024}>1024 (1 vCPU)</option>
            <option value={2048}>2048 (2 vCPU)</option>
          </select>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="fs_mem" className="field-label">Memory (MB)</label>
          <select id="fs_mem" className="field-input" value={settings.memory_mb} onChange={(e) => set("memory_mb", Number(e.target.value))}>
            <option value={512}>512 MB</option>
            <option value={1024}>1024 MB (1 GB)</option>
            <option value={2048}>2048 MB (2 GB)</option>
            <option value={4096}>4096 MB (4 GB)</option>
          </select>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="fs_health" className="field-label">Health check path</label>
          <input id="fs_health" type="text" className="field-input" value={settings.health_check_path}
            onChange={(e) => set("health_check_path", e.target.value)} />
        </div>
      </div>

      <div className="space-y-2">
        <div className="field-label flex items-center gap-1.5"><Database className="w-3.5 h-3.5" /> Database</div>
        <div className="grid sm:grid-cols-3 gap-2">
          {([
            ["rds", db?.rds_supported ? `New ${db.engine === "mysql" ? "MySQL" : "PostgreSQL"} on RDS` : "New database on RDS", "Private subnets; the password is created and kept by RDS."],
            ["external", "Database I host", "An empty DATABASE_URL secret you fill with your own connection string."],
            ["none", "No database", "Nothing database-related is created."],
          ] as [FullstackSettings["database"], string, string][]).map(([mode, title, hint]) => {
            const disabled = mode === "rds" && !db?.rds_supported;
            return (
              <button key={mode} type="button" disabled={disabled} onClick={() => set("database", mode)} aria-pressed={settings.database === mode}
                className={`text-left rounded-xl border px-3 py-2.5 text-xs transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
                  settings.database === mode ? "border-brand-600 bg-brand-50 text-brand-800" : "border-slate-200 hover:bg-slate-50"
                }`}>
                <div className="font-semibold">{title}</div>
                <div className="text-2xs text-slate-500 mt-0.5">{disabled ? "No PostgreSQL or MySQL driver was found in the backend." : hint}</div>
              </button>
            );
          })}
        </div>
        {settings.database === "rds" && (
          <div className="grid sm:grid-cols-3 gap-4 pt-1">
            <div className="space-y-1.5">
              <label htmlFor="fs_dbclass" className="field-label">Instance class</label>
              <select id="fs_dbclass" className="field-input" value={settings.db_instance_class}
                onChange={(e) => set("db_instance_class", e.target.value as FullstackSettings["db_instance_class"])}>
                <option value="db.t4g.micro">db.t4g.micro (2 vCPU burst, 1 GB)</option>
                <option value="db.t4g.small">db.t4g.small (2 GB)</option>
                <option value="db.t4g.medium">db.t4g.medium (4 GB)</option>
                <option value="db.t4g.large">db.t4g.large (8 GB)</option>
                <option value="db.m7g.large">db.m7g.large (8 GB, steady)</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <label htmlFor="fs_dbsize" className="field-label">Storage (GB)</label>
              <input id="fs_dbsize" type="number" min={20} max={500} className="field-input" value={settings.db_allocated_storage_gb}
                onChange={(e) => set("db_allocated_storage_gb", Number(e.target.value))} />
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-700 sm:mt-6">
              <input type="checkbox" checked={settings.db_multi_az} onChange={(e) => set("db_multi_az", e.target.checked)} />
              Standby in a second zone (doubles DB cost)
            </label>
            <div className="space-y-1.5">
              <label htmlFor="fs_backups" className="field-label">Backup retention (days)</label>
              <input id="fs_backups" type="number" min={0} max={35} className="field-input" value={settings.db_backup_retention_days}
                onChange={(e) => set("db_backup_retention_days", Number(e.target.value))} />
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-700 sm:mt-6 sm:col-span-2">
              <input type="checkbox" checked={settings.db_final_snapshot} onChange={(e) => set("db_final_snapshot", e.target.checked)} />
              Keep a final snapshot when this deployment is torn down
            </label>
            {layout?.migration && (
              <label className="flex items-start gap-2 text-xs text-slate-700 sm:col-span-3">
                <input type="checkbox" className="mt-0.5" checked={settings.run_migrations} onChange={(e) => set("run_migrations", e.target.checked)} />
                <span>
                  Create/update tables on start with <code className="font-mono">{layout.migration.command.join(" ")}</code>
                  <span className="block text-2xs text-slate-500">Found in {layout.migration.evidence[0]?.file}. A failure is logged and the app still starts.</span>
                </span>
              </label>
            )}
          </div>
        )}
      </div>

      <div className="space-y-2">
        <div className="field-label flex items-center gap-1.5"><KeyRound className="w-3.5 h-3.5" /> Secrets (environment variables)</div>
        <p className="text-2xs text-slate-500">
          Each checked name becomes an <strong>empty</strong> AWS Secrets Manager secret. You paste the values in the AWS console after
          deploying; TerraAgent never sees them. The app starts once every checked secret has a value, so untick any it doesn&apos;t need.
        </p>
        {allKeys.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {allKeys.map((key) => (
              <label key={key} className={`flex items-center gap-1.5 rounded-lg border px-2 py-1 text-2xs font-mono cursor-pointer ${
                settings.secret_env_keys.includes(key) ? "border-brand-300 bg-brand-50 text-brand-800" : "border-slate-200 text-slate-500"
              }`}>
                <input type="checkbox" checked={settings.secret_env_keys.includes(key)} onChange={() => toggleKey(key)} />
                {key}
                {!detectedKeys.includes(key) && <span className="text-slate-400 font-sans">(added)</span>}
              </label>
            ))}
          </div>
        ) : (
          <p className="text-2xs text-slate-500">No environment variables were found in the backend code.</p>
        )}
        <div className="flex gap-2 max-w-sm">
          <input type="text" placeholder="ADD_ANOTHER_KEY" className="field-input text-xs font-mono" value={newKey}
            onChange={(e) => setNewKey(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addKey(); } }} />
          <button type="button" className="btn-secondary text-xs" onClick={addKey} disabled={!newKeyValid}>Add</button>
        </div>
      </div>

      <EstimatePanel estimate={estimate?.estimate ?? null} error={estimateError} />

      {layout && layout.warnings.length > 0 && (
        <div className="p-3 rounded-xl bg-amber-50 border border-amber-200 text-xs text-amber-900 space-y-1">
          <div className="font-semibold flex items-center gap-1.5"><AlertTriangle className="w-4 h-4" /> Before you deploy</div>
          <ul className="list-disc pl-5 space-y-0.5">
            {layout.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function EstimatePanel({ estimate, error }: { estimate: DeploymentEstimateResponse["estimate"] | null; error: string | null }) {
  if (error) return <p className="text-2xs text-slate-500">Estimate unavailable: {error}</p>;
  if (!estimate) return <p className="text-2xs text-slate-500">Estimating cost and time…</p>;
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-4 space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="text-2xs text-slate-500 flex items-center gap-1"><Wallet className="w-3.5 h-3.5" /> Estimated cost</div>
          <div className="text-lg font-bold text-slate-900">${estimate.monthly_usd.toFixed(0)}<span className="text-xs font-medium text-slate-500"> / month</span></div>
        </div>
        <div>
          <div className="text-2xs text-slate-500 flex items-center gap-1"><Clock className="w-3.5 h-3.5" /> First deploy</div>
          <div className="text-lg font-bold text-slate-900">{estimate.minutes_low}–{estimate.minutes_high}<span className="text-xs font-medium text-slate-500"> min</span></div>
        </div>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-500">Breakdown</summary>
        <ul className="mt-2 space-y-1">
          {estimate.lines.map((line) => (
            <li key={line.item} className="flex justify-between gap-4">
              <span className="text-slate-600">{line.item}</span>
              <span className="font-mono text-slate-900">${line.monthly_usd.toFixed(2)}</span>
            </li>
          ))}
        </ul>
      </details>
      <ul className="text-2xs text-slate-500 list-disc pl-4 space-y-0.5">
        {estimate.notes.map((n) => <li key={n}>{n}</li>)}
      </ul>
    </div>
  );
}

function AnalysisCard({ dep }: { dep: DeploymentDetail }) {
  const p = dep.profile;
  const intake = dep.intake;
  return (
    <div className="card p-5 space-y-4">
      <SectionHeading icon={Search} title="Analysis" description="Read from the files only; nothing in the project was run." />
      {intake && (
        <div>
          <Row label="Source">{dep.source_name}</Row>
          <Row label="Files kept">{`${intake.file_count} (${formatBytes(intake.total_bytes)})`}</Row>
          {intake.dropped_count > 0 && (
            <details className="text-xs py-1.5">
              <summary className="cursor-pointer text-slate-500">{intake.dropped_count} files not packaged</summary>
              <ul className="mt-2 space-y-1 max-h-48 overflow-y-auto font-mono text-2xs">
                {intake.dropped.map((d) => (
                  <li key={d.path}><span className="text-slate-900">{d.path}</span> <span className="text-slate-500">· {d.reason}</span></li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {intake?.secret_hits && intake.secret_hits.length > 0 && (
        <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-900 space-y-1">
          <div className="font-semibold flex items-center gap-1.5"><AlertTriangle className="w-4 h-4" /> Credentials found</div>
          <ul className="font-mono text-2xs space-y-0.5">
            {intake.secret_hits.map((h) => (
              <li key={`${h.path}:${h.line}:${h.kind}`}>{h.path}{h.line ? `:${h.line}` : ""} · {h.kind}</li>
            ))}
          </ul>
        </div>
      )}
      {p && (
        <div>
          <Row label="Runtime">{p.runtime}{p.runtime_version ? ` ${p.runtime_version}` : ""}</Row>
          <Row label="Framework">{p.framework ?? "—"}</Row>
          {p.lambda_handler && <Row label="Lambda handler"><code>{p.lambda_handler}</code></Row>}
          {p.static_output_dir !== null && <Row label="Static site">{p.static_output_dir || "project root"}</Row>}
          {p.dependencies.length > 0 && <Row label="Dependencies">{`${p.dependencies.length} (${p.dependency_manifest})`}</Row>}
          {p.server_entrypoint && <Row label="Server">{p.listens_on_port ? `listens on ${p.listens_on_port}` : "yes"}</Row>}
          {p.fullstack && (
            <>
              <Row label="Backend">{`${p.fullstack.backend.framework ?? p.fullstack.backend.runtime} in ${p.fullstack.backend.dir || "project root"}`}</Row>
              <Row label="Frontend">
                {p.fullstack.frontend
                  ? `${p.fullstack.frontend.framework ?? "static"} in ${p.fullstack.frontend.dir || "project root"}`
                  : "served by the backend"}
              </Row>
              <Row label="Database">
                {p.fullstack.database
                  ? `${p.fullstack.database.engine}${p.fullstack.database.rds_supported ? "" : " (not provisioned on AWS)"}`
                  : "none detected"}
              </Row>
              {p.fullstack.env_keys.length > 0 && <Row label="Env variables">{p.fullstack.env_keys.join(", ")}</Row>}
            </>
          )}
          {Object.keys(p.evidence).length > 0 && (
            <details className="text-xs py-1.5">
              <summary className="cursor-pointer text-slate-500">Evidence</summary>
              <ul className="mt-2 space-y-1 font-mono text-2xs">
                {Object.entries(p.evidence).flatMap(([field, items]) =>
                  items.map((e) => <li key={`${field}-${e.file}-${e.rule}`}>{field}: {e.file} ({e.rule})</li>),
                )}
              </ul>
            </details>
          )}
        </div>
      )}
      {dep.decision && dep.decision.reasons.length > 0 && (
        <ul className="space-y-1.5">
          {dep.decision.reasons.map((r) => {
            const fits = POSITIVE_REASONS.has(r.code);
            return (
              <li key={r.code} className="flex gap-2 text-xs">
                {fits ? <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" /> : <Circle className="w-4 h-4 text-amber-500 shrink-0" />}
                <span className="text-slate-700">{r.message}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function VerificationCard({ dep }: { dep: DeploymentDetail }) {
  const v = dep.verification;
  if (!v) return null;
  const tone = v.verdict === "PASS" ? "text-emerald-700" : v.verdict === "FAIL" ? "text-rose-700" : "text-amber-700";
  const findings = v.security.findings ?? [];
  return (
    <div className="card p-5 space-y-4">
      <SectionHeading icon={ShieldCheck} title="Verification" description="Terraform validation, security scans and cost estimate." />
      <div className={`text-sm font-bold ${tone}`}>Verdict: {v.verdict}</div>
      {v.incomplete_reasons.length > 0 && (
        <p className="text-xs text-amber-800">Not fully verified: {v.incomplete_reasons.join("; ")}. Missing checks are never counted as passing.</p>
      )}
      <div className="grid sm:grid-cols-3 gap-3">
        {v.validation.checks.map((c) => (
          <div key={c.check_name} className="p-3 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-between text-xs">
            <span className="font-semibold">terraform {c.check_name}</span>
            {c.passed ? <CheckCircle2 className="w-4 h-4 text-emerald-600" /> : <XCircle className="w-4 h-4 text-rose-600" />}
          </div>
        ))}
      </div>
      <div className="grid sm:grid-cols-2 gap-3 text-xs">
        <div className="p-3 rounded-xl border border-slate-100">
          <div className="text-slate-500">Security posture</div>
          <div className="text-lg font-bold">{v.security_posture.score ?? "—"}{v.security_posture.score !== null && <span className="text-xs text-slate-500">/100</span>}</div>
          <div className="text-2xs text-slate-500">{v.security_posture.reason}</div>
        </div>
        <div className="p-3 rounded-xl border border-slate-100">
          <div className="text-slate-500">Estimated monthly cost</div>
          <div className="text-lg font-bold">
            {v.cost.tool_skipped || v.cost.total_monthly_cost === undefined
              ? "Not estimated"
              : `${v.cost.total_monthly_cost.toFixed(2)} ${v.cost.currency ?? "USD"}`}
          </div>
          <div className="text-2xs text-slate-500">
            {v.cost.tool_skipped
              ? "Infracost API key missing in Settings (this is not $0)"
              : "Infracost, usage-based items excluded"}
          </div>
        </div>
      </div>
      {findings.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-3xs uppercase tracking-wider text-slate-500 bg-slate-50">
              <tr><th className="text-left px-3 py-2">Severity</th><th className="text-left px-3 py-2">Rule</th><th className="text-left px-3 py-2">Resource</th><th className="text-left px-3 py-2">Finding</th></tr>
            </thead>
            <tbody>
              {findings.slice(0, 50).map((f, i) => (
                <tr key={`${f.rule_id}-${f.resource}-${i}`} className="border-t border-slate-100">
                  <td className="px-3 py-2 font-semibold">{f.severity}</td>
                  <td className="px-3 py-2 font-mono">{f.rule_id}</td>
                  <td className="px-3 py-2 font-mono">{f.resource ?? ""}</td>
                  <td className="px-3 py-2">{f.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TerraformFiles({ dep }: { dep: DeploymentDetail }) {
  const [files, setFiles] = useState<Record<string, string> | null>(null);
  const [active, setActive] = useState<string>("main.tf");
  const key = `${dep.id}:${dep.updated_at}`;

  useEffect(() => {
    if (dep.rendered_files.length === 0) return;
    let cancelled = false;
    fetchDeploymentTerraform(dep.id).then((f) => !cancelled && setFiles(f)).catch(() => !cancelled && setFiles({}));
    return () => {
      cancelled = true;
    };
  }, [key, dep.id, dep.rendered_files.length]);

  if (dep.rendered_files.length === 0 || !files) return null;
  const names = Object.keys(files).sort();
  const shown = files[active] !== undefined ? active : names[0];
  return (
    <div className="card overflow-hidden">
      <div className="card-header">
        <SectionHeading icon={FileCode2} title="Generated Terraform" description="From TerraAgent's vetted template; your values are in terraform.tfvars.json." />
      </div>
      <div className="flex flex-wrap gap-1 px-4 pt-3">
        {names.map((n) => (
          <button key={n} type="button" onClick={() => setActive(n)}
            className={`px-2.5 py-1 rounded-lg text-2xs font-mono ${n === shown ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-700"}`}>
            {n}
          </button>
        ))}
      </div>
      <pre className="m-4 p-4 rounded-xl bg-slate-950 text-slate-100 text-2xs overflow-auto max-h-[28rem]">{files[shown]}</pre>
    </div>
  );
}

function Logs({ lines }: { lines: JobLogLine[] }) {
  if (lines.length === 0) return null;
  return (
    <details className="card p-5" open>
      <summary className="cursor-pointer section-title"><Terminal className="w-4 h-4" /> Live log</summary>
      <pre className="mt-3 p-3 rounded-xl bg-slate-950 text-slate-100 text-2xs overflow-auto max-h-72 whitespace-pre-wrap">
        {lines.slice(-200).map((l) => l.message).join("\n")}
      </pre>
    </details>
  );
}

function GitOpsPrCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [token, setToken] = useState("");
  const defaultRepo = dep.source_name.includes("/") ? dep.source_name.split("@")[0] : "";
  const [repo, setRepo] = useState(defaultRepo);
  const [baseBranch, setBaseBranch] = useState("main");
  const [targetDir, setTargetDir] = useState("terraform");
  const [addWorkflows, setAddWorkflows] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleOpenPr = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token.trim() || !repo.trim()) {
      setError("GitHub Personal Access Token and Repository are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createDeploymentPullRequest(dep.id, {
        github_token: token.trim(),
        repo: repo.trim(),
        base_branch: baseBranch.trim() || "main",
        target_dir: targetDir.trim() || "terraform",
        add_workflows: addWorkflows,
      });
      onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to open Pull Request");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-slate-200 bg-slate-50/50">
      <SectionHeading
        icon={GitPullRequest}
        title="Deliver via GitHub Pull Request (GitOps)"
        description="Creates a feature branch and Pull Request with Terraform configurations and GitHub Actions CI/CD workflows."
      />
      {error && <p className="text-xs text-rose-700">{error}</p>}
      <form onSubmit={handleOpenPr} className="space-y-3">
        <div className="grid sm:grid-cols-2 gap-3">
          <div className="space-y-1">
            <label className="field-label">Target Repository (owner/repo)</label>
            <input
              type="text"
              placeholder="e.g. acme-corp/infrastructure"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              className="field-input text-xs"
              required
            />
          </div>
          <div className="space-y-1">
            <label className="field-label">GitHub Personal Access Token (PAT)</label>
            <input
              type="password"
              placeholder="ghp_... (contents=write, pull_requests=write)"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="field-input text-xs"
              required
            />
          </div>
        </div>
        <div className="grid sm:grid-cols-2 gap-3">
          <div className="space-y-1">
            <label className="field-label">Base Branch</label>
            <input
              type="text"
              value={baseBranch}
              onChange={(e) => setBaseBranch(e.target.value)}
              className="field-input text-xs"
            />
          </div>
          <div className="space-y-1">
            <label className="field-label">Subdirectory in Repo</label>
            <input
              type="text"
              value={targetDir}
              onChange={(e) => setTargetDir(e.target.value)}
              className="field-input text-xs"
            />
          </div>
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-700 cursor-pointer pt-1">
          <input
            type="checkbox"
            checked={addWorkflows}
            onChange={(e) => setAddWorkflows(e.target.checked)}
            className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span>Include GitHub Actions CI/CD workflows (<code>.github/workflows/terraagent-*.yml</code>)</span>
        </label>
        <button
          type="submit"
          disabled={busy || !token.trim() || !repo.trim()}
          className="btn-secondary w-full py-2 rounded-xl text-xs flex items-center justify-center gap-2 font-medium border-slate-300 hover:bg-white"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitPullRequest className="w-4 h-4 text-indigo-600" />}
          {busy ? "Opening Pull Request..." : "Open GitHub Pull Request"}
        </button>
      </form>
    </div>
  );
}

function PullRequestStatusCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const pr = dep.pr;
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!pr) return null;

  const handleMerge = async () => {
    if (!token.trim()) {
      setError("GitHub Personal Access Token is required to merge.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await mergeDeploymentPullRequest(dep.id, {
        github_token: token.trim(),
        merge_method: "squash",
      });
      onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to merge Pull Request");
      setBusy(false);
    }
  };

  const isMerged = dep.status === "MERGED";

  return (
    <div className={`card p-5 space-y-4 border ${isMerged ? "border-purple-200 bg-purple-50/40" : "border-blue-200 bg-blue-50/40"}`}>
      <div className="flex justify-between items-start gap-4 flex-wrap">
        <SectionHeading
          icon={isMerged ? GitMerge : GitPullRequest}
          title={isMerged ? "GitHub Pull Request Merged" : "GitHub Pull Request Active"}
          description={isMerged ? "The deployment PR has been successfully merged into the target repository." : "Review and merge this PR on GitHub or merge directly below."}
        />
        <a
          href={pr.html_url}
          target="_blank"
          rel="noopener noreferrer"
          className="btn-secondary px-3 py-1.5 text-xs flex items-center gap-1.5 font-semibold text-slate-900 border-slate-300 hover:bg-white"
        >
          <span>View PR #{pr.number}</span>
          <ExternalLink className="w-3.5 h-3.5" />
        </a>
      </div>

      <div className="grid sm:grid-cols-3 gap-3 text-xs">
        <div className="p-3 rounded-xl bg-white border border-slate-200/80">
          <div className="text-slate-500 text-2xs uppercase">Repository</div>
          <div className="font-mono font-semibold text-slate-800 break-all">{pr.repo}</div>
        </div>
        <div className="p-3 rounded-xl bg-white border border-slate-200/80">
          <div className="text-slate-500 text-2xs uppercase">Branch</div>
          <div className="font-mono font-semibold text-slate-800 break-all">{pr.branch}</div>
        </div>
        <div className="p-3 rounded-xl bg-white border border-slate-200/80">
          <div className="text-slate-500 text-2xs uppercase">Commit SHA</div>
          <div className="font-mono font-semibold text-slate-800">{pr.commit_sha?.substring(0, 8)}</div>
        </div>
      </div>

      {dep.status === "PR_OPEN" && (
        <div className="p-4 rounded-xl bg-white border border-blue-100 space-y-3">
          <div className="text-xs font-semibold text-slate-900">Merge PR from TerraAgent</div>
          {error && <p className="text-xs text-rose-700">{error}</p>}
          <div className="flex gap-2 items-center flex-wrap sm:flex-nowrap">
            <input
              type="password"
              placeholder="GitHub PAT (to authorize merge)"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="field-input text-xs flex-1"
            />
            <button
              type="button"
              onClick={handleMerge}
              disabled={busy || !token.trim()}
              className="btn-primary py-2 px-4 rounded-xl text-xs flex items-center gap-2 whitespace-nowrap bg-purple-600 hover:bg-purple-700 text-white font-medium"
            >
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitMerge className="w-4 h-4" />}
              {busy ? "Merging..." : "Merge Pull Request"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DeployCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startDeploy = async () => {
    if (!confirm) {
      setError("You must check the confirmation box to deploy.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await deployDeployment(dep.id, true);
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to trigger deploy");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-indigo-200 bg-indigo-50/40">
      <SectionHeading icon={Rocket} title="Option A: STS Direct Apply" description={`Approved by ${dep.approved_by ?? "operator"}. Apply changes directly to AWS.`} />
      {error && <p className="text-xs text-rose-700">{error}</p>}
      <label className="flex items-start gap-2.5 text-xs text-slate-700 cursor-pointer">
        <input
          type="checkbox"
          checked={confirm}
          onChange={(e) => setConfirm(e.target.checked)}
          className="mt-0.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
        />
        <span>I confirm that I want TerraAgent to execute this approved Terraform plan against AWS using short-lived STS credentials.</span>
      </label>
      <button
        type="button"
        onClick={startDeploy}
        disabled={busy || !confirm}
        className="btn-primary w-full bg-indigo-600 hover:bg-indigo-700 text-white font-medium py-2 rounded-xl text-xs flex items-center justify-center gap-2"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Rocket className="w-4 h-4" />}
        {busy ? "Initiating deploy..." : "Deploy to AWS Now"}
      </button>
    </div>
  );
}

function RollbackCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [artifacts, setArtifacts] = useState<BuildHistoryItem[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchDeploymentArtifacts(dep.id).then((items) => {
      setArtifacts(items);
      if (items.length > 1) {
        setSelectedArtifact(items[1].artifact_id);
      } else if (items.length > 0) {
        setSelectedArtifact(items[0].artifact_id);
      }
    });
  }, [dep.id, dep.updated_at]);

  if (artifacts.length === 0) return null;

  const handleRollback = async () => {
    setBusy(true);
    setError(null);
    try {
      const art = artifacts.find((a) => a.artifact_id === selectedArtifact);
      await rollbackDeployment(dep.id, {
        target_artifact_id: selectedArtifact || undefined,
        target_release_id: art?.release_id || undefined,
        reason: `Rollback to build ${selectedArtifact?.substring(0, 8)}`,
      });
      onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rollback request failed");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-amber-200 bg-amber-50/30">
      <SectionHeading
        icon={RotateCcw}
        title="Zero-Downtime Rollback"
        description="Switch CloudFront origin_path or Lambda live alias to a previous build version."
      />
      {error && <p className="text-xs text-rose-700">{error}</p>}
      <div className="space-y-3">
        <div className="space-y-1">
          <label className="field-label">Select Target Build Version</label>
          <select
            className="field-input text-xs"
            value={selectedArtifact}
            onChange={(e) => setSelectedArtifact(e.target.value)}
          >
            {artifacts.map((a, idx) => (
              <option key={a.artifact_id} value={a.artifact_id}>
                {idx === 0 ? "Latest Build" : `Build ${idx + 1}`} ({a.release_id || a.sha256.substring(0, 8)}) - {new Date(a.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={handleRollback}
          disabled={busy || !selectedArtifact}
          className="btn-secondary w-full py-2 rounded-xl text-xs flex items-center justify-center gap-2 font-medium border-amber-300 text-amber-900 hover:bg-amber-100/60"
        >
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4 text-amber-700" />}
          {busy ? "Preparing Rollback Plan..." : "Rollback to Selected Build"}
        </button>
      </div>
    </div>
  );
}

function TeardownCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handlePlanDestroy = async () => {
    if (!confirm) {
      setError("Please check the confirmation box to initiate teardown planning.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await planDestroyDeployment(dep.id);
      onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to initiate teardown");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4 border-rose-200 bg-rose-50/40">
      <SectionHeading
        icon={Trash2}
        title="Infrastructure Teardown (Destroy)"
        description="Plan and destroy all AWS infrastructure provisioned for this deployment."
      />
      {error && <p className="text-xs text-rose-700">{error}</p>}
      <div className="p-3 rounded-xl bg-white/80 border border-rose-200 text-xs text-rose-900 space-y-1">
        <p className="font-semibold">Destructive Action Warning</p>
        <p>This generates a <code>terraform plan -destroy</code> against AWS. All live resources associated with this deployment will be planned for deletion.</p>
      </div>
      <label className="flex items-start gap-2.5 text-xs text-slate-700 cursor-pointer">
        <input
          type="checkbox"
          checked={confirm}
          onChange={(e) => setConfirm(e.target.checked)}
          className="mt-0.5 rounded border-slate-300 text-rose-600 focus:ring-rose-500"
        />
        <span>I understand that this will initiate a teardown plan to delete live AWS resources.</span>
      </label>
      <button
        type="button"
        onClick={handlePlanDestroy}
        disabled={busy || !confirm}
        className="btn-danger w-full py-2 rounded-xl text-xs flex items-center justify-center gap-2 font-medium bg-rose-600 hover:bg-rose-700 text-white"
      >
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
        {busy ? "Generating Destroy Plan..." : "Plan Infrastructure Teardown"}
      </button>
    </div>
  );
}

function OutputsCard({ outputs }: { outputs: Record<string, unknown> }) {
  return (
    <div className="card p-5 space-y-3 bg-emerald-50/40 border-emerald-200">
      <SectionHeading icon={CheckCircle2} title="Deployed Outputs" description="Live AWS endpoints and provisioned infrastructure resources." />
      <div className="space-y-2">
        {Object.entries(outputs).map(([k, v]) => {
          const valStr = typeof v === "string" ? v : JSON.stringify(v);
          const isUrl = typeof v === "string" && (v.startsWith("http://") || v.startsWith("https://"));
          return (
            <div key={k} className="flex justify-between items-center gap-4 py-1.5 border-b border-emerald-100 last:border-0 text-xs">
              <span className="font-mono text-slate-600 font-medium">{k}</span>
              {isUrl ? (
                <a
                  href={valStr}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-indigo-600 hover:text-indigo-800 font-mono font-semibold flex items-center gap-1"
                >
                  {valStr}
                  <ExternalLink className="w-3 h-3" />
                </a>
              ) : (
                <span className="font-mono text-slate-900 break-all">{valStr}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** DEPLOYED -> new source: same stack, target and settings; stops at approval. */
function UpdateCodeCard({ dep, onStarted }: { dep: DeploymentDetail; onStarted: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [ref, setRef] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fromGithub = dep.source_kind === "github";

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      if (fromGithub) {
        await updateDeploymentFromGithub(dep.id, { ref: ref.trim() || undefined, github_token: token.trim() || undefined });
      } else if (file) {
        await updateDeploymentSource(dep.id, file);
      }
      onStarted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the code update");
      setBusy(false);
    }
  };

  return (
    <div className="card p-5 space-y-4">
      <SectionHeading
        icon={Upload}
        title="Deploy a new version"
        description="Same AWS stack, target and settings. TerraAgent analyzes, builds, verifies and plans it on its own, then waits for your approval."
      />
      {fromGithub ? (
        <div className="grid sm:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label htmlFor="upd_ref" className="field-label">Branch, tag or commit</label>
            <input id="upd_ref" type="text" className="field-input font-mono text-xs" placeholder={dep.source_name.split("@")[1] || "default branch"}
              value={ref} onChange={(e) => setRef(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="upd_token" className="field-label">GitHub token (private repos only)</label>
            <input id="upd_token" type="password" autoComplete="off" className="field-input text-xs" value={token}
              onChange={(e) => setToken(e.target.value)} />
            <p className="text-2xs text-slate-500">Used once for the download, never stored.</p>
          </div>
        </div>
      ) : (
        <div className="space-y-1.5">
          <label htmlFor="upd_zip" className="field-label">New version (.zip)</label>
          <input id="upd_zip" type="file" accept=".zip,application/zip" className="field-input text-xs"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </div>
      )}
      <p className="text-2xs text-slate-500">
        A code-only change usually takes 5–7 minutes from approval to live, with no downtime. If the new version needs
        different infrastructure, the plan shows it before anything changes.
      </p>
      {error && <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">{error}</div>}
      <button type="button" className="btn-primary" onClick={start} disabled={busy || (!fromGithub && !file)}>
        {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
        {fromGithub ? "Pull latest and plan" : "Upload and plan"}
      </button>
    </div>
  );
}

function CodeUpdateBanner({ dep }: { dep: DeploymentDetail }) {
  const update = dep.code_update;
  if (!update) return null;
  return (
    <div className="p-3 rounded-xl bg-indigo-50 border border-indigo-200 text-xs text-indigo-900 flex items-start gap-2">
      <Upload className="w-4 h-4 shrink-0 mt-0.5" />
      <div>
        <div className="font-semibold">Updating the deployed app to {dep.source_name}</div>
        <div>
          The live app keeps running the previous version
          {update.previous_image_tag ? <> (<code className="font-mono">{update.previous_image_tag}</code>)</> : null} until
          you approve and deploy this plan.
        </div>
      </div>
    </div>
  );
}

// backend/deploy/templates/fullstack_app/outputs.tf::secrets_to_fill (names only, never values)
function SecretsToFillCard({ outputs, region }: { outputs: Record<string, unknown>; region: string }) {
  const raw = outputs.secrets_to_fill;
  if (!raw || typeof raw !== "object") return null;
  const secrets = Object.entries(raw as Record<string, string>);
  if (secrets.length === 0) return null;
  const service = typeof outputs.service_name === "string" ? outputs.service_name : null;
  const cluster = typeof outputs.cluster_name === "string" ? outputs.cluster_name : null;
  return (
    <div className="card p-5 space-y-3 border-amber-200 bg-amber-50/40">
      <SectionHeading icon={KeyRound} title="Fill in your secrets"
        description="The app starts once each of these has a value. TerraAgent created them empty and never reads them." />
      <ol className="list-decimal pl-5 text-xs text-slate-700 space-y-1">
        <li>Open each secret below in the AWS console and choose <strong>Retrieve secret value → Set secret value</strong> (plaintext).</li>
        <li>
          Then restart the app: ECS console → cluster <code className="font-mono">{cluster ?? "…"}</code> → service{" "}
          <code className="font-mono">{service ?? "…"}</code> → <strong>Update service → Force new deployment</strong>.
        </li>
      </ol>
      <div className="space-y-1.5">
        {secrets.map(([env, name]) => (
          <div key={env} className="flex justify-between items-center gap-4 py-1.5 border-b border-amber-100 last:border-0 text-xs">
            <span className="font-mono font-semibold text-slate-800">{env}</span>
            <a
              href={`https://${region}.console.aws.amazon.com/secretsmanager/secret?name=${encodeURIComponent(name)}&region=${region}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-600 hover:text-indigo-800 font-mono flex items-center gap-1 break-all"
            >
              {name}
              <ExternalLink className="w-3 h-3 shrink-0" />
            </a>
          </div>
        ))}
      </div>
    </div>
  );
}

function ReconciliationAlert({ error }: { error?: string | null }) {
  return (
    <div className="p-4 rounded-2xl bg-amber-50 border border-amber-200 text-xs text-amber-900 space-y-2">
      <div className="flex items-center gap-2 font-semibold">
        <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0" />
        <span>Deployment Needs Reconciliation</span>
      </div>
      <p>{error ?? "Apply was interrupted before reporting completion. State may have partial resources."}</p>
      <div className="text-2xs text-amber-800 pt-1">
        Please follow <a href="file:///c:/Users/USER/Desktop/AIKART/Terraform%20Agent/docs/runbooks/deploy-reconciliation.md" className="underline font-bold">docs/runbooks/deploy-reconciliation.md</a> to inspect the state lock and re-plan.
      </div>
    </div>
  );
}

export default function DeploymentDetailView({ id }: { id: string }) {
  const [dep, setDep] = useState<DeploymentDetail | null>(null);
  const [logs, setLogs] = useState<JobLogLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Bumped after starting a build so polling restarts.
  const [reloadKey, setReloadKey] = useState(0);
  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const load = async () => {
      try {
        const [d, l] = await Promise.all([fetchDeployment(id), fetchDeploymentLogs(id)]);
        if (cancelled) return;
        setDep(d);
        setLogs(l);
        setError(null);
        if (IN_PROGRESS_STATUSES.includes(d.status)) {
          if (!timer) timer = setInterval(load, POLL_MS);
        } else if (timer) {
          clearInterval(timer);
          timer = null;
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load the deployment");
      }
    };

    load();
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [id, reloadKey]);

  if (error && !dep) {
    return <div className="card p-6 text-xs text-rose-700">{error}</div>;
  }
  if (!dep) {
    return <div className="card p-6 text-xs text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>;
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3 flex-wrap">
        <StatusBadge status={dep.status} size="md" />
        {dep.target_type && <span className="text-xs text-slate-600">{TARGET_LABELS[dep.target_type]}</span>}
        <span className="text-xs text-slate-500">{dep.region} · {dep.environment}</span>
      </div>
      <StageTracker status={dep.status} />
      {dep.status === "FAILED" && dep.error && (
        <div className="p-4 rounded-2xl bg-rose-50 border border-rose-200 text-xs text-rose-900 flex gap-2 whitespace-pre-wrap">
          <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{dep.error}</span>
        </div>
      )}
      {dep.status === "NEEDS_RECONCILIATION" && <ReconciliationAlert error={dep.error} />}
      {dep.can_rollback && <RollbackCard dep={dep} onStarted={reload} />}
      {dep.can_destroy && <TeardownCard dep={dep} onStarted={reload} />}
      {/* Active Step Actions (Prominently placed at top for fast execution) */}
      {dep.outputs && Object.keys(dep.outputs).length > 0 && <OutputsCard outputs={dep.outputs} />}
      {dep.outputs && <SecretsToFillCard outputs={dep.outputs} region={dep.region} />}
      {dep.can_update_code && <UpdateCodeCard dep={dep} onStarted={reload} />}
      {dep.code_update?.active && <CodeUpdateBanner dep={dep} />}
      {dep.pr && <PullRequestStatusCard dep={dep} onStarted={reload} />}
      {dep.can_deploy && (
        <div className="grid lg:grid-cols-2 gap-5 items-start">
          <DeployCard dep={dep} onStarted={reload} />
          <GitOpsPrCard dep={dep} onStarted={reload} />
        </div>
      )}
      {dep.can_approve && <ApprovalCard key={`approve-${dep.plan_bundle_sha256}`} dep={dep} onDecided={reload} />}
      {dep.can_plan && <PlanCard key={`plan-${dep.updated_at}`} dep={dep} onStarted={reload} />}
      {dep.can_prepare && <ConfigureForm key={dep.updated_at} dep={dep} onStarted={reload} />}

      {/* Diagnostics and Supporting Information */}
      <div className="grid lg:grid-cols-2 gap-5 items-start">
        <AnalysisCard dep={dep} />
        {dep.build && (
          <div className="card p-5 space-y-2">
            <SectionHeading icon={Hammer} title="Build Details" />
            {dep.build.runtime && <Row label="Runtime">{dep.build.runtime}</Row>}
            {dep.build.handler && <Row label="Handler"><code>{dep.build.handler}</code></Row>}
            {dep.build.kind === "static_site" && <Row label="Site files">{dep.build.file_count}</Row>}
            {dep.build.package_bytes > 0 && <Row label="Package">{formatBytes(dep.build.package_bytes)}</Row>}
            {dep.build.warnings.map((w) => <p key={w} className="text-xs text-amber-700">{w}</p>)}
          </div>
        )}
      </div>
      <VerificationCard dep={dep} />
      <TerraformFiles dep={dep} />
      <Logs lines={logs} />
    </div>
  );
}
