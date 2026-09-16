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
  Network
} from "lucide-react";

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
  const [region, setRegion] = useState("us-east-1");
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
      };

      const result = await initiateScan(payload);
      router.push(`/scan/${result.job_id}`);
    } catch (err: any) {
      setError(err.message || "Failed to initiate scan");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-7">
      {/* Safety Notice Banner */}
      <div className="p-4 rounded-xl bg-gradient-to-r from-brand-50/90 to-indigo-50/60 border border-brand-200/80 text-xs text-brand-950 flex items-start gap-3.5 shadow-2xs">
        <div className="p-1.5 rounded-lg bg-brand-600 text-white shrink-0 mt-0.5 shadow-xs">
          <Shield className="w-4 h-4" />
        </div>
        <div className="space-y-1">
          <span className="font-bold text-slate-900 block text-sm">Strict Read-Only Guarantee</span>
          <p className="text-slate-600 leading-relaxed">
            Credentials reside strictly in ephemeral memory for read-only AWS APIs (<code className="text-brand-700 bg-white/80 px-1 py-0.5 rounded border border-brand-200/50">Describe*</code>, <code className="text-brand-700 bg-white/80 px-1 py-0.5 rounded border border-brand-200/50">Get*</code>, <code className="text-brand-700 bg-white/80 px-1 py-0.5 rounded border border-brand-200/50">List*</code>). They are never saved to disk, logged, or sent to LLMs.
          </p>
        </div>
      </div>

      {error && (
        <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800 flex items-start gap-3 animate-in fade-in duration-200 shadow-xs">
          <AlertCircle className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
          <span className="font-medium">{error}</span>
        </div>
      )}

      {/* Credentials Section */}
      <div className="space-y-4">
        <div className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
          <Key className="w-3.5 h-3.5 text-brand-600" />
          <span>AWS Authentication</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Access Key */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700 flex items-center justify-between">
              <span>AWS Access Key ID <span className="text-rose-500">*</span></span>
            </label>
            <input
              type="text"
              required
              placeholder="AKIAIOSFODNN7EXAMPLE"
              value={accessKey}
              onChange={(e) => setAccessKey(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-xl bg-white border border-slate-200/90 text-slate-900 placeholder:text-slate-400 text-xs font-mono shadow-2xs focus:border-brand-600 focus:ring-3 focus:ring-brand-500/15 outline-none transition-all"
            />
          </div>

          {/* Secret Key */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700 flex items-center justify-between">
              <span>AWS Secret Access Key <span className="text-rose-500">*</span></span>
              <button
                type="button"
                onClick={() => setShowSecret(!showSecret)}
                className="text-[11px] text-brand-600 hover:text-brand-700 font-medium inline-flex items-center gap-1"
              >
                {showSecret ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                {showSecret ? "Hide" : "Show"}
              </button>
            </label>
            <div className="relative">
              <input
                type={showSecret ? "text" : "password"}
                required
                placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                className="w-full px-3.5 py-2.5 rounded-xl bg-white border border-slate-200/90 text-slate-900 placeholder:text-slate-400 text-xs font-mono shadow-2xs focus:border-brand-600 focus:ring-3 focus:ring-brand-500/15 outline-none transition-all"
              />
            </div>
          </div>
        </div>

        {/* Session Token & Region */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700 flex items-center gap-1">
              <span>AWS Session Token</span>
              <span className="text-[10px] text-slate-400 font-normal">(Optional STS)</span>
            </label>
            <input
              type="password"
              placeholder="AQoDYXdzEJr1... (optional)"
              value={sessionToken}
              onChange={(e) => setSessionToken(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-xl bg-white border border-slate-200/90 text-slate-900 placeholder:text-slate-400 text-xs font-mono shadow-2xs focus:border-brand-600 focus:ring-3 focus:ring-brand-500/15 outline-none transition-all"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700 flex items-center gap-1.5">
              <Globe className="w-3.5 h-3.5 text-brand-600" />
              <span>Target Region <span className="text-rose-500">*</span></span>
            </label>
            <select
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-xl bg-white border border-slate-200/90 text-slate-900 text-xs shadow-2xs focus:border-brand-600 focus:ring-3 focus:ring-brand-500/15 outline-none transition-all cursor-pointer"
            >
              {AWS_REGIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Operation Mode */}
      <div className="space-y-2.5">
        <label className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
          <Sparkles className="w-3.5 h-3.5 text-brand-600" />
          <span>Pipeline Mode</span>
        </label>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {OPERATION_MODES.map((op) => {
            const Icon = op.icon;
            const isSelected = operation === op.id;
            return (
              <label
                key={op.id}
                onClick={() => setOperation(op.id)}
                className={`relative flex flex-col p-3.5 rounded-xl border text-left cursor-pointer transition-all ${
                  isSelected
                    ? "bg-brand-50/60 border-brand-500 ring-2 ring-brand-500/20 shadow-xs"
                    : "bg-white border-slate-200/90 hover:border-slate-300 hover:bg-slate-50/50 shadow-2xs"
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className={`p-2 rounded-lg ${isSelected ? "bg-brand-600 text-white" : "bg-slate-100 text-slate-600"}`}>
                    <Icon className="w-4 h-4" />
                  </div>
                  <div className={`w-3.5 h-3.5 rounded-full border flex items-center justify-center ${isSelected ? "border-brand-600 bg-brand-600" : "border-slate-300"}`}>
                    {isSelected && <div className="w-1.5 h-1.5 rounded-full bg-white" />}
                  </div>
                </div>
                <span className={`text-xs font-bold ${isSelected ? "text-brand-900" : "text-slate-800"}`}>
                  {op.title}
                </span>
                <span className="text-[11px] text-slate-500 mt-0.5 leading-snug">
                  {op.desc}
                </span>
              </label>
            );
          })}
        </div>
      </div>

      {/* Resource Filters */}
      <div className="space-y-2.5">
        <div className="flex items-center justify-between">
          <label className="text-xs font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5">
            <Layers className="w-3.5 h-3.5 text-brand-600" />
            <span>Target AWS Resource Types</span>
          </label>
          <button
            type="button"
            onClick={handleSelectAllResources}
            className="text-xs text-brand-600 hover:text-brand-700 font-semibold"
          >
            {selectedResources.length === RESOURCE_OPTIONS.length ? "Deselect All" : "Select All"}
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {RESOURCE_OPTIONS.map((res) => {
            const Icon = res.icon;
            const isChecked = selectedResources.includes(res.id);
            return (
              <button
                type="button"
                key={res.id}
                onClick={() => toggleResource(res.id)}
                className={`flex items-center gap-3 p-3 rounded-xl border text-left transition-all ${
                  isChecked
                    ? "bg-brand-50/50 border-brand-500/70 text-slate-900 shadow-2xs"
                    : "bg-white border-slate-200/90 text-slate-500 hover:border-slate-300 shadow-2xs"
                }`}
              >
                <div
                  className={`w-4 h-4 rounded-md flex items-center justify-center border text-[10px] font-bold shrink-0 transition-colors ${
                    isChecked
                      ? "bg-brand-600 border-brand-600 text-white"
                      : "border-slate-300 bg-white"
                  }`}
                >
                  {isChecked && "✓"}
                </div>
                <div className="min-w-0">
                  <div className="font-bold text-xs text-slate-800 truncate">{res.id}</div>
                  <div className="text-[10px] text-slate-500 truncate">{res.label}</div>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Submit Action */}
      <div className="pt-2">
        <button
          type="submit"
          disabled={isLoading || !accessKey || !secretKey}
          className="w-full py-3.5 px-6 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 active:scale-[0.99] disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none text-white font-bold shadow-lg shadow-brand-500/25 flex items-center justify-center gap-2.5 text-sm transition-all"
        >
          {isLoading ? (
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              <span>Orchestrating 8-Agent Pipeline...</span>
            </div>
          ) : (
            <>
              <span>Launch TerraAgent Scan</span>
              <ArrowRight className="w-4 h-4 group-hover:translate-x-0.5 transition-transform" />
            </>
          )}
        </button>
      </div>
    </form>
  );
}
