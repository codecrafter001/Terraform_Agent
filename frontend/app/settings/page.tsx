"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Cpu, GitBranch, Loader2, Save, Shield, SlidersHorizontal, Wrench, XCircle } from "lucide-react";
import { fetchSettings, saveWorkspaceDefaults } from "@/lib/api";
import { WorkspaceDefaults, WorkspaceSettings } from "@/lib/types";
import { PageHeader } from "@/components/ui";

const REGIONS = [
  "auto", "us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
  "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ca-central-1", "sa-east-1",
];

// Same categories as backend/routers/settings.py::KNOWN_FILTERS.
const FILTERS: { id: string; label: string }[] = [
  { id: "EC2", label: "EC2" }, { id: "ECS", label: "ECS" }, { id: "VPC", label: "VPC & gateways" },
  { id: "SG", label: "Security groups" }, { id: "S3", label: "S3" }, { id: "RDS", label: "RDS" },
  { id: "IAM", label: "IAM roles" }, { id: "ELB", label: "Load balancers" }, { id: "DYNAMODB", label: "DynamoDB" },
  { id: "KMS", label: "KMS" }, { id: "SQS", label: "SQS" }, { id: "SNS", label: "SNS" },
];

const PIPELINE_LABELS: Record<string, [string, string]> = {
  max_repair_iterations: ["Max repair cycles", "TERRAAGENT_MAX_REPAIR_ITERATIONS"],
  manage_iam_roles: ["Manage IAM roles (else Review)", "TERRAAGENT_MANAGE_IAM"],
  terraform_command_timeout_seconds: ["Terraform command timeout (s)", "TERRAAGENT_TF_COMMAND_TIMEOUT"],
  scanner_timeout_seconds: ["Security scanner timeout (s)", "TERRAAGENT_SCANNER_TIMEOUT"],
  heartbeat_seconds: ["Log heartbeat (s)", "TERRAAGENT_HEARTBEAT_SECONDS"],
  max_job_runtime_seconds: ["Max job runtime (s)", "MAX_JOB_RUNTIME_SECONDS"],
  max_adoption_wave_size: ["Max adoption wave size", "MAX_ADOPTION_WAVE_SIZE"],
  dependency_confidence_threshold: ["Dependency confidence threshold", "DEPENDENCY_CONFIDENCE_THRESHOLD"],
  bundle_expiry_hours: ["Bundle download expiry (h)", "ZIP_EXPIRY_HOURS"],
};

function Flag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between gap-3 text-xs">
      <span className="font-semibold text-slate-800">{label}</span>
      {ok ? (
        <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
      ) : (
        <XCircle className="w-4 h-4 text-slate-400 shrink-0" />
      )}
    </div>
  );
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<WorkspaceSettings | null>(null);
  const [draft, setDraft] = useState<WorkspaceDefaults | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSettings()
      .then((s) => {
        if (cancelled) return;
        setSettings(s);
        setDraft(s.defaults);
      })
      .catch((e: unknown) => !cancelled && setLoadError(e instanceof Error ? e.message : "Failed to load settings"));
    return () => {
      cancelled = true;
    };
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    setSaving(true);
    setSaveMessage(null);
    try {
      const saved = await saveWorkspaceDefaults({ ...draft, github_repo: draft.github_repo?.trim() || null });
      setDraft(saved);
      setSaveMessage({ ok: true, text: "Saved. New scans and PR forms start from these defaults." });
    } catch (err) {
      setSaveMessage({ ok: false, text: err instanceof Error ? err.message : "Failed to save" });
    } finally {
      setSaving(false);
    }
  };

  const toggleFilter = (id: string) =>
    setDraft((d) =>
      d && {
        ...d,
        resource_filters: d.resource_filters.includes(id)
          ? d.resource_filters.filter((f) => f !== id)
          : [...d.resource_filters, id],
      }
    );

  const config = settings?.configuration;

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Settings" }]}
        title="Settings"
        description="Defaults for new scans and pull requests, and the configuration this deployment is running with."
      />

      {loadError ? (
        <div className="card p-6 text-xs text-rose-700">{loadError}</div>
      ) : !settings || !draft || !config ? (
        <div className="card p-12 flex items-center justify-center gap-3 text-sm text-slate-400">
          <Loader2 className="w-5 h-5 animate-spin text-brand-600" /> Loading settings...
        </div>
      ) : (
        <div className="space-y-6 max-w-4xl">
          {/* Editable defaults */}
          <form onSubmit={save} className="card p-6 space-y-5">
            <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
              <SlidersHorizontal className="w-5 h-5 text-brand-600" />
              Workspace defaults
            </div>
            <p className="text-xs text-slate-500 -mt-3">
              Pre-fill the New Request and pull request forms. No credentials or tokens are stored here - those are
              entered per request and never saved.
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <label className="space-y-1.5">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">Default region</span>
                <select
                  value={draft.region}
                  onChange={(e) => setDraft({ ...draft, region: e.target.value })}
                  className="field-input"
                >
                  {REGIONS.map((r) => (
                    <option key={r} value={r}>
                      {r === "auto" ? "Auto (region with the most resources)" : r}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">IaC engine</span>
                <select
                  value={draft.terraform_binary}
                  onChange={(e) => setDraft({ ...draft, terraform_binary: e.target.value as "terraform" | "tofu" })}
                  className="field-input"
                >
                  <option value="terraform">Terraform</option>
                  <option value="tofu">OpenTofu</option>
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">GitHub repository</span>
                <input
                  value={draft.github_repo ?? ""}
                  onChange={(e) => setDraft({ ...draft, github_repo: e.target.value })}
                  placeholder="owner/repo or https://github.com/owner/repo"
                  className="field-input font-mono"
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">Base branch</span>
                <input
                  value={draft.base_branch}
                  onChange={(e) => setDraft({ ...draft, base_branch: e.target.value })}
                  placeholder="main"
                  className="field-input font-mono"
                />
              </label>
            </div>

            <div className="space-y-2">
              <span className="text-2xs font-semibold uppercase tracking-wider text-slate-500">Resource types to scan</span>
              <div className="flex flex-wrap gap-2">
                {FILTERS.map((f) => {
                  const on = draft.resource_filters.includes(f.id);
                  return (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => toggleFilter(f.id)}
                      aria-pressed={on}
                      className={`px-3 py-1.5 rounded-lg border text-2xs font-semibold transition-colors ${
                        on ? "bg-slate-900 text-white border-slate-900" : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
                      }`}
                    >
                      {f.label}
                    </button>
                  );
                })}
              </div>
              {draft.resource_filters.includes("ECS") && (
                <p className="text-2xs text-amber-700">ECS isn&apos;t discovered yet, so selecting it finds nothing.</p>
              )}
            </div>

            <div className="flex items-center justify-between gap-3 pt-2 border-t border-slate-100">
              <span className={`text-xs ${saveMessage?.ok ? "text-emerald-700" : "text-rose-700"}`}>{saveMessage?.text}</span>
              <button type="submit" disabled={saving} className="btn-primary">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Save defaults
              </button>
            </div>
          </form>

          {/* Safety */}
          <div className="card p-6 space-y-4">
            <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
              <Shield className="w-5 h-5 text-emerald-600" />
              Safety guardrails (enforced in code, not configurable)
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 space-y-1">
                <div className="font-semibold text-slate-800">Allowed terraform / tofu subcommands</div>
                <div className="font-mono text-slate-600">{config.safety.allowed_terraform_subcommands.join(", ")}</div>
              </div>
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 space-y-1">
                <div className="font-semibold text-slate-800">Always blocked</div>
                <div className="font-mono text-rose-700">{config.safety.blocked_terraform_commands.join(", ")}</div>
              </div>
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 space-y-1">
                <div className="font-semibold text-slate-800">AWS API access</div>
                <div className="text-slate-600">{config.safety.aws_api_access}</div>
              </div>
              <Flag ok={config.safety.api_key_required} label="API key required on /api" />
            </div>
          </div>

          {/* Pipeline */}
          <div className="card p-6 space-y-4">
            <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
              <Cpu className="w-5 h-5 text-brand-600" />
              Pipeline configuration
            </div>
            <p className="text-xs text-slate-500 -mt-2">
              Set through environment variables on the API and worker containers; shown here as they are running.
            </p>
            <div className="divide-y divide-slate-100 text-xs">
              {Object.entries(config.pipeline).map(([key, value]) => (
                <div key={key} className="py-2 flex items-center justify-between gap-3">
                  <div>
                    <div className="font-semibold text-slate-800">{PIPELINE_LABELS[key]?.[0] ?? key}</div>
                    {PIPELINE_LABELS[key] && <div className="text-3xs font-mono text-slate-400">{PIPELINE_LABELS[key][1]}</div>}
                  </div>
                  <span className="font-mono text-slate-900">{typeof value === "boolean" ? (value ? "on" : "off") : value}</span>
                </div>
              ))}
              <div className="py-2 flex items-center justify-between gap-3">
                <div>
                  <div className="font-semibold text-slate-800">LLM model</div>
                  <div className="text-3xs font-mono text-slate-400">OLLAMA_MODEL</div>
                </div>
                <span className="font-mono text-slate-900">
                  {config.llm.provider} / {config.llm.model}
                </span>
              </div>
            </div>
          </div>

          {/* Tools & integrations */}
          <div className="card p-6 space-y-4">
            <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
              <Wrench className="w-5 h-5 text-brand-600" />
              Tools and integrations
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {Object.entries(config.tools).map(([tool, installed]) => (
                <Flag key={tool} ok={installed} label={`${tool}${installed ? "" : " (not installed)"}`} />
              ))}
              <Flag
                ok={config.integrations.infracost_api_key_set}
                label={config.integrations.infracost_api_key_set ? "Infracost API key set" : "Infracost API key not set"}
              />
            </div>
            <p className="text-2xs text-slate-500 flex items-center gap-1.5">
              <GitBranch className="w-3.5 h-3.5" />
              GitHub tokens are entered per pull request and never stored.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
