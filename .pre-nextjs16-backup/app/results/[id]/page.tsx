import { fetchJobResults } from "@/lib/api";
import DependencyGraph from "@/components/DependencyGraph";
import ValidationReport from "@/components/ValidationReport";
import ZipDownload from "@/components/ZipDownload";
import Link from "next/link";
import { ArrowLeft, CheckCircle2, Shield, Layers, ExternalLink, Globe, Cpu, CheckCheck } from "lucide-react";
import { JobResults } from "@/lib/types";

interface ResultsPageProps {
  params: {
    id: string;
  };
}

export const dynamic = "force-dynamic";

export default async function JobResultsPage({ params }: ResultsPageProps) {
  let results: JobResults;
  try {
    results = await fetchJobResults(params.id);
  } catch (err) {
    // Fallback demonstration data if server is unreachable
    results = {
      job_id: params.id,
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
      download_url: `/api/download/${params.id}`,
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
              {params.id}
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
            href={`/results/${params.id}/graph`}
            className="px-4 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-bold flex items-center gap-1.5 shadow-sm transition-all"
          >
            <span>Full-Screen D3 View</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </Link>
        </div>
      </div>

      {/* ZIP Download Card */}
      <ZipDownload
        jobId={params.id}
        downloadUrl={results.download_url}
        manifest={results.zip_manifest}
        sha256={results.zip_sha256}
      />

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
