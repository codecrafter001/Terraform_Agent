import { fetchJobResults } from "@/lib/api";
import { demoJobResults } from "@/lib/demo";
import CreatePullRequestAction from "@/components/CreatePullRequestAction";
import DependencyGraph from "@/components/DependencyGraph";
import DecisionsPanel from "@/components/DecisionsPanel";
import HardeningPanel from "@/components/HardeningPanel";
import InventoryPanel from "@/components/InventoryPanel";
import ResultsApprovalSection from "@/components/ResultsApprovalSection";
import ResultsTabs from "@/components/ResultsTabs";
import ScoresPanel from "@/components/ScoresPanel";
import ValidationReport from "@/components/ValidationReport";
import ZipDownload from "@/components/ZipDownload";
import { PageHeader, SectionHeading, StatCard, StatusBadge, Tone } from "@/components/ui";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowLeftRight,
  Boxes,
  CheckCheck,
  Download,
  GitPullRequestArrow,
  Globe,
  Globe2,
  LayoutGrid,
  Layers,
  Maximize2,
  Network,
  Package,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { AdoptionPlan, JobResults } from "@/lib/types";

interface ResultsPageProps {
  params: Promise<{
    id: string;
  }>;
}

export const dynamic = "force-dynamic";

function categoryCount(plan: AdoptionPlan, explicit: number | undefined, category: string): number {
  return explicit ?? plan.categories.find((c) => c.category === category)?.resource_count ?? 0;
}

function riskBadgeClass(level: string): string {
  if (level === "high") return "bg-rose-50 text-rose-700 border-rose-200";
  if (level === "medium") return "bg-amber-50 text-amber-700 border-amber-200";
  return "bg-emerald-50 text-emerald-700 border-emerald-200";
}

function AdoptionPlanCard({ results, plan }: { results: JobResults; plan: AdoptionPlan }) {
  const metrics: { label: string; value: number; tone: Tone }[] = [
    { label: "Discovered", value: results.resources_count, tone: "slate" },
    { label: "Managed", value: categoryCount(plan, plan.managed_count, "safe_to_import"), tone: "emerald" },
    { label: "Review", value: categoryCount(plan, plan.review_count, "review_required"), tone: "amber" },
    { label: "Data sources", value: categoryCount(plan, plan.data_source_count, "use_data_source"), tone: "blue" },
    { label: "Unsupported", value: categoryCount(plan, plan.unsupported_count, "unsupported"), tone: "rose" },
    {
      label: "Dependencies",
      value: results.dependency_graph?.edge_count ?? plan.total_dependencies ?? 0,
      tone: "slate",
    },
    {
      label: "High confidence",
      value:
        results.dependency_graph?.high_confidence_edge_count ??
        plan.high_confidence_dependencies ??
        results.dependency_graph?.edge_count ??
        0,
      tone: "indigo",
    },
    { label: "Waves", value: plan.waves?.length ?? plan.wave_count ?? 0, tone: "purple" },
  ];

  const toneText: Record<Tone, string> = {
    slate: "text-slate-900",
    brand: "text-brand-700",
    emerald: "text-emerald-700",
    amber: "text-amber-700",
    rose: "text-rose-700",
    blue: "text-blue-700",
    indigo: "text-indigo-700",
    purple: "text-purple-700",
  };

  const riskHigh = plan.risk_score > 30;

  return (
    <div className="card overflow-hidden">
      <div className="card-header">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Layers className="w-4 h-4 text-brand-600" />
            Adoption plan
          </h2>
          <p className="text-2xs text-slate-500 mt-0.5">
            Resource classification, dependency confidence, and the order to migrate in.
          </p>
        </div>
        <div className="text-right shrink-0">
          <div className="text-3xs uppercase tracking-wider font-semibold text-slate-500">Risk score</div>
          <div className={`text-lg font-bold tabular-nums ${riskHigh ? "text-amber-600" : "text-emerald-600"}`}>
            {plan.risk_score}
            <span className="text-xs text-slate-400 font-medium">/100</span>
          </div>
        </div>
      </div>

      <div className="p-5 space-y-5">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-px bg-slate-100 rounded-xl overflow-hidden border border-slate-100">
          {metrics.map((m) => (
            <div key={m.label} className="bg-white px-4 py-3">
              <div className={`text-xl font-bold tabular-nums ${toneText[m.tone]}`}>{m.value}</div>
              <div className="text-3xs uppercase tracking-wider font-semibold text-slate-500 mt-0.5">{m.label}</div>
            </div>
          ))}
        </div>

        {plan.summary && (
          <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 text-xs text-slate-700 leading-relaxed">
            <span className="font-bold text-slate-900">Summary: </span>
            {plan.summary}
          </div>
        )}

        {plan.waves && plan.waves.length > 0 && (
          <div className="space-y-3">
            <h3 className="section-title">Migration waves</h3>
            <ol className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {plan.waves.map((w) => (
                <li
                  key={w.wave}
                  className="p-3.5 rounded-xl border border-slate-200 bg-white hover:border-brand-300 transition-colors space-y-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-bold text-2xs bg-slate-900 text-white px-2 py-0.5 rounded-md shrink-0">
                        Wave {w.wave}
                      </span>
                      <span className="text-xs font-semibold text-slate-800 truncate">
                        {w.category_name || `Stage ${w.wave}`}
                      </span>
                    </div>
                    <span
                      className={`text-3xs font-bold uppercase px-2 py-0.5 rounded-full border shrink-0 ${riskBadgeClass(
                        w.risk_level
                      )}`}
                    >
                      {w.risk_level} risk
                    </span>
                  </div>
                  <div className="text-2xs text-slate-500">
                    {w.resource_ids.length} resource{w.resource_ids.length === 1 ? "" : "s"}
                  </div>
                  <div className="text-2xs text-slate-500 font-mono break-all line-clamp-2">
                    {w.resource_ids.join(", ")}
                  </div>
                  {w.risk_signals && w.risk_signals.length > 0 && (
                    <div className="text-3xs text-amber-800 bg-amber-50 p-1.5 rounded-md border border-amber-100">
                      {w.risk_signals.join("; ")}
                    </div>
                  )}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}

export default async function JobResultsPage({ params }: ResultsPageProps) {
  const { id } = await params;
  let results: JobResults;
  let isDemo = false;
  try {
    results = await fetchJobResults(id);
  } catch {
    results = demoJobResults(id);
    isDemo = true;
  }

  const security = results.security_results;
  const findingsCount = security?.findings?.length ?? 0;
  const safety = results.migration_safety;
  const posture = results.security_posture;
  const safetyTone: Tone =
    !safety || safety.score === null
      ? "slate"
      : safety.status === "SAFE"
      ? "emerald"
      : safety.status === "DESTRUCTIVE"
      ? "rose"
      : "amber";
  const postureTone: Tone =
    !posture || posture.score === null
      ? "slate"
      : posture.rating === "GOOD"
      ? "emerald"
      : posture.rating === "POOR"
      ? "rose"
      : "amber";
  const validationPassed = results.validation_results?.passed ?? false;
  const checksCount = results.validation_results?.checks?.length ?? 0;
  const edgeCount = results.dependency_graph?.edge_count ?? 0;
  const nodeCount = results.dependency_graph?.node_count ?? results.dependency_graph?.nodes?.length ?? 0;

  const tabs = [
    {
      id: "overview",
      label: "Overview",
      icon: <LayoutGrid className="w-3.5 h-3.5" />,
      content: (
        <>
          <ScoresPanel safety={safety} posture={posture} />
          <DecisionsPanel model={results.infra_model} />
          {results.adoption_plan ? (
            <AdoptionPlanCard results={results} plan={results.adoption_plan} />
          ) : (
            <div className="card p-10 text-center text-xs text-slate-500">
              No adoption plan was produced for this job ({results.operation} mode).
            </div>
          )}
        </>
      ),
    },
    {
      id: "inventory",
      label: "Inventory",
      icon: <Globe2 className="w-3.5 h-3.5" />,
      badge: results.resource_inventory?.available
        ? `${results.resource_inventory.total ?? 0}${results.resource_inventory.truncated ? "+" : ""}`
        : undefined,
      content: (
        <InventoryPanel
          inventory={results.resource_inventory}
          scannedRegion={results.region}
          requestedRegion={results.requested_region}
        />
      ),
    },
    {
      id: "topology",
      label: "Topology",
      icon: <Network className="w-3.5 h-3.5" />,
      badge: nodeCount,
      content: (
        <>
          <SectionHeading
            title="Dependency graph"
            description="Drag nodes to rearrange, scroll to zoom, hover for tags."
            action={
              <Link href={`/results/${id}/graph`} className="btn-secondary py-2 shrink-0">
                <Maximize2 className="w-3.5 h-3.5" />
                Full screen
              </Link>
            }
          />
          <DependencyGraph data={results.dependency_graph} />
        </>
      ),
    },
    {
      id: "verification",
      label: "Verification",
      icon: <ShieldCheck className="w-3.5 h-3.5" />,
      badge: findingsCount > 0 ? findingsCount : undefined,
      content: (
        <ValidationReport
          validationResults={results.validation_results}
          securityResults={results.security_results}
          resources={results.resources}
        />
      ),
    },
    {
      id: "deliverables",
      label: "Pull request & bundle",
      icon: <Package className="w-3.5 h-3.5" />,
      content: (
        <>
          <CreatePullRequestAction
            jobId={id}
            status={results.status}
            githubPr={results.github_pr}
            githubWavePrs={results.github_wave_prs}
            adoptionPlan={results.adoption_plan}
            githubHardeningPr={results.github_hardening_pr}
            hardening={results.hardening}
          />
          <HardeningPanel hardening={results.hardening} />
          <ZipDownload
            jobId={id}
            downloadUrl={results.download_url}
            manifest={results.zip_manifest}
            sha256={results.zip_sha256}
            status={results.status}
          />
        </>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <PageHeader
        breadcrumbs={[{ label: "Dashboard", href: "/" }, { label: "Results" }]}
        title={
          <>
            <span>Scan results</span>
            <span className="font-mono text-xs font-semibold text-brand-700 bg-brand-50 px-2.5 py-1 rounded-lg border border-brand-200">
              {id}
            </span>
            <StatusBadge status={results.status} size="md" />
          </>
        }
        meta={
          <div className="flex items-center gap-x-4 gap-y-1 text-xs text-slate-500 flex-wrap">
            <span className="inline-flex items-center gap-1.5">
              <Globe className="w-3.5 h-3.5 text-slate-400" />
              <span className="font-mono text-slate-700">{results.region}</span>
              {results.requested_region === "auto" && <span className="text-slate-400">(auto)</span>}
            </span>
            <span className="capitalize">{results.operation} mode</span>
          </div>
        }
        actions={
          <>
            <Link href="/scan" className="btn-secondary">
              New scan
            </Link>
            {results.status === "COMPLETE" && !isDemo && (
              <>
                <a
                  href={results.download_url || `/api/download/${id}`}
                  download={`terraagent_${id}.zip`}
                  className="btn-secondary"
                >
                  <Download className="w-4 h-4" />
                  ZIP
                </a>
                {results.github_pr ? (
                  <a href={results.github_pr.pr_url} target="_blank" rel="noopener noreferrer" className="btn-primary">
                    <GitPullRequestArrow className="w-4 h-4" />
                    Adoption PR #{results.github_pr.pr_number}
                  </a>
                ) : (
                  <a href="#deliverables" className="btn-primary">
                    <GitPullRequestArrow className="w-4 h-4" />
                    Open pull request
                  </a>
                )}
              </>
            )}
          </>
        }
      />

      {isDemo && (
        <div className="p-3.5 rounded-xl border border-amber-200 bg-amber-50 text-xs text-amber-900 flex items-start gap-2.5">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
          <div>
            <span className="font-bold">Demo data.</span> The API could not be reached, so this page is showing sample
            output rather than the results of job <span className="font-mono">{id}</span>.
          </div>
        </div>
      )}

      {/* Human approval gate - pinned above the tabs so it's never hidden */}
      <ResultsApprovalSection
        jobId={id}
        status={results.status}
        pendingApproval={results.pending_approval}
        approvalRequest={results.approval_request}
        planEquivalenceResults={results.plan_equivalence_results}
        approvalDecision={results.approval_decision}
      />

      {/* At-a-glance KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 sm:gap-4">
        <StatCard label="Resources" value={results.resources_count} icon={Boxes} tone="brand" compact hint={`${nodeCount} graph nodes`} />
        <StatCard
          label="Validation"
          value={checksCount === 0 ? "—" : validationPassed ? "Passed" : "Failed"}
          icon={validationPassed ? CheckCheck : XCircle}
          tone={checksCount === 0 ? "slate" : validationPassed ? "emerald" : "rose"}
          compact
          hint={`${checksCount} check${checksCount === 1 ? "" : "s"}`}
        />
        <StatCard
          label="Migration safety"
          value={safety && safety.score !== null ? `${safety.score}%` : "—"}
          icon={ArrowLeftRight}
          tone={safetyTone}
          compact
          hint={safety ? `${safety.status.toLowerCase()} · ${safety.destroy_or_replace} destroy/replace` : "not measured"}
        />
        <StatCard
          label="Security posture"
          value={posture && posture.score !== null ? `${posture.score}/100` : "—"}
          icon={ShieldCheck}
          tone={postureTone}
          compact
          hint={posture ? `${findingsCount} finding${findingsCount === 1 ? "" : "s"}${posture.complete ? "" : " · partial scan"}` : "not measured"}
        />
        <StatCard label="Dependencies" value={edgeCount} icon={Network} tone="indigo" compact hint={results.dependency_graph?.is_dag ? "acyclic graph" : "cycles detected"} />
      </div>

      <ResultsTabs tabs={tabs} />
    </div>
  );
}
