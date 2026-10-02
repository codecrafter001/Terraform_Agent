"use client";

import { useState, useRef } from "react";
import { useRouter } from "next/navigation";
import {
  FileArchive,
  GitBranch,
  Loader2,
  Rocket,
  ShieldCheck,
  UploadCloud,
  FileCode2,
  CheckCircle2,
  Trash2,
  Sparkles,
  ExternalLink,
} from "lucide-react";
import { createGithubDeployment, uploadDeploymentSource } from "@/lib/api";
import { PageHeader } from "@/components/ui";

const REGIONS = [
  "us-east-1", "us-east-2", "us-west-1", "us-west-2",
  "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
  "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
  "ca-central-1", "sa-east-1",
];

const MAX_UPLOAD_MB = 25;

type SourceKind = "zip" | "github";

interface ExamplePreset {
  name: string;
  type: string;
  repo: string;
  ref: string;
  description: string;
}

const SAMPLE_PRESETS: ExamplePreset[] = [
  {
    name: "Static Website (Vite + React)",
    type: "Static Site",
    repo: "vercel/next.js",
    ref: "canary",
    description: "CloudFront + S3 + OAC multi-version distribution",
  },
  {
    name: "Python API (FastAPI / Mangum)",
    type: "Serverless Lambda",
    repo: "tiangolo/fastapi",
    ref: "master",
    description: "API Gateway HTTP API + AWS Lambda with versioned alias",
  },
  {
    name: "Containerized Web Server",
    type: "ECS Fargate",
    repo: "docker-library/httpd",
    ref: "master",
    description: "AWS CodeBuild remote image build + ECS Fargate cluster & ALB",
  },
];

export default function NewDeploymentPage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState<SourceKind>("zip");
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("");
  const [token, setToken] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [environment, setEnvironment] = useState("production");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tooLarge = file !== null && file.size > MAX_UPLOAD_MB * 1024 * 1024;
  const ready = kind === "zip" ? file !== null && !tooLarge : repo.trim().length > 0;

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
    const droppedFile = e.dataTransfer.files?.[0];
    if (droppedFile) {
      if (droppedFile.name.endsWith(".zip") || droppedFile.type === "application/zip") {
        setFile(droppedFile);
        setError(null);
      } else {
        setError("Please upload a .zip archive file.");
      }
    }
  };

  const handleRepoChange = (value: string) => {
    // Smart GitHub URL parser
    let cleanRepo = value.trim();
    if (cleanRepo.startsWith("https://github.com/")) {
      const parts = cleanRepo.replace("https://github.com/", "").split("/");
      if (parts.length >= 2) {
        cleanRepo = `${parts[0]}/${parts[1]}`;
        if (parts[2] === "tree" && parts[3]) {
          setRef(parts.slice(3).join("/"));
        }
      }
    }
    setRepo(cleanRepo);
  };

  const applyPreset = (preset: ExamplePreset) => {
    setKind("github");
    setRepo(preset.repo);
    setRef(preset.ref);
    setError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setSubmitting(true);
    setError(null);
    try {
      const accepted =
        kind === "zip" && file
          ? await uploadDeploymentSource(file, region, environment)
          : await createGithubDeployment({
              repo: repo.trim(),
              ref: ref.trim() || undefined,
              github_token: token.trim() || undefined,
              region,
              environment,
            });
      setToken("");
      router.push(`/deployments/${accepted.deployment_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong creating the deployment");
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <PageHeader
        breadcrumbs={[{ label: "Deployments", href: "/deployments" }, { label: "New deployment" }]}
        title="Deploy Code to AWS"
        description="Upload a ZIP archive or connect a GitHub repository. TerraAgent analyzes project structure, generates validated Terraform, and provisions AWS resources safely."
      />

      <div className="p-4 rounded-2xl bg-amber-50 border border-amber-200 text-xs text-amber-900 flex gap-3 shadow-xs">
        <ShieldCheck className="w-5 h-5 shrink-0 mt-0.5 text-amber-600" />
        <div>
          <span className="font-semibold">Zero-Mutation Analysis:</span> Intake and analysis never create any live cloud infrastructure or require write credentials. You will review a generated Terraform plan and human approval gate before any deployment occurs.
        </div>
      </div>

      <form onSubmit={submit} className="card p-5 sm:p-6 space-y-6">
        {/* Source Selection Tabs */}
        <div>
          <label className="field-label mb-2 block text-xs font-bold text-slate-700 uppercase tracking-wider">
            Choose Source Method
          </label>
          <div className="grid grid-cols-2 gap-3" role="tablist" aria-label="Source">
            {([
              ["zip", "Upload ZIP File", FileArchive, "Drop or select a .zip archive"],
              ["github", "GitHub Repository", GitBranch, "Public or private GitHub repo"],
            ] as const).map(([value, label, Icon, subtitle]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={kind === value}
                onClick={() => {
                  setKind(value);
                  setError(null);
                }}
                className={`flex flex-col items-start p-3.5 rounded-xl border text-left transition-all ${
                  kind === value
                    ? "border-brand-600 bg-brand-50/70 text-brand-900 ring-2 ring-brand-500/20 shadow-xs"
                    : "border-slate-200 text-slate-600 hover:bg-slate-50 hover:border-slate-300"
                }`}
              >
                <div className="flex items-center gap-2 font-semibold text-xs mb-0.5">
                  <Icon className={`w-4 h-4 ${kind === value ? "text-brand-600" : "text-slate-400"}`} />
                  {label}
                </div>
                <span className="text-3xs text-slate-500">{subtitle}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Tab 1: ZIP Archive Upload */}
        {kind === "zip" && (
          <div className="space-y-3">
            <label className="field-label block font-semibold text-slate-800">
              Project Archive (.zip)
            </label>

            <div
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`relative flex flex-col items-center justify-center p-8 border-2 border-dashed rounded-2xl cursor-pointer transition-all ${
                isDragging
                  ? "border-brand-500 bg-brand-50/50 scale-[0.99]"
                  : file
                  ? "border-emerald-400 bg-emerald-50/30"
                  : "border-slate-200 hover:border-brand-400 hover:bg-slate-50/60"
              }`}
            >
              <input
                ref={fileInputRef}
                id="zip"
                type="file"
                accept=".zip,application/zip"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setError(null);
                }}
                className="hidden"
              />

              {file ? (
                <div className="flex flex-col items-center text-center space-y-2">
                  <div className="w-12 h-12 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600">
                    <CheckCircle2 className="w-6 h-6" />
                  </div>
                  <div>
                    <p className="text-xs font-semibold text-slate-900">{file.name}</p>
                    <p className="text-2xs text-slate-500">
                      {(file.size / (1024 * 1024)).toFixed(2)} MB • Ready to analyze
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setFile(null);
                    }}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 text-2xs font-medium text-rose-600 hover:bg-rose-50 rounded-lg transition-colors"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Remove file
                  </button>
                </div>
              ) : (
                <div className="flex flex-col items-center text-center space-y-2">
                  <div className="w-12 h-12 rounded-full bg-brand-50 flex items-center justify-center text-brand-600">
                    <UploadCloud className="w-6 h-6" />
                  </div>
                  <div>
                    <p className="text-xs font-semibold text-slate-800">
                      Drag and drop your project ZIP here, or <span className="text-brand-600 underline">browse</span>
                    </p>
                    <p className="text-2xs text-slate-400 mt-1">
                      Max file size: {MAX_UPLOAD_MB} MB. Supports Static Sites, Lambdas, and Dockerfiles.
                    </p>
                  </div>
                </div>
              )}
            </div>

            {tooLarge && (
              <p className="text-2xs text-rose-600 font-medium">
                This file exceeds the {MAX_UPLOAD_MB} MB maximum upload limit.
              </p>
            )}

            <p className="text-2xs text-slate-500">
              Note: <code className="font-mono text-slate-700">.git</code>, <code className="font-mono text-slate-700">node_modules</code>, and <code className="font-mono text-slate-700">.env</code> files are automatically ignored. Hardcoded credentials found in source code will trigger safety guardrails.
            </p>
          </div>
        )}

        {/* Tab 2: GitHub Repository */}
        {kind === "github" && (
          <div className="space-y-4">
            <div className="grid sm:grid-cols-2 gap-4">
              <div className="space-y-1.5 sm:col-span-2">
                <label htmlFor="repo" className="field-label">
                  GitHub Repository URL or Name
                </label>
                <div className="relative">
                  <input
                    id="repo"
                    className="field-input pr-10"
                    placeholder="e.g. owner/repo or https://github.com/owner/repo"
                    value={repo}
                    onChange={(e) => handleRepoChange(e.target.value)}
                  />
                  <GitBranch className="w-4 h-4 text-slate-400 absolute right-3 top-3 pointer-events-none" />
                </div>
              </div>

              <div className="space-y-1.5">
                <label htmlFor="ref" className="field-label">
                  Branch, Tag, or Commit (Optional)
                </label>
                <input
                  id="ref"
                  className="field-input"
                  placeholder="default branch (main/master)"
                  value={ref}
                  onChange={(e) => setRef(e.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <label htmlFor="token" className="field-label">
                  Personal Access Token (Private Repos)
                </label>
                <input
                  id="token"
                  type="password"
                  autoComplete="off"
                  className="field-input"
                  placeholder="ghp_... (Read-only token)"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                />
                <p className="text-3xs text-slate-400">Used strictly once for the initial download; never stored.</p>
              </div>
            </div>

            {/* Quick Sample Presets */}
            <div className="pt-2 border-t border-slate-100">
              <div className="flex items-center gap-1.5 text-2xs font-semibold text-slate-600 mb-2">
                <Sparkles className="w-3.5 h-3.5 text-brand-600" />
                Or try a sample project architecture:
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {SAMPLE_PRESETS.map((preset) => (
                  <button
                    key={preset.name}
                    type="button"
                    onClick={() => applyPreset(preset)}
                    className="p-2.5 rounded-xl border border-slate-200 hover:border-brand-300 hover:bg-slate-50 text-left transition-colors"
                  >
                    <div className="text-2xs font-semibold text-slate-800">{preset.name}</div>
                    <div className="text-3xs text-brand-600 font-mono mt-0.5">{preset.type}</div>
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* AWS Target Parameters */}
        <div className="grid sm:grid-cols-2 gap-4 pt-2 border-t border-slate-100">
          <div className="space-y-1.5">
            <label htmlFor="region" className="field-label">
              AWS Target Region
            </label>
            <select
              id="region"
              className="field-input"
              value={region}
              onChange={(e) => setRegion(e.target.value)}
            >
              {REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="env" className="field-label">
              Target Environment
            </label>
            <input
              id="env"
              className="field-input"
              value={environment}
              maxLength={32}
              placeholder="production / staging / dev"
              onChange={(e) => setEnvironment(e.target.value)}
            />
          </div>
        </div>

        {error && (
          <div className="p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-800">
            {error}
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <button
            type="submit"
            className="btn-primary w-full sm:w-auto"
            disabled={!ready || submitting}
          >
            {submitting ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Rocket className="w-4 h-4" />
            )}
            {submitting ? "Analyzing source code..." : "Analyze & Generate Terraform"}
          </button>
        </div>
      </form>
    </div>
  );
}
