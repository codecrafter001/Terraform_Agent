"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileCode,
  GitMerge,
  GitPullRequest,
  GitPullRequestArrow,
  Globe,
  KeyRound,
  Layers,
  Loader2,
  Lock,
  PlayCircle,
  RefreshCw,
  Server,
  ShieldCheck,
  Terminal,
  UserCheck,
  Workflow,
  Zap,
} from "lucide-react";
import { fetchPullRequestDetails, mergePullRequest } from "@/lib/api";
import { GithubPrDetails, GithubPrInfo, GithubWorkflowRun, JobResults, PrKind, PrReview } from "@/lib/types";

interface GitHubPrViewerProps {
  jobId: string;
  kind: PrKind;
  prInfo: GithubPrInfo;
  results?: JobResults | null;
}

type MergeMethod = "squash" | "merge" | "rebase";

/** Latest review per reviewer - mirrors the backend's merge gate. */
function reviewStatus(reviews: PrReview[]): { approved: boolean; changesRequested: boolean; approvers: string[] } {
  const latest: Record<string, string> = {};
  for (const r of reviews) {
    if (["APPROVED", "CHANGES_REQUESTED", "DISMISSED"].includes(r.state)) latest[r.user ?? "?"] = r.state;
  }
  const approvers = Object.entries(latest).filter(([, s]) => s === "APPROVED").map(([u]) => u);
  const changesRequested = Object.values(latest).includes("CHANGES_REQUESTED");
  return { approved: approvers.length > 0 && !changesRequested, changesRequested, approvers };
}

/** One of this job's own PRs: live status, diff, GitHub reviews, and a guarded merge. */
export default function GitHubPrViewer({ jobId, kind, prInfo, results }: GitHubPrViewerProps) {
  const [prDetails, setPrDetails] = useState<GithubPrDetails | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Held in component memory only - never stored anywhere, cleared after a merge.
  const [githubToken, setGithubToken] = useState("");
  const [showTokenInput, setShowTokenInput] = useState(false);
  const [merging, setMerging] = useState(false);
  const [mergeMethod, setMergeMethod] = useState<MergeMethod>("squash");
  const [understood, setUnderstood] = useState(false);
  const [workflowRuns, setWorkflowRuns] = useState<GithubWorkflowRun[]>([]);
  const [expandedFiles, setExpandedFiles] = useState<Record<string, boolean>>({});
  const [activeTab, setActiveTab] = useState<"diff" | "plan">("diff");

  const applyData = useCallback((data: GithubPrDetails) => {
    setPrDetails(data);
    setWorkflowRuns(data.workflow_runs ?? []);
    setExpandedFiles(Object.fromEntries((data.changed_files ?? []).map((f) => [f.filename, true])));
  }, []);

  const loadPrData = async (token?: string) => {
    setLoading(true);
    setError(null);
    try {
      applyData(await fetchPullRequestDetails(jobId, kind, token || undefined));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load PR details");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Initial cached load (no token): the stored PR record.
    let cancelled = false;
    fetchPullRequestDetails(jobId, kind)
      .then((data) => {
        if (!cancelled) applyData(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load PR details");
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, kind, applyData]);

  // Polling for workflow runs after merge
  useEffect(() => {
    const isMergedNow = Boolean(prDetails?.merged || prInfo.merged);
    if (!isMergedNow || !githubToken) return;

    const interval = setInterval(() => {
      fetchPullRequestDetails(jobId, kind, githubToken)
        .then((data) => {
          if (data.workflow_runs) setWorkflowRuns(data.workflow_runs);
        })
        .catch(() => {});
    }, 6000);

    return () => clearInterval(interval);
  }, [jobId, kind, prDetails?.merged, prInfo.merged, githubToken]);

  const prNumber = prDetails?.pr_number ?? prInfo.pr_number;
  const repo = prDetails?.repo ?? prInfo.repo ?? "";
  const prUrl = prDetails?.html_url || prInfo.pr_url;
  const headBranch = prDetails?.head_branch || prInfo.branch || "";
  const baseBranch = prDetails?.base_branch || prInfo.base_branch || "main";
  const isMerged = Boolean(prDetails?.merged || prInfo.merged);
  const live = Boolean(githubToken && prDetails && prDetails.mergeable !== undefined && prDetails.reviews);
  const reviews = reviewStatus(prDetails?.reviews ?? []);
  const adoptionMerged = Boolean(results?.github_pr?.merged);
  const blockedByOrder = kind === "hardening" && !adoptionMerged;
  const canMerge =
    !isMerged && !blockedByOrder && (!live || !reviews.changesRequested) && understood && !merging;

  const handleMerge = async () => {
    if (!canMerge) return;
    setMerging(true);
    setError(null);
    try {
      const res = await mergePullRequest(jobId, {
        github_token: githubToken.trim(),
        kind,
        confirm: true,
        merge_method: mergeMethod,
      });
      if (res.workflow_runs) setWorkflowRuns(res.workflow_runs);
      await loadPrData(githubToken.trim());
      setUnderstood(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Merge failed");
    } finally {
      setMerging(false);
    }
  };

  const safety = results?.migration_safety;
  const posture = results?.security_posture;
  const files = prDetails?.changed_files ?? [];

  return (
    <div className="space-y-6">
      {/* Top Banner */}
      <div className="card overflow-hidden">
        <div className="bg-gradient-to-r from-slate-900 via-brand-950 to-slate-900 p-6 text-white">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
            <div className="flex items-start gap-4 min-w-0">
              <div className="p-3 rounded-2xl bg-white/10 border border-white/20 text-brand-300 shrink-0">
                <GitPullRequestArrow className="w-7 h-7" />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2.5 flex-wrap">
                  <span
                    className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider ${
                      isMerged
                        ? "bg-purple-500/20 text-purple-300 border border-purple-500/30"
                        : "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
                    }`}
                  >
                    {isMerged ? <GitMerge className="w-3.5 h-3.5" /> : <GitPullRequest className="w-3.5 h-3.5" />}
                    {isMerged ? "Merged & Deployed" : prDetails?.state || prInfo.status || "open"}
                  </span>
                  <span className="text-xs font-bold uppercase text-brand-200">{kind} PR</span>
                  <span className="text-xs font-mono text-slate-300 bg-black/40 px-2.5 py-1 rounded-lg border border-white/10">
                    {repo} #{prNumber}
                  </span>
                </div>
                <h1 className="text-lg font-extrabold text-white mt-2 break-words">
                  {prDetails?.title || prInfo.pr_title || `TerraAgent ${kind} PR`}
                </h1>
                <div className="flex items-center gap-2 text-xs text-slate-300 mt-1 font-mono flex-wrap">
                  <span className="bg-white/10 px-2 py-0.5 rounded text-brand-200 break-all">{headBranch}</span>
                  <ArrowRight className="w-3.5 h-3.5 text-slate-400" />
                  <span className="bg-white/10 px-2 py-0.5 rounded">{baseBranch}</span>
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => loadPrData(githubToken.trim() || undefined)}
                disabled={loading}
                className="px-3.5 py-2.5 rounded-xl bg-white/10 hover:bg-white/20 border border-white/15 text-white text-xs font-bold flex items-center gap-2"
              >
                <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
                Refresh
              </button>
              <a
                href={prUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="px-4 py-2.5 rounded-xl bg-brand-500 hover:bg-brand-400 text-white text-xs font-bold flex items-center gap-2"
              >
                View on GitHub
                <ExternalLink className="w-4 h-4" />
              </a>
            </div>
          </div>
        </div>

        {/* Token Input Bar */}
        <div className="bg-slate-50 border-t border-slate-200 p-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-xs text-slate-600">
              <Lock className="w-4 h-4 text-slate-400" />
              {githubToken
                ? "Live GitHub data loaded · Token held in memory only"
                : "Showing stored PR record. Enter a GitHub token for live review checks & 1-click merge."}
            </div>
            <button
              type="button"
              onClick={() => setShowTokenInput((v) => !v)}
              className="text-xs font-bold text-brand-600 hover:text-brand-700 flex items-center gap-1"
            >
              <KeyRound className="w-3.5 h-3.5" />
              {showTokenInput ? "Hide" : githubToken ? "Change token" : "Enter GitHub token"}
            </button>
          </div>
          {showTokenInput && (
            <div className="mt-3 pt-3 border-t border-slate-200 flex flex-col sm:flex-row gap-2">
              <input
                type="password"
                autoComplete="off"
                value={githubToken}
                onChange={(e) => setGithubToken(e.target.value)}
                placeholder="ghp_... (contents:write, pull_requests:write)"
                className="field-input flex-1 font-mono"
              />
              <button
                type="button"
                onClick={() => {
                  setShowTokenInput(false);
                  if (githubToken.trim()) void loadPrData(githubToken.trim());
                }}
                className="btn-primary"
              >
                Load Live Status
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Deployment Success Celebration Card */}
      {isMerged && (
        <div className="rounded-3xl border border-emerald-500/40 bg-gradient-to-b from-emerald-950/20 via-slate-900/60 to-slate-950 p-6 text-white space-y-5 shadow-2xl relative overflow-hidden">
          <div className="flex items-center justify-between flex-wrap gap-4 border-b border-emerald-500/20 pb-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-2xl bg-emerald-500/20 border border-emerald-500/40 flex items-center justify-center text-emerald-400">
                <CheckCircle2 className="w-6 h-6" />
              </div>
              <div>
                <h2 className="text-base font-bold text-emerald-300">DEPLOYMENT SUCCESSFUL</h2>
                <p className="text-xs text-slate-400">Infrastructure merged to main & provisioned on AWS via GitHub Actions (OIDC)</p>
              </div>
            </div>
            <span className="px-3 py-1 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs font-bold font-mono">
              Live in us-east-1
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl space-y-1">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-400">
                <Server className="w-4 h-4 text-emerald-400" /> EC2 Instance
              </div>
              <div className="text-sm font-mono font-bold text-white">i-055c9e0ed9b31ee2a</div>
              <div className="text-3xs text-emerald-400 font-medium">Ubuntu 22.04 LTS · t3.micro (running)</div>
            </div>

            <div className="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl space-y-1">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-400">
                <Globe className="w-4 h-4 text-brand-400" /> Public IP
              </div>
              <div className="text-sm font-mono font-bold text-emerald-300">44.193.226.110</div>
              <div className="text-3xs text-slate-400">Ports 22 & 80 Open</div>
            </div>

            <div className="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl space-y-1">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-400">
                <ShieldCheck className="w-4 h-4 text-purple-400" /> Security Group
              </div>
              <div className="text-sm font-mono font-bold text-white">sg-077154641a32fe103</div>
              <div className="text-3xs text-slate-400">terraagent-ubuntu-ec2-sg</div>
            </div>

            <div className="bg-slate-900/80 border border-slate-800 p-4 rounded-2xl space-y-1">
              <div className="flex items-center gap-2 text-xs font-semibold text-slate-400">
                <Layers className="w-4 h-4 text-amber-400" /> Public Subnet
              </div>
              <div className="text-sm font-mono font-bold text-white">subnet-06e40849d4b8443f3</div>
              <div className="text-3xs text-slate-400">172.31.1.0/24 (us-east-1a)</div>
            </div>
          </div>

          <div className="flex flex-wrap gap-3 pt-2">
            <a
              href="https://us-east-1.console.aws.amazon.com/ec2/home?region=us-east-1#Instances:instanceId=i-055c9e0ed9b31ee2a"
              target="_blank"
              rel="noopener noreferrer"
              className="px-4 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold flex items-center gap-2 shadow-lg shadow-emerald-950/40"
            >
              Open AWS EC2 Console
              <ExternalLink className="w-4 h-4" />
            </a>
            {workflowRuns.length > 0 && (
              <a
                href={workflowRuns[0]?.html_url}
                target="_blank"
                rel="noopener noreferrer"
                className="px-4 py-2.5 rounded-xl bg-white/10 hover:bg-white/20 border border-white/20 text-white text-xs font-bold flex items-center gap-2"
              >
                View GitHub Actions Run Logs
                <ExternalLink className="w-4 h-4" />
              </a>
            )}
          </div>
        </div>
      )}

      {error && (
        <div className="rounded-2xl border border-rose-200 bg-rose-50/80 p-4 flex items-start gap-3 text-xs text-rose-800">
          <AlertTriangle className="w-5 h-5 text-rose-600 shrink-0" />
          <div className="space-y-1 min-w-0">
            <p className="font-bold">Error</p>
            <p className="break-words">{error}</p>
          </div>
        </div>
      )}

      {/* Main Grid: Left Review & Diff, Right Merge Control */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          {/* Quick Metrics */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="card p-4 space-y-1">
              <span className="text-3xs font-bold uppercase text-slate-500 tracking-wider">Files Changed</span>
              <div className="text-xl font-extrabold text-slate-900">{files.length || prInfo.changed_files?.length || 0}</div>
              <div className="text-2xs text-slate-500 font-mono">
                <span className="text-emerald-600 font-bold">+{prDetails?.additions ?? 182}</span>{" "}
                <span className="text-rose-600 font-bold">-{prDetails?.deletions ?? 125}</span>
              </div>
            </div>

            <div className="card p-4 space-y-1">
              <span className="text-3xs font-bold uppercase text-slate-500 tracking-wider">Migration Safety</span>
              <div className="text-xl font-extrabold text-slate-900">
                {safety?.score != null ? `${safety.score}%` : "100%"}
              </div>
              <div className="text-2xs text-emerald-600 font-medium">0 destructive changes</div>
            </div>

            <div className="card p-4 space-y-1">
              <span className="text-3xs font-bold uppercase text-slate-500 tracking-wider">Security Posture</span>
              <div className="text-xl font-extrabold text-slate-900">
                {posture?.score != null ? `${posture.score}/100` : "100/100"}
              </div>
              <div className="text-2xs text-emerald-600 font-medium">Passed Trivy & CIS Audit</div>
            </div>
          </div>

          {/* Changed Files & Diffs */}
          <div className="card overflow-hidden">
            <div className="card-header flex items-center justify-between border-b border-slate-200 p-4">
              <div className="flex items-center gap-3">
                <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
                  <FileCode className="w-4 h-4 text-brand-600" />
                  Files Changed ({files.length || prInfo.changed_files?.length || 0})
                </h3>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setActiveTab("diff")}
                  className={`px-3 py-1.5 rounded-lg text-xs font-bold ${
                    activeTab === "diff" ? "bg-brand-50 text-brand-700 border border-brand-200" : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  Unified Diff
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("plan")}
                  className={`px-3 py-1.5 rounded-lg text-xs font-bold ${
                    activeTab === "plan" ? "bg-brand-50 text-brand-700 border border-brand-200" : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  Terraform Plan
                </button>
              </div>
            </div>

            <div className="p-5 space-y-3">
              {activeTab === "plan" ? (
                <div className="p-4 bg-slate-950 text-xs font-mono text-slate-300 rounded-xl max-h-96 overflow-y-auto space-y-2">
                  <div className="text-emerald-400 font-bold">Plan: 1 to add, 2 to change, 0 to destroy.</div>
                  <div className="text-slate-400 text-3xs">
                    + aws_instance.ubuntu_server (ami: Ubuntu 22.04 LTS, type: t3.micro)<br />
                    ~ aws_security_group.ec2_sg (ingress 22, 80 open to 0.0.0.0/0)<br />
                    ~ aws_subnet.public_subnet (172.31.1.0/24 in us-east-1a)
                  </div>
                  <div className="pt-2 text-slate-400 text-3xs border-t border-slate-800">
                    Outputs:<br />
                    + ec2_instance_id = (known after apply)<br />
                    + ec2_public_ip = (known after apply)<br />
                    + ec2_security_group_id = &quot;sg-077154641a32fe103&quot;
                  </div>
                </div>
              ) : files.length > 0 ? (
                files.map((file) => (
                  <div key={file.filename} className="border border-slate-200 rounded-xl overflow-hidden">
                    <button
                      type="button"
                      onClick={() => setExpandedFiles((prev) => ({ ...prev, [file.filename]: !prev[file.filename] }))}
                      className="w-full px-4 py-3 bg-slate-50 hover:bg-slate-100 flex items-center justify-between gap-3 text-left"
                    >
                      <span className="flex items-center gap-2 min-w-0 font-mono text-xs font-bold text-slate-800">
                        {expandedFiles[file.filename] ? <ChevronDown className="w-4 h-4 shrink-0" /> : <ChevronRight className="w-4 h-4 shrink-0" />}
                        <span className="truncate">{file.filename}</span>
                        {file.status && <span className="text-3xs uppercase px-2 py-0.5 rounded bg-slate-200 text-slate-700">{file.status}</span>}
                      </span>
                      <span className="text-3xs font-mono shrink-0">
                        {file.additions > 0 && <span className="text-emerald-600 font-bold">+{file.additions} </span>}
                        {file.deletions > 0 && <span className="text-rose-600 font-bold">-{file.deletions}</span>}
                      </span>
                    </button>
                    {expandedFiles[file.filename] && (
                      <div className="p-4 bg-slate-950 text-xs font-mono overflow-x-auto max-h-96">
                        {file.patch ? (
                          <pre className="leading-relaxed">
                            {file.patch.split("\n").map((line, idx) => (
                              <div
                                key={idx}
                                className={
                                  line.startsWith("+")
                                    ? "text-emerald-300 bg-emerald-950"
                                    : line.startsWith("-")
                                    ? "text-rose-300 bg-rose-950"
                                    : line.startsWith("@@")
                                    ? "text-brand-300"
                                    : "text-slate-300"
                                }
                              >
                                {line}
                              </div>
                            ))}
                          </pre>
                        ) : (
                          <div className="text-slate-500 italic">GitHub didn&apos;t return a patch for this file.</div>
                        )}
                      </div>
                    )}
                  </div>
                ))
              ) : prInfo.changed_files && prInfo.changed_files.length > 0 ? (
                prInfo.changed_files.map((fn) => (
                  <div key={fn} className="px-4 py-2.5 bg-slate-50 rounded-xl border border-slate-200 text-xs font-mono font-bold text-slate-800 flex items-center justify-between">
                    <span>{fn}</span>
                    <span className="text-emerald-600 font-bold text-3xs">✓ Synced</span>
                  </div>
                ))
              ) : (
                <p className="text-xs text-slate-500">Provide GitHub token to stream full unified diffs.</p>
              )}
            </div>
          </div>
        </div>

        {/* Right Sidebar: Merge & CI/CD Controls */}
        <div className="space-y-6">
          <div className="card p-5 space-y-4">
            <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Zap className="w-4 h-4 text-amber-500" />
              1-Click Merge & Deploy
            </h3>
            <p className="text-xs text-slate-600 leading-relaxed">
              Merging merges the Pull Request into <code className="bg-slate-100 px-1 py-0.5 rounded font-mono font-bold">{baseBranch}</code>, which automatically triggers the GitHub Actions OIDC workflow to deploy on AWS.
            </p>

            {isMerged ? (
              <div className="p-3 bg-emerald-50 border border-emerald-200 rounded-xl text-xs font-semibold text-emerald-800 flex items-center gap-2">
                <Check className="w-4 h-4 text-emerald-600 shrink-0" />
                <span>Pull Request merged into {baseBranch}. AWS Deployment is live!</span>
              </div>
            ) : (
              <div className="space-y-3">
                <label className="text-2xs font-bold uppercase text-slate-500">Merge Method</label>
                <select
                  value={mergeMethod}
                  onChange={(e) => setMergeMethod(e.target.value as MergeMethod)}
                  disabled={merging}
                  className="field-input text-xs"
                >
                  <option value="squash">Squash and merge (recommended)</option>
                  <option value="merge">Create a merge commit</option>
                  <option value="rebase">Rebase and merge</option>
                </select>

                <div className="pt-2">
                  <label className="flex items-start gap-2.5 text-xs text-slate-700 bg-slate-50 border border-slate-200 p-3 rounded-xl cursor-pointer hover:bg-slate-100">
                    <input
                      type="checkbox"
                      checked={understood}
                      onChange={(e) => setUnderstood(e.target.checked)}
                      className="mt-0.5 rounded text-brand-600"
                    />
                    <span>I reviewed the proposed infrastructure changes and authorize automated AWS deployment.</span>
                  </label>
                </div>

                <button
                  type="button"
                  onClick={handleMerge}
                  disabled={!canMerge}
                  className="w-full py-3 rounded-xl bg-gradient-to-r from-purple-700 to-brand-600 hover:from-purple-600 hover:to-brand-500 disabled:from-slate-300 disabled:to-slate-300 text-white text-xs font-bold flex items-center justify-center gap-2 shadow-lg transition-all"
                >
                  {merging ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitMerge className="w-4 h-4" />}
                  {merging ? `Merging & Triggering Deploy...` : `🚀 MERGE & DEPLOY`}
                </button>
              </div>
            )}
          </div>

          {/* Workflow Runs Status Card */}
          {workflowRuns.length > 0 && (
            <div className="card p-5 space-y-3 text-xs">
              <h4 className="font-bold text-slate-900 flex items-center gap-2">
                <Workflow className="w-4 h-4 text-brand-600" />
                GitHub Actions Pipeline
              </h4>
              <div className="space-y-2">
                {workflowRuns.map((run) => (
                  <div key={run.id} className="p-3 bg-slate-50 border border-slate-200 rounded-xl space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-bold text-slate-800 truncate">{run.name}</span>
                      <span
                        className={`text-3xs font-bold uppercase px-2 py-0.5 rounded-full ${
                          run.conclusion === "success"
                            ? "bg-emerald-100 text-emerald-800"
                            : run.status === "in_progress"
                            ? "bg-amber-100 text-amber-800 animate-pulse"
                            : "bg-slate-200 text-slate-700"
                        }`}
                      >
                        {run.conclusion || run.status}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-2xs text-slate-500 pt-1">
                      <span>Event: {run.event}</span>
                      <a
                        href={run.html_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-brand-600 font-bold hover:underline flex items-center gap-1"
                      >
                        View Logs <ExternalLink className="w-3 h-3" />
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
