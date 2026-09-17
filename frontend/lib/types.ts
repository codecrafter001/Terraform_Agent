export type OperationType = 'generate' | 'scan' | 'explain' | 'validate';

export type JobStatus = 'PENDING' | 'RUNNING' | 'COMPLETE' | 'FAILED' | 'AWAITING_APPROVAL' | 'REJECTED';

export interface ScanRequest {
  aws_access_key: string;
  aws_secret_key: string;
  aws_session_token?: string;
  region: string;
  operation: OperationType;
  resource_filters: string[];
}

export interface ScanResponse {
  job_id: string;
  status: JobStatus;
  created_at: string;
  operation: OperationType;
  region: string;
  message?: string;
}

export interface JobProgress {
  job_id: string;
  status: JobStatus;
  progress_percentage: number;
  current_agent?: string;
  completed_agents: string[];
  created_at: string;
  updated_at?: string;
  error?: string;
}

export interface LogMessage {
  job_id: string;
  message: string;
  agent: string;
  level: 'INFO' | 'WARNING' | 'ERROR' | 'SUCCESS';
  data?: Record<string, unknown>;
}

export interface GraphNode {
  id: string;
  name: string;
  type: string;
  category: string;
  tags?: Array<{ Key: string; Value: string }>;
}

export interface GraphLink {
  source: string;
  target: string;
  relation: string;
  relationship_type?: string;
  confidence?: number;
  evidence?: string;
  is_trusted?: boolean;
}

export interface DependencyGraphData {
  nodes: GraphNode[];
  links: GraphLink[];
  is_dag: boolean;
  node_count: number;
  edge_count: number;
  high_confidence_edge_count?: number;
  advisory_edge_count?: number;
  cycles?: string[][];
  topological_order: string[];
}

export interface SecurityFinding {
  tool: string;
  rule_id: string;
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';
  description: string;
  resource?: string;
  file?: string;
  line?: number;
  remediation?: string;
}

export interface SecurityReport {
  passed: boolean;
  risk_score: number;
  critical_count: number;
  high_count: number;
  medium_count: number;
  low_count: number;
  findings: SecurityFinding[];
  // Scanner binaries (tfsec/checkov/trivy/conftest) not found on PATH in this
  // environment - the risk score above does NOT reflect these tools at all,
  // so a non-empty list here means "not verified", never "verified clean".
  scanners_skipped?: string[];
  compliance_summary?: {
    checkov?: { passed: number; failed: number; skipped: number };
  };
}

export interface ValidationCheck {
  check_name: string;
  passed: boolean;
  output?: string;
  errors?: string[];
  warnings?: string[];
}

export interface ValidationReportData {
  passed: boolean;
  checks: ValidationCheck[];
}

export interface ZipManifestEntry {
  name: string;
  size: number;
}

export interface PendingApprovalFinding {
  tool: string;
  rule_id: string;
  severity: string;
  description: string;
  resource?: string;
  tier: 'safe_auto' | 'behavior_changing' | 'destructive';
}

export interface PendingApproval {
  reason: string;
  findings: PendingApprovalFinding[];
}

export interface ApprovalDecision {
  decision: 'approved' | 'rejected';
  reason?: string | null;
  decided_at: string;
}

export interface PlanEquivalenceResult {
  passed?: boolean;
  skipped?: boolean;
  reason?: string;
  create?: number;
  update?: number;
  replace?: number;
  destroy?: number;
  no_op?: number;
  blocking_actions?: Array<{ address: string; action: string }>;
  checks?: ValidationCheck[];
}

export interface GithubPrInfo {
  pr_url: string;
  pr_number: number;
  branch?: string;
  wave?: number | null;
}

export interface AdoptionWave {
  wave: number;
  category_name?: string;
  resource_ids: string[];
  risk_level: 'low' | 'medium' | 'high';
  risk_signals: string[];
}

export interface AdoptionCategoryPlan {
  category: 'safe_to_import' | 'review_required' | 'do_not_manage' | 'use_data_source' | 'unsupported';
  resource_ids: string[];
  resource_count: number;
}

export interface AdoptionPlan {
  categories: AdoptionCategoryPlan[];
  total_resource_count: number;
  managed_count?: number;
  review_count?: number;
  data_source_count?: number;
  unsupported_count?: number;
  do_not_manage_count?: number;
  total_dependencies?: number;
  high_confidence_dependencies?: number;
  wave_count?: number;
  risk_score: number;
  import_order: string[];
  waves: AdoptionWave[];
  cycles_detected?: string[][];
  summary?: string | null;
}

export interface JobResults {
  job_id: string;
  status: JobStatus;
  operation: OperationType;
  region: string;
  resources_count: number;
  resources: Array<Record<string, unknown>>;
  dependency_graph: DependencyGraphData;
  adoption_plan?: AdoptionPlan;
  validation_results: ValidationReportData;
  plan_equivalence_results?: PlanEquivalenceResult;
  security_results: SecurityReport;
  pending_approval?: PendingApproval | null;
  approval_decision?: ApprovalDecision | null;
  github_pr?: GithubPrInfo | null;
  github_wave_prs?: Record<string, GithubPrInfo>;
  zip_available: boolean;
  download_url?: string;
  zip_sha256?: string;
  zip_manifest: ZipManifestEntry[];
}
