"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { initiateScan, ScanRequestPayload } from "@/lib/api";
import {
  Shield,
  Key,
  Globe,
  Layers,
  AlertCircle,
  ArrowRight,
  Eye,
  EyeOff,
  Sparkles,
  Search,
  HelpCircle,
  CheckCircle,
  Server,
  Database,
  Lock,
  HardDrive,
  Network,
  Cpu,
} from "lucide-react";

const AUTO_REGION = "auto";

const AWS_REGIONS = [
  { value: "us-east-1", label: "US East (N. Virginia) — us-east-1" },
  { value: "us-east-2", label: "US East (Ohio) — us-east-2" },
  { value: "us-west-1", label: "US West (N. California) — us-west-1" },
  { value: "us-west-2", label: "US West (Oregon) — us-west-2" },
  { value: "eu-central-1", label: "Europe (Frankfurt) — eu-central-1" },
  { value: "eu-west-1", label: "Europe (Ireland) — eu-west-1" },
  { value: "eu-west-2", label: "Europe (London) — eu-west-2" },
  { value: "eu-west-3", label: "Europe (Paris) — eu-west-3" },
  { value: "ap-south-1", label: "Asia Pacific (Mumbai) — ap-south-1" },
  { value: "ap-southeast-1", label: "Asia Pacific (Singapore) — ap-southeast-1" },
  { value: "ap-southeast-2", label: "Asia Pacific (Sydney) — ap-southeast-2" },
  { value: "ap-northeast-1", label: "Asia Pacific (Tokyo) — ap-northeast-1" },
  { value: "ca-central-1", label: "Canada (Central) — ca-central-1" },
  { value: "sa-east-1", label: "South America (São Paulo) — sa-east-1" },
];

const OPERATION_MODES = [
  {
    id: "generate",
    title: "Generate HCL",
    desc: "Discover & synthesize full Terraform code",
    icon: Sparkles,
  },
  {
    id: "scan",
    title: "Scan Only",
    desc: "Read-only inventory & topology mapping",
    icon: Search,
  },
  {
    id: "explain",
    title: "Explain Graph",
    desc: "Analyze topology & cross-service links",
    icon: HelpCircle,
  },
  {
    id: "validate",
    title: "Validate & Policy",
    desc: "fmt, validate & run tfsec/OPA rules",
    icon: CheckCircle,
  },
] as const;

const RESOURCE_OPTIONS = [
  { id: "EC2", label: "Instances & AMIs", icon: Server, color: "text-blue-600", bg: "bg-blue-50" },
  { id: "VPC", label: "VPC & Subnets", icon: Network, color: "text-purple-600", bg: "bg-purple-50" },
  { id: "SG", label: "Security Groups", icon: Lock, color: "text-rose-600", bg: "bg-rose-50" },
  { id: "S3", label: "S3 Buckets", icon: HardDrive, color: "text-emerald-600", bg: "bg-emerald-50" },
  { id: "RDS", label: "RDS Databases", icon: Database, color: "text-amber-600", bg: "bg-amber-50" },
  { id: "IAM", label: "IAM Roles & Policies", icon: Shield, color: "text-pink-600", bg: "bg-pink-50" },
];

export default function CredentialForm() {
  const router = useRouter();
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const [region, setRegion] = useState(AUTO_REGION);
  const [useResourceExplorer, setUseResourceExplorer] = useState(true);
  const [terraformBinary, setTerraformBinary] = useState<"terraform" | "tofu">("terraform");
  const [operation, setOperation] = useState<"generate" | "scan" | "explain" | "validate">("generate");
  const [selectedResources, setSelectedResources] = useState<string[]>(["EC2", "VPC", "SG", "S3", "RDS", "IAM"]);
  const [showSecret, setShowSecret] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleResource = (id: string) => {
    setSelectedResources((prev) =>
      prev.includes(id) ? prev.filter((r) => r !== id) : [...prev, id]
    );
  };

  const handleSelectAllResources = () => {
    if (selectedResources.length === RESOURCE_OPTIONS.length) {
      setSelectedResources([]);
    } else {
      setSelectedResources(RESOURCE_OPTIONS.map((r) => r.id));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      const payload: ScanRequestPayload = {
        aws_access_key: accessKey,
        aws_secret_key: secretKey,
        aws_session_token: sessionToken || undefined,
        region,
        operation,
        resource_filters: selectedResources,
        terraform_binary: terraformBinary,
        // "auto" needs Resource Explorer to find the region.
        use_resource_explorer: useResourceExplorer || region === AUTO_REGION,
      };

      const result = await initiateScan(payload);
      router.push(`/scan/${result.job_id}`);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Failed to initiate scan");
    } finally {
      setIsLoading(false);
    }
  };

  const selectedMode = OPERATION_MODES.find((m) => m.id === operation);
  const hasCredentials = accessKey.length > 0 && secretKey.length > 0;
  const canSubmit = !isLoading && hasCredentials && selectedResources.length > 0;

  return (
    <form onSubmit={handleSubmit} className="grid grid-cols-1 xl:grid-cols-12 gap-6 items-start">
      <div className="xl:col-span-8 space-y-5">
        {/* 1. Credentials */}
        <FormSection
          step={1}
          icon={Key}
          title="AWS credentials"
          description="Use a read-only IAM user or short-lived STS credentials."
        >
          <div className="p-3.5 rounded-xl bg-brand-50/70 border border-brand-100 text-xs flex items-start gap-3">
            <Shield className="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
            <p className="text-slate-600 leading-relaxed">
              Credentials are kept in memory only and used for read-only AWS calls (
              <code className="text-brand-700 font-mono">Describe*</code>,{" "}
              <code className="text-brand-700 font-mono">Get*</code>,{" "}
              <code className="text-brand-700 font-mono">List*</code>). They are never saved to disk, logged, or sent
              to an LLM.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="aws-access-key" className="text-xs font-medium text-slate-700 block">
                Access key ID <span className="text-rose-500">*</span>
              </label>
              <input
                id="aws-access-key"
                type="text"
                required
                autoComplete="off"
                spellCheck={false}
                placeholder="AKIAIOSFODNN7EXAMPLE"
                value={accessKey}
                onChange={(e) => setAccessKey(e.target.value)}
                className="field-input font-mono"
              />
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <label htmlFor="aws-secret-key" className="text-xs font-medium text-slate-700">
                  Secret access key <span className="text-rose-500">*</span>
                </label>
                <button
                  type="button"
                  onClick={() => setShowSecret(!showSecret)}
                  className="text-2xs text-brand-600 hover:text-brand-700 font-medium inline-flex items-center gap-1"
                >
                  {showSecret ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                  {showSecret ? "Hide" : "Show"}
                </button>
              </div>
              <input
                id="aws-secret-key"
                type={showSecret ? "text" : "password"}
                required
                autoComplete="off"
                spellCheck={false}
                placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                className="field-input font-mono"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="aws-session-token" className="text-xs font-medium text-slate-700 flex items-center gap-1.5">
              Session token <span className="text-2xs text-slate-400 font-normal">optional, for STS credentials</span>
            </label>
            <input
              id="aws-session-token"
              type="password"
              autoComplete="off"
              placeholder="AQoDYXdzEJr1..."
              value={sessionToken}
              onChange={(e) => setSessionToken(e.target.value)}
              className="field-input font-mono"
            />
          </div>
        </FormSection>

        {/* 2. Target */}
        <FormSection
          step={2}
          icon={Globe}
          title="Target & engine"
          description="Where to scan, and which IaC dialect to validate against."
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label htmlFor="aws-region" className="text-xs font-medium text-slate-700 block">
                Region <span className="text-rose-500">*</span>
              </label>
              <select
                id="aws-region"
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                className="field-input cursor-pointer"
              >
                <option value={AUTO_REGION}>Auto: find my resources (Resource Explorer)</option>
                {AWS_REGIONS.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
              {region === AUTO_REGION && (
                <p className="text-2xs text-slate-500 leading-relaxed">
                  Scans the region that holds the most supported resources. Falls back to us-east-1 if Resource
                  Explorer isn&apos;t turned on in the account.
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <span className="text-xs font-medium text-slate-700 block">IaC engine</span>
              <div className="grid grid-cols-2 gap-1 p-1 rounded-xl bg-slate-100">
                {(
                  [
                    { id: "terraform", label: "Terraform" },
                    { id: "tofu", label: "OpenTofu" },
                  ] as const
                ).map((engine) => (
                  <button
                    key={engine.id}
                    type="button"
                    onClick={() => setTerraformBinary(engine.id)}
                    aria-pressed={terraformBinary === engine.id}
                    className={`py-2 rounded-lg text-xs font-semibold transition-all ${
                      terraformBinary === engine.id
                        ? "bg-white text-slate-900 shadow-xs"
                        : "text-slate-500 hover:text-slate-800"
                    }`}
                  >
                    {engine.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <label className="flex items-start gap-3 p-3.5 rounded-xl border border-slate-200 bg-slate-50/60 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-brand-600"
              checked={useResourceExplorer || region === AUTO_REGION}
              disabled={region === AUTO_REGION}
              onChange={(e) => setUseResourceExplorer(e.target.checked)}
            />
            <span className="text-xs text-slate-700 leading-relaxed">
              <span className="font-semibold text-slate-900">All-region inventory with AWS Resource Explorer</span>
              <span className="block text-2xs text-slate-500 mt-0.5">
                Read-only (<code className="font-mono">ListIndexes</code>, <code className="font-mono">Search</code>).
                Resource Explorer must already be turned on in the account. TerraAgent never turns it on for you.
              </span>
            </span>
          </label>
        </FormSection>

        {/* 3. Mode */}
        <FormSection step={3} icon={Sparkles} title="Pipeline mode" description="How far the agent pipeline should go.">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3" role="radiogroup" aria-label="Pipeline mode">
            {OPERATION_MODES.map((op) => {
              const Icon = op.icon;
              const isSelected = operation === op.id;
              return (
                <button
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  key={op.id}
                  onClick={() => setOperation(op.id)}
                  className={`flex items-start gap-3 p-3.5 rounded-xl border text-left transition-all ${
                    isSelected
                      ? "bg-brand-50/60 border-brand-500 ring-3 ring-brand-500/15"
                      : "bg-white border-slate-200 hover:border-slate-300 hover:bg-slate-50/60"
                  }`}
                >
                  <div
                    className={`p-2 rounded-lg shrink-0 ${
                      isSelected ? "bg-brand-600 text-white" : "bg-slate-100 text-slate-600"
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className={`text-xs font-bold ${isSelected ? "text-brand-900" : "text-slate-800"}`}>
                      {op.title}
                    </div>
                    <div className="text-2xs text-slate-500 mt-0.5 leading-snug">{op.desc}</div>
                  </div>
                  <div
                    className={`w-4 h-4 mt-0.5 rounded-full border flex items-center justify-center shrink-0 ${
                      isSelected ? "border-brand-600 bg-brand-600" : "border-slate-300"
                    }`}
                  >
                    {isSelected && <div className="w-1.5 h-1.5 rounded-full bg-white" />}
                  </div>
                </button>
              );
            })}
          </div>
        </FormSection>

        {/* 4. Resources */}
        <FormSection
          step={4}
          icon={Layers}
          title="Resource types"
          description="Only these services will be inventoried."
          action={
            <button
              type="button"
              onClick={handleSelectAllResources}
              className="text-xs text-brand-600 hover:text-brand-700 font-semibold shrink-0"
            >
              {selectedResources.length === RESOURCE_OPTIONS.length ? "Deselect all" : "Select all"}
            </button>
          }
        >
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {RESOURCE_OPTIONS.map((res) => {
              const Icon = res.icon;
              const isChecked = selectedResources.includes(res.id);
              return (
                <button
                  type="button"
                  key={res.id}
                  onClick={() => toggleResource(res.id)}
                  aria-pressed={isChecked}
                  className={`flex items-center gap-3 p-3 rounded-xl border text-left transition-all ${
                    isChecked
                      ? "bg-white border-brand-500/70 ring-3 ring-brand-500/10"
                      : "bg-slate-50/60 border-slate-200 opacity-70 hover:opacity-100"
                  }`}
                >
                  <div
                    className={`p-1.5 rounded-lg shrink-0 ${
                      isChecked ? `${res.bg} ${res.color}` : "bg-slate-100 text-slate-400"
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="font-bold text-xs text-slate-800 truncate">{res.id}</div>
                    <div className="text-3xs text-slate-500 truncate">{res.label}</div>
                  </div>
                  <div
                    className={`w-4 h-4 rounded-md flex items-center justify-center border text-3xs font-bold shrink-0 transition-colors ${
                      isChecked ? "bg-brand-600 border-brand-600 text-white" : "border-slate-300 bg-white"
                    }`}
                  >
                    {isChecked && "✓"}
                  </div>
                </button>
              );
            })}
          </div>
        </FormSection>
      </div>

      {/* Summary + launch */}
      <aside className="xl:col-span-4 xl:sticky xl:top-8 space-y-4">
        <div className="card overflow-hidden">
          <div className="card-header">
            <h3 className="text-sm font-bold text-slate-900">Scan summary</h3>
          </div>
          <dl className="p-5 space-y-3 text-xs">
            <SummaryRow label="Credentials" ok={hasCredentials}>
              {hasCredentials ? `Provided${sessionToken ? " (STS)" : ""}` : "Not provided"}
            </SummaryRow>
            <SummaryRow label="Region" ok>
              {region === AUTO_REGION ? "Auto (Resource Explorer)" : <span className="font-mono">{region}</span>}
            </SummaryRow>
            <SummaryRow label="Engine" ok>
              {terraformBinary === "tofu" ? "OpenTofu" : "Terraform"}
            </SummaryRow>
            <SummaryRow label="Mode" ok>
              {selectedMode?.title}
            </SummaryRow>
            <SummaryRow label="Resources" ok={selectedResources.length > 0}>
              {selectedResources.length === 0
                ? "None selected"
                : selectedResources.length === RESOURCE_OPTIONS.length
                ? "All types"
                : selectedResources.join(", ")}
            </SummaryRow>
          </dl>

          <div className="p-5 pt-0 space-y-3">
            {error && (
              <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800 flex items-start gap-2.5 animate-fade-in">
                <AlertCircle className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
                <span className="font-medium">{error}</span>
              </div>
            )}
            <button type="submit" disabled={!canSubmit} className="btn-primary w-full py-3 text-sm group">
              {isLoading ? (
                <>
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  <span>Starting pipeline...</span>
                </>
              ) : (
                <>
                  <span>Launch scan</span>
                  <ArrowRight className="w-4 h-4 group-hover:translate-x-0.5 transition-transform" />
                </>
              )}
            </button>
            <p className="text-3xs text-slate-400 text-center leading-relaxed">
              Nothing in your AWS account is created, changed, or deleted.
            </p>
          </div>
        </div>

        <div className="card p-4 flex items-start gap-3">
          <div className="p-2 rounded-lg bg-slate-100 text-slate-600 shrink-0">
            <Cpu className="w-4 h-4" />
          </div>
          <p className="text-2xs text-slate-500 leading-relaxed">
            HCL is generated by a local Ollama model, then checked with <code className="font-mono">fmt</code>,{" "}
            <code className="font-mono">validate</code>, tfsec, Checkov, Trivy and OPA before anything is packaged.
          </p>
        </div>
      </aside>
    </form>
  );
}

function FormSection({
  step,
  icon: Icon,
  title,
  description,
  action,
  children,
}: {
  step: number;
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="card-header">
        <div className="flex items-center gap-3 min-w-0">
          <span className="w-6 h-6 rounded-full bg-slate-900 text-white text-2xs font-bold flex items-center justify-center shrink-0">
            {step}
          </span>
          <div className="min-w-0">
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-1.5">
              <Icon className="w-3.5 h-3.5 text-brand-600" />
              {title}
            </h2>
            {description && <p className="text-2xs text-slate-500 mt-0.5">{description}</p>}
          </div>
        </div>
        {action}
      </div>
      <div className="p-5 space-y-4">{children}</div>
    </section>
  );
}

function SummaryRow({ label, ok, children }: { label: string; ok: boolean; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-slate-500 flex items-center gap-1.5 shrink-0">
        <span className={`w-1.5 h-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-slate-300"}`} />
        {label}
      </dt>
      <dd className="text-slate-800 font-medium text-right min-w-0 break-words">{children}</dd>
    </div>
  );
}
