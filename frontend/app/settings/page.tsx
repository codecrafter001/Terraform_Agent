import { Settings, Shield, Key, GitBranch, Cpu } from "lucide-react";
import { PageHeader } from "@/components/ui";
import SampleDataBanner from "@/components/SampleDataBanner";

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Settings" }]}
        title={
          <div className="flex items-center gap-3">
            <Settings className="w-6 h-6 text-brand-600" />
            <span>TerraAgent System Settings</span>
          </div>
        }
        meta="Configure AWS read-only assume roles, GitHub integrations, LLM models, and safety thresholds."
      />

      <SampleDataBanner feature="Settings" />

      <div className="space-y-6 max-w-4xl">
        {/* Safety & Guardrails */}
        <div className="card p-6 space-y-4">
          <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
            <Shield className="w-5 h-5 text-emerald-600" />
            Zero-Mutation & Read-Only Safety Policies
          </div>
          <p className="text-xs text-slate-600 leading-relaxed">
            TerraAgent enforces strict read-only execution guardrails. The backend API is hardwired to reject any direct cloud modification.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 text-xs">
            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
              <span className="font-semibold text-slate-800">Terraform Apply</span>
              <span className="text-2xs font-bold text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-200">BLOCKED</span>
            </div>
            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
              <span className="font-semibold text-slate-800">Terraform Destroy</span>
              <span className="text-2xs font-bold text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-200">BLOCKED</span>
            </div>
            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
              <span className="font-semibold text-slate-800">Data Read Denials (S3/Secrets)</span>
              <span className="text-2xs font-bold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">ACTIVE</span>
            </div>
            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 flex items-center justify-between">
              <span className="font-semibold text-slate-800">Deterministic Import Verification</span>
              <span className="text-2xs font-bold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">ACTIVE</span>
            </div>
          </div>
        </div>

        {/* Engine Configuration */}
        <div className="card p-6 space-y-4">
          <div className="flex items-center gap-2.5 text-sm font-bold text-slate-900">
            <Cpu className="w-5 h-5 text-brand-600" />
            4-Agent Multi-Agent Engine
          </div>
          <div className="text-xs space-y-3 text-slate-700">
            <div className="flex items-center justify-between pb-2 border-b border-slate-100">
              <span className="text-slate-600">LangGraph Version:</span>
              <span className="font-mono font-bold text-slate-900">1.2.11 (with interrupt risk gates)</span>
            </div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-100">
              <span className="text-slate-600">IaC Engine:</span>
              <span className="font-mono font-bold text-slate-900">Terraform / OpenTofu 1.8+</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Intent Analysis Model:</span>
              <span className="font-mono font-bold text-slate-900">Gemini 2.5 Flash + Deterministic NLP Fallback</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
