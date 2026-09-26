import Link from "next/link";
import { ExternalLink, GitPullRequest } from "lucide-react";
import { fetchJobs } from "@/lib/api";
import { PageHeader } from "@/components/ui";

export const dynamic = "force-dynamic";

/** Every pull request TerraAgent opened, from the job records (latest 100 jobs). */
export default async function PullRequestsPage() {
  const jobs = (await fetchJobs(100)).filter((j) => j.github_pr_url || j.github_hardening_pr_url);

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Pull Requests" }]}
        title="Pull requests"
        description="Adoption and hardening PRs opened by TerraAgent. Review and merge them from each job's PR page or in GitHub."
      />
      {jobs.length === 0 ? (
        <div className="card p-10 text-center text-xs text-slate-500 space-y-2">
          <GitPullRequest className="w-6 h-6 mx-auto text-slate-400" />
          <p>No pull requests yet. Open one from a completed scan&apos;s &ldquo;Pull request &amp; bundle&rdquo; tab.</p>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-3xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="text-left px-4 py-2.5">Job</th>
                <th className="text-left px-4 py-2.5">Region</th>
                <th className="text-left px-4 py-2.5">Adoption PR</th>
                <th className="text-left px-4 py-2.5">Hardening PR</th>
                <th className="text-left px-4 py-2.5">Created</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.job_id} className="border-t border-slate-100">
                  <td className="px-4 py-2.5 font-mono">
                    <Link href={`/results/${j.job_id}`} className="text-brand-700 hover:underline">{j.job_id}</Link>
                  </td>
                  <td className="px-4 py-2.5 font-mono">{j.region}</td>
                  <td className="px-4 py-2.5">
                    {j.github_pr_url ? (
                      <a href={j.github_pr_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand-700 hover:underline">
                        #{j.github_pr_number} <ExternalLink className="w-3 h-3" />
                      </a>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-2.5">
                    {j.github_hardening_pr_url ? (
                      <a href={j.github_hardening_pr_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand-700 hover:underline">
                        #{j.github_hardening_pr_number} <ExternalLink className="w-3 h-3" />
                      </a>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-slate-500">{new Date(j.created_at).toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right">
                    <Link href={`/results/${j.job_id}/pr`} className="btn-secondary py-1.5 text-2xs">Review &amp; merge</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
