"use client";

import { useState } from "react";
import { Eye, EyeOff, ExternalLink, GitPullRequestArrow, Loader2 } from "lucide-react";
import { createPullRequest } from "@/lib/api";
import { AdoptionPlan, GithubPrInfo, JobStatus } from "@/lib/types";

interface CreatePullRequestActionProps {
  jobId: string;
  status: JobStatus;
  githubPr?: GithubPrInfo | null;
  githubWavePrs?: Record<string, GithubPrInfo>;
  adoptionPlan?: AdoptionPlan;
}

// "whole" (the entire job) or a wave number as a string - keys this
// component's created-PR map the same way the backend keys
// github_wave_prs (JSON object keys are always strings).
const WHOLE_JOB_SCOPE = "whole";

function riskBadgeClass(riskLevel: string): string {
  if (riskLevel === "high") return "bg-rose-50 text-rose-700 border-rose-200";
  if (riskLevel === "medium") return "bg-amber-50 text-amber-700 border-amber-200";
  return "bg-emerald-50 text-emerald-700 border-emerald-200";
}

export default function CreatePullRequestAction({
  jobId,
  status,
  githubPr,
  githubWavePrs,
  adoptionPlan,
}: CreatePullRequestActionProps) {
  const [expanded, setExpanded] = useState(false);
  const [repo, setRepo] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [scope, setScope] = useState<string>(WHOLE_JOB_SCOPE);
  // Never pre-filled, never persisted (not localStorage, not a form default) -
  // same discipline CredentialForm.tsx applies to AWS credentials. Cleared
  // from this component's state the moment the request settles, success or
  // failure, so it never lingers in memory longer than the one request needs.
  const [githubToken, setGithubToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdPrs, setCreatedPrs] = useState<Record<string, GithubPrInfo>>({
    ...(githubPr ? { [WHOLE_JOB_SCOPE]: githubPr } : {}),
    ...(githubWavePrs ?? {}),
  });

  if (status !== "COMPLETE") return null;

  const waves = adoptionPlan?.waves ?? [];

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!repo.trim() || !githubToken.trim()) return;
    setSubmitting(true);
    setError(null);
    const waveNumber = scope === WHOLE_JOB_SCOPE ? undefined : Number(scope);
    try {
      const pr = await createPullRequest(jobId, {
        github_token: githubToken,
        repo: repo.trim(),
        base_branch: baseBranch.trim() || "main",
        ...(waveNumber !== undefined ? { wave: waveNumber } : {}),
      });
      setCreatedPrs((prev) => ({
        ...prev,
        [scope]: { pr_url: pr.pr_url, pr_number: pr.pr_number, branch: pr.branch, wave: pr.wave },
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create pull request");
    } finally {
      setGithubToken("");
      setSubmitting(false);
    }
  };

  const scopeLabel = (key: string): string => {
    if (key === WHOLE_JOB_SCOPE) return "Entire adoption";
    const wave = waves.find((w) => String(w.wave) === key);
    return wave ? `Wave ${wave.wave} (${wave.risk_level} risk, ${wave.resource_ids.length} resources)` : `Wave ${key}`;
  };

  return (
    <div className="space-y-3">
      {Object.entries(createdPrs).map(([key, pr]) => (
        <div
          key={key}
          className="rounded-2xl border border-emerald-200 bg-emerald-50/60 p-5 flex items-center justify-between gap-4 flex-wrap"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-xl bg-emerald-600 text-white shadow-xs">
              <GitPullRequestArrow className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-emerald-900">
                Pull Request Opened - {scopeLabel(key)}
              </h3>
              <p className="text-xs text-emerald-800/80">
                #{pr.pr_number}{pr.branch ? ` on branch ${pr.branch}` : ""}
              </p>
            </div>
          </div>
          <a
            href={pr.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm transition-all"
          >
            <span>View on GitHub</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </div>
      ))}

      <div className="rounded-2xl border border-slate-200/90 bg-white shadow-sm overflow-hidden">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full p-5 flex items-center justify-between gap-4 hover:bg-slate-50/60 transition-colors"
        >
          <div className="flex items-center gap-3 text-left">
            <div className="p-2 rounded-xl bg-slate-900 text-white shadow-xs">
              <GitPullRequestArrow className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-slate-900">
                {Object.keys(createdPrs).length > 0 ? "Create Another Pull Request" : "Create Pull Request"}
              </h3>
              <p className="text-xs text-slate-500">
                Open a reviewable, version-controlled PR with the generated Terraform files instead of just
                downloading a ZIP{waves.length > 0 ? " - the whole adoption, or just one wave at a time" : ""}.
              </p>
            </div>
          </div>
        </button>

        {expanded && (
          <form onSubmit={handleSubmit} className="px-5 pb-5 space-y-3 border-t border-slate-100 pt-4">
            {waves.length > 0 && (
              <div>
                <label className="text-[11px] font-bold text-slate-600 uppercase tracking-wider">Scope</label>
                <select
                  value={scope}
                  onChange={(e) => setScope(e.target.value)}
                  className="mt-1 w-full text-xs px-3 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-300 bg-white"
                >
                  <option value={WHOLE_JOB_SCOPE}>Entire adoption ({adoptionPlan?.total_resource_count ?? 0} resources)</option>
                  {waves.map((w) => (
                    <option key={w.wave} value={String(w.wave)}>
                      Wave {w.wave} - {w.risk_level} risk, {w.resource_ids.length} resource(s)
                    </option>
                  ))}
                </select>
                {scope !== WHOLE_JOB_SCOPE && (() => {
                  const selectedWave = waves.find((w) => String(w.wave) === scope);
                  return selectedWave && selectedWave.risk_signals.length > 0 ? (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {selectedWave.risk_signals.map((signal, i) => (
                        <span
                          key={i}
                          className={`text-[10px] font-medium px-2 py-0.5 rounded-full border ${riskBadgeClass(selectedWave.risk_level)}`}
                        >
                          {signal}
                        </span>
                      ))}
                    </div>
                  ) : null;
                })()}
              </div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="text-[11px] font-bold text-slate-600 uppercase tracking-wider">Repository</label>
                <input
                  type="text"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  placeholder="owner/repo"
                  className="mt-1 w-full text-xs px-3 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-300 font-mono"
                  required
                />
              </div>
              <div>
                <label className="text-[11px] font-bold text-slate-600 uppercase tracking-wider">Base Branch</label>
                <input
                  type="text"
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  placeholder="main"
                  className="mt-1 w-full text-xs px-3 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-300 font-mono"
                />
              </div>
            </div>

            <div>
              <label className="text-[11px] font-bold text-slate-600 uppercase tracking-wider">GitHub Token</label>
              <div className="relative mt-1">
                <input
                  type={showToken ? "text" : "password"}
                  value={githubToken}
                  onChange={(e) => setGithubToken(e.target.value)}
                  placeholder="ghp_... (contents:write, pull_requests:write)"
                  autoComplete="off"
                  className="w-full text-xs px-3 py-2.5 pr-10 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-brand-300 font-mono"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowToken((v) => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  tabIndex={-1}
                >
                  {showToken ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                </button>
              </div>
              <p className="text-[11px] text-slate-400 mt-1">
                Used once for this request only - never stored, logged, or saved anywhere.
              </p>
            </div>

            {error && <div className="text-xs text-rose-700 font-medium">{error}</div>}

            <button
              type="submit"
              disabled={submitting}
              className="w-full py-3 rounded-xl bg-slate-900 hover:bg-slate-800 disabled:opacity-60 text-white font-bold flex items-center justify-center gap-2 text-xs shadow-sm transition-all"
            >
              {submitting ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>Opening Pull Request...</span>
                </>
              ) : (
                <>
                  <GitPullRequestArrow className="w-4 h-4" />
                  <span>Open Pull Request</span>
                </>
              )}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
