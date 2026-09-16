import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import DependencyGraph from "@/components/DependencyGraph";
import { fetchJobResults } from "@/lib/api";
import { DependencyGraphData } from "@/lib/types";

interface FullGraphPageProps {
  params: {
    id: string;
  };
}

export const dynamic = "force-dynamic";

export default async function FullGraphPage({ params }: FullGraphPageProps) {
  let dependencyGraph: DependencyGraphData;
  try {
    const results = await fetchJobResults(params.id);
    dependencyGraph = results.dependency_graph;
  } catch {
    dependencyGraph = {
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
    };
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Link
          href={`/results/${params.id}`}
          className="inline-flex items-center gap-1.5 text-xs text-gray-500 hover:text-ink transition-colors"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          Back to Results
        </Link>
        <span className="font-mono text-xs text-brand-600">Job: {params.id}</span>
      </div>

      <DependencyGraph data={dependencyGraph} />
    </div>
  );
}
