import { DependencyGraphData, JobResults } from "./types";

// Fallback demonstration data used by the results and full-graph pages when
// the backend is unreachable. Pages that use it must say so visibly - it is
// never real scan output.

export const DEMO_DEPENDENCY_GRAPH: DependencyGraphData = {
  nodes: [
    { id: "vpc-0a1b2c3d4e5f", name: "prod-main-vpc", type: "aws_vpc", category: "Networking" },
    { id: "subnet-0123456789abcdef", name: "prod-public-subnet-1", type: "aws_subnet", category: "Networking" },
    { id: "sg-0feebda1234", name: "web-tier-sg", type: "aws_security_group", category: "Security" },
    { id: "i-0987654321fedcba0", name: "api-gateway-node", type: "aws_instance", category: "Compute" },
    { id: "app-production-assets-2026", name: "app-production-assets-2026", type: "aws_s3_bucket", category: "Storage" },
  ],
  links: [
    { source: "subnet-0123456789abcdef", target: "vpc-0a1b2c3d4e5f", relation: "contains" },
    { source: "i-0987654321fedcba0", target: "subnet-0123456789abcdef", relation: "hosted_in" },
    { source: "i-0987654321fedcba0", target: "sg-0feebda1234", relation: "secured_by" },
  ],
  is_dag: true,
  node_count: 5,
  edge_count: 3,
  topological_order: [
    "vpc-0a1b2c3d4e5f",
    "subnet-0123456789abcdef",
    "sg-0feebda1234",
    "i-0987654321fedcba0",
    "app-production-assets-2026",
  ],
};

export function demoJobResults(id: string): JobResults {
  return {
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
      { id: "app-production-assets-2026", resource_type: "aws_s3_bucket", name: "app-production-assets-2026" },
    ],
    dependency_graph: DEMO_DEPENDENCY_GRAPH,
    validation_results: {
      passed: true,
      checks: [
        { check_name: "terraform_fmt", passed: true, output: "All files formatted cleanly." },
        { check_name: "terraform_init", passed: true, output: "Terraform has been successfully initialized." },
        { check_name: "terraform_validate", passed: true, output: "Success! The configuration is valid." },
      ],
    },
    security_results: {
      passed: true,
      risk_score: 0,
      critical_count: 0,
      high_count: 0,
      medium_count: 0,
      low_count: 0,
      findings: [],
    },
    zip_available: true,
    download_url: `/api/download/${id}`,
    zip_sha256: undefined,
    zip_manifest: [],
  };
}
