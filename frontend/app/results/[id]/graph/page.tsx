import { AlertTriangle } from "lucide-react";
import DependencyGraph from "@/components/DependencyGraph";
import { PageHeader } from "@/components/ui";
import { fetchJobResults } from "@/lib/api";
import { DEMO_DEPENDENCY_GRAPH } from "@/lib/demo";
import { DependencyGraphData } from "@/lib/types";

interface FullGraphPageProps {
  params: Promise<{
    id: string;
  }>;
}

export const dynamic = "force-dynamic";

export default async function FullGraphPage({ params }: FullGraphPageProps) {
  const { id } = await params;
  let dependencyGraph: DependencyGraphData;
  let isDemo = false;
  try {
    const results = await fetchJobResults(id);
    dependencyGraph = results.dependency_graph;
  } catch {
    dependencyGraph = DEMO_DEPENDENCY_GRAPH;
    isDemo = true;
  }

  return (
    <div className="space-y-5">
      <PageHeader
        breadcrumbs={[
          { label: "Dashboard", href: "/" },
          { label: "Results", href: `/results/${id}#topology` },
          { label: "Dependency graph" },
        ]}
        title={
          <>
            <span>Dependency graph</span>
            <span className="font-mono text-xs font-semibold text-brand-700 bg-brand-50 px-2.5 py-1 rounded-lg border border-brand-200">
              {id}
            </span>
          </>
        }
        description={`${dependencyGraph.node_count ?? dependencyGraph.nodes.length} resources, ${
          dependencyGraph.edge_count ?? dependencyGraph.links.length
        } dependencies`}
      />

      {isDemo && (
        <div className="p-3.5 rounded-xl border border-amber-200 bg-amber-50 text-xs text-amber-900 flex items-start gap-2.5">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
          <span>
            <span className="font-bold">Demo data.</span> The API could not be reached, so this is a sample graph.
          </span>
        </div>
      )}

      <DependencyGraph data={dependencyGraph} tall />
    </div>
  );
}
