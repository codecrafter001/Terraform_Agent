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
  KeyRound,
  Layers,
  Loader2,
  Lock,
  PlayCircle,
  RefreshCw,
  ShieldCheck,
  UserCheck,
  Workflow,
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
  const [confirmText, setConfirmText] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [workflowRuns, setWorkflowRuns] = useState<GithubWorkflowRun[]>([]);
  const [expandedFiles, setExpandedFiles] = useState<Record<string, boolean>>({});

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
  const confirmed = understood && confirmText.trim() === String(prNumber);
  const canMerge =
    !isMerged && live && !blockedByOrder && prDetails?.state === "open" &&
    prDetails?.mergeable === true && reviews.approved && confirmed && !merging;

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
      setConfirmText("");
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
                    {isMerged ? "Merged" : prDetails?.state || prInfo.status || "open"}
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
                Review in GitHub
                <ExternalLink className="w-4 h-4" />
              </a>
            </div>
          </div>
        </div>

        <div className="bg-slate-50 border-t border-slate-200 p-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-xs text-slate-600">
              <Lock className="w-4 h-4 text-slate-400" />
              {githubToken
                ? "Live GitHub data - token held in this page's memory only"
                : "Showing the saved PR record. Add a GitHub token for live status, diff, reviews and merge."}
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
                Load live status
              </button>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-2xl border border-rose-200 bg-rose-50/80 p-4 flex items-start gap-3 text-xs text-rose-800">
          <AlertTriangle className="w-5 h-5 text-rose-600 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {isMerged && (
        <div className="rounded-2xl border border-purple-200 bg-purple-50/70 p-5 space-y-3">
          <h3 className="text-sm font-bold text-purple-950 flex items-center gap-2">
            <CheckCircle2 className="w-5 h-5 text-purple-600" /> Merged into <span className="font-mono">{baseBranch}</span>
          </h3>
          <p className="text-xs text-purple-900">
            {prDetails?.merge_commit_sha || prInfo.merge_commit_sha ? (
              <>Merge commit <span className="font-mono">{(prDetails?.merge_commit_sha || prInfo.merge_commit_sha || "").slice(0, 12)}</span>. </>
            ) : null}
            Any apply now happens in your own pipeline - TerraAgent never runs terraform apply.
          </p>
          {workflowRuns.length > 0 && (
            <div className="space-y-1.5">
              {workflowRuns.map((run) => (
                <div key={run.id} className="flex items-center justify-between text-xs bg-white/80 px-3 py-2 rounded-xl border border-purple-100">
                  <span className="flex items-center gap-2 min-w-0">
                    <PlayCircle className="w-3.5 h-3.5 text-purple-600 shrink-0" />
                    <span className="font-semibold truncate">{run.name}</span>
                    <span className="text-3xs text-slate-500 font-mono">
                      {run.status}{run.conclusion ? ` · ${run.conclusion}` : ""}
                    </span>
                  </span>
                  <a href={run.html_url} target="_blank" rel="noopener noreferrer" className="text-purple-700 text-3xs font-bold flex items-center gap-1">
                    Logs <ExternalLink className="w-3 h-3" />
                  </a>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          {kind === "adoption" && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="card p-5 space-y-2">
                <h3 className="text-xs font-bold text-slate-700 uppercase tracking-wider flex items-center gap-2">
                  <Layers className="w-4 h-4 text-brand-600" /> Migration Safety
                </h3>
                <div className="text-2xl font-extrabold text-slate-900">
                  {safety?.score != null ? `${safety.score}%` : "—"}
                </div>
                <p className="text-2xs text-slate-500">
                  {safety
                    ? `${safety.status.toLowerCase()} · ${safety.destroy_or_replace} destroy/replace · basis: ${safety.basis}`
                    : "Not measured"}
                </p>
              </div>
              <div className="card p-5 space-y-2">
                <h3 className="text-xs font-bold text-slate-700 uppercase tracking-wider flex items-center gap-2">
                  <ShieldCheck className="w-4 h-4 text-brand-600" /> Security Posture
                </h3>
                <div className="text-2xl font-extrabold text-slate-900">
                  {posture?.score != null ? `${posture.score}/100` : "—"}
                </div>
                <p className="text-2xs text-slate-500">
                  {posture
                    ? `${posture.counts.critical} critical · ${posture.counts.high} high - reported, fixes go in the Hardening PR`
                    : "Not measured"}
                </p>
              </div>
            </div>
          )}

          <div className="card overflow-hidden">
            <div className="card-header">
              <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
                <FileCode className="w-4 h-4 text-brand-600" />
                Changed files ({files.length || prInfo.changed_files?.length || 0})
              </h3>
              {prDetails && (prDetails.additions > 0 || prDetails.deletions > 0) && (
                <span className="text-2xs font-mono text-slate-500">
                  +{prDetails.additions} -{prDetails.deletions}
                </span>
              )}
            </div>
            <div className="p-5 space-y-3">
              {files.length > 0 ? (
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
                          <div className="text-slate-500 italic">GitHub didn&apos;t return a patch for this file (too large or binary).</div>
                        )}
                      </div>
                    )}
                  </div>
                ))
              ) : prInfo.changed_files && prInfo.changed_files.length > 0 ? (
                prInfo.changed_files.map((fn) => (
                  <div key={fn} className="px-4 py-2.5 bg-slate-50 rounded-xl border border-slate-200 text-xs font-mono font-bold text-slate-800">
                    {fn}
                  </div>
                ))
              ) : (
                <p className="text-xs text-slate-500">Add a GitHub token to load the changed files and diff.</p>
              )}
            </div>
          </div>
        </div>

        <div className="space-y-6">
          <div className="card p-5 space-y-3">
            <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <UserCheck className="w-4 h-4 text-brand-600" /> Review (in GitHub)
            </h3>
            <p className="text-2xs text-slate-500">
              TerraAgent opened this PR, so it never approves it. A teammate reviews and approves it in GitHub.
            </p>
            {!live ? (
              <p className="text-xs text-slate-500">Add a GitHub token to see the reviews.</p>
            ) : reviews.approved ? (
              <p className="text-xs font-semibold text-emerald-700 flex items-center gap-1.5">
                <Check className="w-4 h-4" /> Approved by {reviews.approvers.join(", ")}
              </p>
            ) : (
              <p className="text-xs font-semibold text-amber-700">
                {reviews.changesRequested ? "Changes requested" : "Waiting for an approving review"}
              </p>
            )}
          </div>

          <div className="card p-5 space-y-3">
            <h3 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <GitMerge className="w-4 h-4 text-purple-600" /> Merge
            </h3>
            <div className="text-2xs text-amber-900 bg-amber-50 border border-amber-200 rounded-xl p-3 leading-relaxed">
              <span className="font-bold">Merging can change production.</span> If your Atlantis, HCP Terraform or
              Actions pipeline applies on merge, merging here starts that apply. TerraAgent itself never runs terraform apply.
            </div>
            {isMerged ? (
              <p className="text-xs font-semibold text-purple-700 flex items-center gap-1.5">
                <Check className="w-4 h-4" /> Merged
              </p>
            ) : (
              <>
                <ul className="text-2xs space-y-1">
                  {[
                    { ok: live, label: "Live GitHub status loaded" },
                    ...(kind === "hardening" ? [{ ok: !blockedByOrder, label: "Adoption PR merged first" }] : []),
                    { ok: prDetails?.state === "open", label: "PR is open" },
                    { ok: prDetails?.mergeable === true, label: `Mergeable${prDetails?.mergeable_state ? ` (${prDetails.mergeable_state})` : ""}` },
                    { ok: reviews.approved, label: "Approved in GitHub, no changes requested" },
                  ].map((c) => (
                    <li key={c.label} className={c.ok ? "text-emerald-700" : "text-slate-500"}>
                      {c.ok ? "✓" : "○"} {c.label}
                    </li>
                  ))}
                </ul>
                <select
                  value={mergeMethod}
                  onChange={(e) => setMergeMethod(e.target.value as MergeMethod)}
                  disabled={merging}
                  className="field-input"
                  aria-label="Merge method"
                >
                  <option value="squash">Squash and merge</option>
                  <option value="merge">Create a merge commit</option>
                  <option value="rebase">Rebase and merge</option>
                </select>
                <label className="flex items-start gap-2 text-2xs text-slate-700">
                  <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} className="mt-0.5" />
                  I understand merging may make our pipeline apply this change.
                </label>
                <input
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={`Type ${prNumber} to confirm`}
                  className="field-input font-mono"
                  aria-label="Type the PR number to confirm"
                />
                <button
                  type="button"
                  onClick={handleMerge}
                  disabled={!canMerge}
                  className="w-full py-2.5 rounded-xl bg-purple-700 hover:bg-purple-600 disabled:bg-slate-300 text-white text-xs font-bold flex items-center justify-center gap-2"
                >
                  {merging ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitMerge className="w-4 h-4" />}
                  {merging ? `Merging into ${baseBranch}...` : `Merge into ${baseBranch}`}
                </button>
              </>
            )}
          </div>

          {workflowRuns.length > 0 && !isMerged && (
            <div className="card p-5 space-y-2 text-xs">
              <h4 className="font-bold text-slate-900 flex items-center gap-1.5">
                <Workflow className="w-4 h-4 text-brand-600" /> Recent runs on {baseBranch}
              </h4>
              {workflowRuns.map((run) => (
                <a key={run.id} href={run.html_url} target="_blank" rel="noopener noreferrer" className="block truncate text-brand-700 hover:underline">
                  {run.name} · {run.status}{run.conclusion ? ` · ${run.conclusion}` : ""}
                </a>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
