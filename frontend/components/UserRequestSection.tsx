"use client";

import { useState } from "react";
import {
  Sparkles,
  Layers,
  Globe,
  Server,
  Database,
  HardDrive,
  Network,
  Shield,
  Lock,
  Cpu,
  ArrowRight,
  Loader2,
  Terminal,
  Zap,
  CheckCircle2,
} from "lucide-react";
import { IntentAnalysisResult } from "@/lib/types";
import { analyzeIntent } from "@/lib/api";

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

const ENVIRONMENTS = [
  { id: "production", label: "Production", color: "text-rose-700 bg-rose-50 border-rose-200" },
  { id: "staging", label: "Staging", color: "text-amber-700 bg-amber-50 border-amber-200" },
  { id: "development", label: "Development", color: "text-emerald-700 bg-emerald-50 border-emerald-200" },
  { id: "qa", label: "QA / Test", color: "text-blue-700 bg-blue-50 border-blue-200" },
  { id: "sandbox", label: "Sandbox", color: "text-purple-700 bg-purple-50 border-purple-200" },
];

const RESOURCE_OPTIONS = [
  { id: "EC2", label: "EC2 Instances", icon: Server, color: "text-blue-600", bg: "bg-blue-50" },
  { id: "ECS", label: "ECS / Fargate", icon: Cpu, color: "text-indigo-600", bg: "bg-indigo-50" },
  { id: "VPC", label: "VPC & Subnets", icon: Network, color: "text-purple-600", bg: "bg-purple-50" },
  { id: "SG", label: "Security Groups", icon: Lock, color: "text-rose-600", bg: "bg-rose-50" },
  { id: "S3", label: "S3 Buckets", icon: HardDrive, color: "text-emerald-600", bg: "bg-emerald-50" },
  { id: "RDS", label: "RDS Databases", icon: Database, color: "text-amber-600", bg: "bg-amber-50" },
  { id: "IAM", label: "IAM Roles", icon: Shield, color: "text-pink-600", bg: "bg-pink-50" },
];

const PROMPT_TEMPLATES = [
  {
    title: "Scale EC2 & Fargate",
    prompt: "Increase EC2 web server from t2.micro to t2.medium and scale Fargate from 2 to 4 tasks.",
  },
  {
    title: "Resize RDS Database",
    prompt: "Resize RDS PostgreSQL instance from db.t3.small to db.t3.large and upgrade storage to 50GB.",
  },
  {
    title: "S3 Bucket Encryption",
    prompt: "Enable S3 bucket versioning and SSE-AES256 server-side encryption for data-lake bucket.",
  },
  {
    title: "Create Secure VPC",
    prompt: "Create a new VPC with 3 public subnets, NAT gateway, and internet gateway in us-east-1.",
  },
];

interface UserRequestSectionProps {
  region: string;
  onRegionChange: (region: string) => void;
  environment: string;
  onEnvironmentChange: (env: string) => void;
  selectedResources: string[];
  onToggleResource: (resId: string) => void;
  onSelectAllResources: () => void;
  userRequest: string;
  onUserRequestChange: (req: string) => void;
  onAnalyzeComplete: (result: IntentAnalysisResult) => void;
}

export default function UserRequestSection({
  region,
  onRegionChange,
  environment,
  onEnvironmentChange,
  selectedResources,
  onToggleResource,
  onSelectAllResources,
  userRequest,
  onUserRequestChange,
  onAnalyzeComplete,
}: UserRequestSectionProps) {
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);

  const handleAnalyze = async () => {
    if (!userRequest.trim()) {
      setAnalysisError("Please enter a natural-language infrastructure request.");
      return;
    }

    setIsAnalyzing(true);
    setAnalysisError(null);

    try {
      const result = await analyzeIntent({
        user_request: userRequest,
        region,
        environment,
        resource_filters: selectedResources,
      });
      onAnalyzeComplete(result);
    } catch (err: unknown) {
      setAnalysisError((err instanceof Error && err.message) || "Failed to analyze request intent");
    } finally {
      setIsAnalyzing(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Target Environment & Region */}
      <div className="card p-5 space-y-4">
        <div className="flex items-center gap-2.5 pb-2 border-b border-slate-100">
          <div className="p-1.5 rounded-lg bg-brand-50 text-brand-600">
            <Globe className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-slate-900 uppercase tracking-wide">Target Scope & Environment</h3>
            <p className="text-2xs text-slate-500">Configure target AWS region and environment classification</p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Region */}
          <div className="space-y-1.5">
            <label htmlFor="user-aws-region" className="text-xs font-medium text-slate-700 block">
              AWS Region <span className="text-rose-500">*</span>
            </label>
            <select
              id="user-aws-region"
              value={region}
              onChange={(e) => onRegionChange(e.target.value)}
              className="field-input cursor-pointer"
            >
              <option value={AUTO_REGION}>Auto: find my resources (Resource Explorer)</option>
              {AWS_REGIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </div>

          {/* Environment */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700 block">
              Environment <span className="text-rose-500">*</span>
            </label>
            <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-5 gap-1.5">
              {ENVIRONMENTS.map((env) => {
                const isSelected = environment === env.id;
                return (
                  <button
                    key={env.id}
                    type="button"
                    onClick={() => onEnvironmentChange(env.id)}
                    className={`px-2 py-2 rounded-xl text-3xs sm:text-2xs font-bold border transition-all text-center truncate ${
                      isSelected
                        ? `${env.color} ring-2 ring-brand-500/20 shadow-2xs`
                        : "bg-slate-50 text-slate-600 border-slate-200 hover:bg-slate-100"
                    }`}
                  >
                    {env.label}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Resource Selection */}
      <div className="card p-5 space-y-4">
        <div className="flex items-center justify-between pb-2 border-b border-slate-100">
          <div className="flex items-center gap-2.5">
            <div className="p-1.5 rounded-lg bg-indigo-50 text-indigo-600">
              <Layers className="w-4 h-4" />
            </div>
            <div>
              <h3 className="text-xs font-bold text-slate-900 uppercase tracking-wide">Resource Selection</h3>
              <p className="text-2xs text-slate-500">Filter AWS services relevant to this user request</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onSelectAllResources}
            className="text-xs text-brand-600 hover:text-brand-700 font-semibold"
          >
            {selectedResources.length === RESOURCE_OPTIONS.length ? "Deselect all" : "Select all"}
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2.5">
          {RESOURCE_OPTIONS.map((res) => {
            const Icon = res.icon;
            const isChecked = selectedResources.includes(res.id);
            return (
              <button
                type="button"
                key={res.id}
                onClick={() => onToggleResource(res.id)}
                className={`flex flex-col items-center p-2.5 rounded-xl border text-center transition-all ${
                  isChecked
                    ? "bg-white border-brand-500/80 ring-2 ring-brand-500/10 shadow-2xs"
                    : "bg-slate-50/70 border-slate-200 opacity-60 hover:opacity-100"
                }`}
              >
                <div className={`p-1.5 rounded-lg mb-1.5 ${isChecked ? `${res.bg} ${res.color}` : "bg-slate-100 text-slate-400"}`}>
                  <Icon className="w-4 h-4" />
                </div>
                <span className="font-bold text-2xs text-slate-900">{res.id}</span>
                <span className="text-3xs text-slate-500 truncate max-w-full">{res.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Natural Language Request Box */}
      <div className="card p-5 space-y-4">
        <div className="flex items-center gap-2.5 pb-2 border-b border-slate-100">
          <div className="p-1.5 rounded-lg bg-emerald-50 text-emerald-600">
            <Terminal className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-slate-900 uppercase tracking-wide">AWS Natural-Language Request</h3>
            <p className="text-2xs text-slate-500">
              Describe the infrastructure modification, scaling, generation, or fix in plain English
            </p>
          </div>
        </div>

        {/* Prompt Templates */}
        <div className="space-y-1.5">
          <div className="text-2xs font-semibold text-slate-500 flex items-center gap-1.5">
            <Zap className="w-3 h-3 text-amber-500" />
            Quick Example Requests:
          </div>
          <div className="flex flex-wrap gap-1.5">
            {PROMPT_TEMPLATES.map((tpl, i) => (
              <button
                key={i}
                type="button"
                onClick={() => onUserRequestChange(tpl.prompt)}
                className="text-2xs px-2.5 py-1 rounded-lg bg-slate-100 hover:bg-brand-50 text-slate-700 hover:text-brand-700 border border-slate-200 hover:border-brand-200 transition-colors text-left"
              >
                {tpl.title}
              </button>
            ))}
          </div>
        </div>

        {/* Textarea */}
        <div className="space-y-2">
          <textarea
            rows={4}
            value={userRequest}
            onChange={(e) => onUserRequestChange(e.target.value)}
            placeholder="e.g. Increase EC2 web server from t2.micro to t2.medium and scale Fargate from 2 to 4 tasks."
            className="field-input w-full font-mono text-xs leading-relaxed resize-y p-3.5 focus:ring-2 focus:ring-brand-500/20"
          />

          {analysisError && (
            <p className="text-xs text-rose-600 font-medium">{analysisError}</p>
          )}
        </div>

        {/* Action Button */}
        <div className="pt-1 flex items-center justify-between">
          <p className="text-2xs text-slate-500">
            Press <strong className="font-semibold text-slate-700">Analyze Request</strong> to review operation intent, target resources, and parameter diffs before execution.
          </p>

          <button
            type="button"
            onClick={handleAnalyze}
            disabled={isAnalyzing || !userRequest.trim()}
            className="btn-primary py-2.5 px-5 text-xs flex items-center gap-2 font-bold shadow-sm"
          >
            {isAnalyzing ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>Analyzing Request...</span>
              </>
            ) : (
              <>
                <Sparkles className="w-4 h-4" />
                <span>Analyze Request</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
