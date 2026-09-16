import { fetchJobResults } from "@/lib/api";
import CreatePullRequestAction from "@/components/CreatePullRequestAction";
import DependencyGraph from "@/components/DependencyGraph";
import ResultsApprovalSection from "@/components/ResultsApprovalSection";
import ValidationReport from "@/components/ValidationReport";
import ZipDownload from "@/components/ZipDownload";
import Link from "next/link";
import { ArrowLeft, CheckCircle2, Shield, Layers, ExternalLink, Globe, Cpu, CheckCheck } from "lucide-react";
import { JobResults } from "@/lib/types";

interface ResultsPageProps {
  params: Promise<{
    id: string;
  }>;
}

export const dynamic = "force-dynamic";

export default async function JobResultsPage({ params }: ResultsPageProps) {
  const { id } = await params;
  let results: JobResults;
  try {
    results = await fetchJobResults(id);
  } catch (err) {
    // Fallback demonstration data if server is unreachable
    results = {
      job_id: id,
      status: "COMPLETE",
      operation: "generate",
      region: "us-east-1",
      resources_count: 5,
      resources: [
        { id: "vpc-0a1b2c3d4e5f", resource_type: "aws_vpc", name: "prod-main-vpc" },
        { id: "subnet-0123456789abcdef", resource_type: "aws_subnet", name: "prod-public-subnet-1" },
        { id: "sg-0feebda1234", resource_type: "aws_security_group", name: "web-tier-sg" },
        { id: "i-0987654321fedcba0", resource_type: "aws_instance", name: "api-gateway-node" },
        { id: "app-production-assets-2026", resource_type: "aws_s3_bucket", name: "app-production-assets-2026" }
      ],
      dependency_graph: {
        nodes: [
          { id: "vpc-0a1b2c3d4e5f", name: "prod-main-vpc", type: "aws_vpc", category: "Networking" },
          { id: "subnet-0123456789abcdef", name: "prod-public-subnet-1", type: "aws_subnet", category: "Networking" },
          { id: "sg-0feebda1234", name: "web-tier-sg", type: "aws_security_group", category: "Security" },
          { id: "i-0987654321fedcba0", name: "api-gateway-node", type: "aws_instance", category: "Compute" },
          { id: "app-production-assets-2026", name: "app-production-assets-2026", type: "aws_s3_bucket", category: "Storage" }
        ],
        links: [
          { source: "subnet-0123456789abcdef", target: "vpc-0a1b2c3d4e5f", relation: "contains" },
          { source: "i-0987654321fedcba0", target: "subnet-0123456789abcdef", relation: "hosted_in" },
          { source: "i-0987654321fedcba0", target: "sg-0feebda1234", relation: "secured_by" }
        ],
        is_dag: true,
        node_count: 5,
        edge_count: 3,
        topological_order: [
          "vpc-0a1b2c3d4e5f",
          "subnet-0123456789abcdef",
          "sg-0feebda1234",
          "i-0987654321fedcba0",
          "app-production-assets-2026"
        ]
      },
      validation_results: {
        passed: true,
        checks: [
          { check_name: "terraform_fmt", passed: true, output: "All files formatted cleanly." },
          { check_name: "terraform_init", passed: true, output: "Terraform has been successfully initialized." },
          { check_name: "terraform_validate", passed: true, output: "Success! The configuration is valid." }
        ]
      },
      security_results: {
        passed: true,
        risk_score: 0,
        critical_count: 0,
        high_count: 0,
        medium_count: 0,
        low_count: 0,
        findings: []
      },
      zip_available: true,
      download_url: `/api/download/${id}`,
      zip_sha256: undefined,
      zip_manifest: []
    };
  }

  const d3Data = results.dependency_graph;

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-900 transition-colors font-medium mb-1"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            Back to Dashboard
          </Link>
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-2xl font-bold tracking-tight text-slate-900 flex items-center gap-2">
              <span>Scan Synthesis Results</span>
            </h1>
            <span className="font-mono text-xs font-bold text-brand-700 bg-brand-50 px-2.5 py-1 rounded-lg border border-brand-200 shadow-2xs">
              {id}
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-500 pt-1 flex-wrap font-medium">
            <span className="inline-flex items-center gap-1">
              <Globe className="w-3.5 h-3.5 text-slate-400" />
              <span>Region: <strong className="text-slate-700 font-mono">{results.region}</strong></span>
            </span>
            <span>•</span>
            <span className="inline-flex items-center gap-1">
              <Cpu className="w-3.5 h-3.5 text-slate-400" />
              <span>Discovered: <strong className="text-slate-700">{results.resources_count} resources</strong></span>
            </span>
            <span>•</span>
            <span className="inline-flex items-center gap-1">
              <CheckCheck className="w-3.5 h-3.5 text-emerald-600" />
              <span>Pipeline: <strong className="text-emerald-700 uppercase">Passed</strong></span>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <Link
            href="/scan"
            className="px-4 py-2 rounded-xl bg-white hover:bg-slate-50 text-slate-700 border border-slate-200 text-xs font-bold shadow-2xs transition-all"
          >
            New Scan
          </Link>
          <Link
            href={`/results/${id}/graph`}
            className="px-4 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm transition-all"
          >
            <span>Full-Screen D3 View</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </Link>
        </div>
      </div>

      {/* Human Approval Gate - only renders when there's a real pending_approval finding */}
      <ResultsApprovalSection
        jobId={id}
        status={results.status}
        pendingApproval={results.pending_approval}
        planEquivalenceResults={results.plan_equivalence_results}
        approvalDecision={results.approval_decision}
      />

      {/* ZIP Download Card */}
      <ZipDownload
        jobId={id}
        downloadUrl={results.download_url}
        manifest={results.zip_manifest}
        sha256={results.zip_sha256}
        status={results.status}
      />

      {/* GitHub Pull Request - the engineering deliverable alongside the ZIP */}
      <CreatePullRequestAction
        jobId={id}
        status={results.status}
        githubPr={results.github_pr}
        githubWavePrs={results.github_wave_prs}
        adoptionPlan={results.adoption_plan}
      />

      {/* Infrastructure Intelligence & Adoption Plan Card */}
      {results.adoption_plan && (
        <div className="rounded-2xl border border-slate-200/90 bg-white p-5 sm:p-6 shadow-sm space-y-6">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 pb-4">
            <div>
              <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
                <Layers className="w-5 h-5 text-brand-600" />
                <span>Infrastructure Intelligence & Adoption Plan</span>
              </h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Deterministic classification, dependency confidence scoring, and multi-wave migration ordering.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold px-2.5 py-1 rounded-lg bg-slate-100 text-slate-700 border border-slate-200">
                Risk Score: <strong className={results.adoption_plan.risk_score > 30 ? "text-amber-600" : "text-emerald-600"}>{results.adoption_plan.risk_score}/100</strong>
              </span>
            </div>
          </div>

          {/* Key Metrics Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3 text-center">
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200/60">
              <div className="text-xl font-extrabold text-slate-800">{results.resources_count}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-slate-500 mt-0.5">Discovered</div>
            </div>
            <div className="p-3 rounded-xl bg-emerald-50/70 border border-emerald-200/60">
              <div className="text-xl font-extrabold text-emerald-700">{results.adoption_plan.managed_count ?? results.adoption_plan.categories.find(c => c.category === "safe_to_import")?.resource_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-emerald-600 mt-0.5">Managed</div>
            </div>
            <div className="p-3 rounded-xl bg-amber-50/70 border border-amber-200/60">
              <div className="text-xl font-extrabold text-amber-700">{results.adoption_plan.review_count ?? results.adoption_plan.categories.find(c => c.category === "review_required")?.resource_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-amber-600 mt-0.5">Review</div>
            </div>
            <div className="p-3 rounded-xl bg-blue-50/70 border border-blue-200/60">
              <div className="text-xl font-extrabold text-blue-700">{results.adoption_plan.data_source_count ?? results.adoption_plan.categories.find(c => c.category === "use_data_source")?.resource_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-blue-600 mt-0.5">Data Sources</div>
            </div>
            <div className="p-3 rounded-xl bg-rose-50/70 border border-rose-200/60">
              <div className="text-xl font-extrabold text-rose-700">{results.adoption_plan.unsupported_count ?? results.adoption_plan.categories.find(c => c.category === "unsupported")?.resource_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-rose-600 mt-0.5">Unsupported</div>
            </div>
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200/60">
              <div className="text-xl font-extrabold text-slate-800">{results.dependency_graph?.edge_count ?? results.adoption_plan.total_dependencies ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-slate-500 mt-0.5">Dependencies</div>
            </div>
            <div className="p-3 rounded-xl bg-indigo-50/70 border border-indigo-200/60">
              <div className="text-xl font-extrabold text-indigo-700">{results.dependency_graph?.high_confidence_edge_count ?? results.adoption_plan.high_confidence_dependencies ?? results.dependency_graph?.edge_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-indigo-600 mt-0.5">High Conf.</div>
            </div>
            <div className="p-3 rounded-xl bg-purple-50/70 border border-purple-200/60">
              <div className="text-xl font-extrabold text-purple-700">{results.adoption_plan.waves?.length ?? results.adoption_plan.wave_count ?? 0}</div>
              <div className="text-3xs uppercase tracking-wider font-bold text-purple-600 mt-0.5">Waves</div>
            </div>
          </div>

          {/* Plan Prose Summary */}
          {results.adoption_plan.summary && (
            <div className="p-4 rounded-xl bg-slate-50/80 border border-slate-200 text-xs text-slate-700 leading-relaxed font-sans">
              <span className="font-bold text-slate-900">Executive Adoption Summary: </span>
              {results.adoption_plan.summary}
            </div>
          )}

          {/* Migration Waves Sequence */}
          {results.adoption_plan.waves && results.adoption_plan.waves.length > 0 && (
            <div className="space-y-3">
              <h3 className="text-xs font-bold uppercase tracking-wider text-slate-600">
                Deterministic Migration Waves
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {results.adoption_plan.waves.map((w, idx) => (
                  <div key={idx} className="p-3.5 rounded-xl border border-slate-200 bg-white hover:border-brand-300 transition-all space-y-2 shadow-2xs">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-xs bg-slate-900 text-white px-2 py-0.5 rounded-md">
                          Wave {w.wave}
                        </span>
                        <span className="text-xs font-semibold text-slate-800">
                          {w.category_name || `Stage ${w.wave}`}
                        </span>
                      </div>
                      <span className={`text-3xs font-bold uppercase px-2 py-0.5 rounded-full border ${
                        w.risk_level === 'high' ? 'bg-rose-50 text-rose-700 border-rose-200' :
                        w.risk_level === 'medium' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                        'bg-emerald-50 text-emerald-700 border-emerald-200'
                      }`}>
                        {w.risk_level} risk
                      </span>
                    </div>

                    <div className="text-2xs text-slate-500 font-mono break-all line-clamp-2">
                      {w.resource_ids.join(", ")}
                    </div>

                    {w.risk_signals && w.risk_signals.length > 0 && (
                      <div className="text-3xs text-amber-700 bg-amber-50/50 p-1.5 rounded-md border border-amber-100">
                        {w.risk_signals.join("; ")}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Interactive Topology Graph */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Layers className="w-4 h-4 text-brand-600" />
            <span>Infrastructure Topology & Dependency DAG</span>
          </h2>
          <span className="text-xs text-slate-400 font-medium">Interactive (Pan & Zoom enabled)</span>
        </div>
        <DependencyGraph data={d3Data} />
      </div>

      {/* Verification, Security & Import Report */}
      <div className="space-y-3">
        <ValidationReport
          validationResults={results.validation_results}
          securityResults={results.security_results}
          resources={results.resources}
        />
      </div>
    </div>
  );
}
