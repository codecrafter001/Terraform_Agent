import { fetchJobResults } from "@/lib/api";
import GitHubPrViewer from "@/components/GitHubPrViewer";
import CreatePullRequestAction from "@/components/CreatePullRequestAction";
import { PageHeader } from "@/components/ui";
import { ArrowLeft, GitPullRequest } from "lucide-react";
import Link from "next/link";
import { JobResults } from "@/lib/types";

interface PrPageProps {
  params: Promise<{ id: string }>;
}

export const dynamic = "force-dynamic";

export default async function PrPage({ params }: PrPageProps) {
  const { id } = await params;
  let results: JobResults | null = null;
  try {
    results = await fetchJobResults(id);
  } catch {
    results = null;
  }

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-12">
      <div className="flex items-center justify-between gap-3">
        <PageHeader
          breadcrumbs={[
            { label: "Dashboard", href: "/" },
            { label: `Job ${id}`, href: `/results/${id}` },
            { label: "Pull requests" },
          ]}
          title="Review & merge"
          description="Live status of this job's pull requests. Review happens in GitHub; merging is guarded because your pipeline may apply on merge."
        />
        <Link href={`/results/${id}`} className="btn-secondary py-2 px-3 text-xs shrink-0">
          <ArrowLeft className="w-4 h-4" /> Back to results
        </Link>
      </div>

      {!results ? (
        <div className="card p-8 text-center text-xs text-slate-500">This job could not be loaded.</div>
      ) : results.github_pr ? (
        <>
          <GitHubPrViewer jobId={id} kind="adoption" prInfo={results.github_pr} results={results} />
          {results.github_hardening_pr && (
            <GitHubPrViewer jobId={id} kind="hardening" prInfo={results.github_hardening_pr} results={results} />
          )}
        </>
      ) : (
        <div className="space-y-6">
          <div className="card p-8 text-center space-y-3">
            <div className="w-12 h-12 rounded-2xl bg-slate-100 flex items-center justify-center mx-auto text-slate-700">
              <GitPullRequest className="w-6 h-6" />
            </div>
            <h3 className="text-base font-bold text-slate-900">No adoption pull request yet</h3>
            <p className="text-xs text-slate-500 max-w-md mx-auto">
              Open the adoption PR to put the generated Terraform in your repository for review.
            </p>
          </div>
          <div className="max-w-2xl mx-auto">
            <CreatePullRequestAction
              jobId={id}
              status={results.status}
              githubPr={results.github_pr}
              githubWavePrs={results.github_wave_prs}
              adoptionPlan={results.adoption_plan}
              githubHardeningPr={results.github_hardening_pr}
              hardening={results.hardening}
            />
          </div>
        </div>
      )}
    </div>
  );
}
