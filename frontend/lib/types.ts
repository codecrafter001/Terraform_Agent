export type OperationType = 'generate' | 'scan' | 'modify' | 'explain' | 'fix' | 'validate';

export type JobStatus = 'PENDING' | 'RUNNING' | 'COMPLETE' | 'FAILED' | 'AWAITING_APPROVAL' | 'REJECTED';

export interface IntentTargetResource {
  resource_type: string;
  resource_name: string;
  category: string;
}

export interface IntentRequestedChange {
  resource: string;
  attribute: string;
  current_value: string;
  target_value: string;
  action: string;
}

export interface IntentAnalysisResult {
  operation: OperationType | string;
  operation_label: string;
  target_resources: IntentTargetResource[];
  requested_changes: IntentRequestedChange[];
  confidence_score: number;
  summary: string;
  environment: string;
  region: string;
  risk_level: 'low' | 'medium' | 'high';
  suggested_filters: string[];
}

export interface ScanRequest {
  aws_access_key: string;
  aws_secret_key: string;
  aws_session_token?: string;
  region: string;
  environment?: string;
  user_request?: string;
  analyzed_intent?: IntentAnalysisResult;
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
  // Agent-level progress - see backend/agents/graph.py
  current_stage?: StageId | 'awaiting_approval' | 'complete' | null;
  completed_stages?: StageId[];
  stage_summaries?: Partial<Record<StageId, string>>;
  verification_iterations?: VerificationIteration[];
  repair_attempts?: number;
  max_repair_iterations?: number | null;
  verification_verdict?: VerificationVerdict | null;
  repair_history?: RepairEntry[];
  created_at: string;
  updated_at?: string;
  error?: string;
}

export type StageId = 'infrastructure' | 'iac_engineering' | 'verification' | 'delivery';

export type VerificationVerdict = 'PASS' | 'FAIL' | 'INCOMPLETE' | 'NEEDS_APPROVAL';

export interface RepairEntry {
  cycle: number;
  errors: number;
  fixed: string[];
  rejected: { address: string; violations: string[] }[];
  unresolved: string[];
}

export interface VerificationIteration {
  iteration: number;
  verdict: VerificationVerdict;
  checks_run: string[];
  incomplete_reasons: string[];
  imported?: number | null;
  config_mismatches?: number | null;
  validation_passed: boolean;
  system_failure: boolean;
  high_findings: number;
  total_findings: number;
  plan_changes: number | null;
  drift_findings: number;
  halted_for_approval: boolean;
  passed: boolean;
  at: string;
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

export type HumanChoice = 'manage' | 'reference' | 'exclude';

export interface ApprovalDecision {
  decision: 'approved' | 'rejected';
  reason?: string | null;
  decided_at: string;
  resource_decisions?: Record<string, HumanChoice>;
}

// What the Delivery & Approval Agent's risk gate is waiting on - backend/agents/graph.py::approval_request
export interface ReviewResource {
  resource_id: string;
  resource_type: string;
  category?: string | null;
  reasons: string[];
  evidence: Record<string, unknown>;
  choices: HumanChoice[];
}

export interface ApprovalRequest {
  findings: PendingApprovalFinding[];
  review_resources: ReviewResource[];
  verdict?: VerificationVerdict | null;
}

// backend/tools/scores.py - kept separate on purpose
export interface MigrationSafety {
  score: number | null;
  status: 'SAFE' | 'CHANGES' | 'DESTRUCTIVE' | 'UNVERIFIED';
  basis: 'plan' | 'drift' | 'none';
  reason: string;
  resources_managed: number;
  no_op?: number;
  changing_resources: string[];
  destroy_or_replace: number;
  imported?: number;
  config_mismatches?: number | null;
}

export interface SecurityPosture {
  score: number | null;
  rating: 'GOOD' | 'FAIR' | 'POOR' | 'UNKNOWN';
  complete: boolean;
  scanners_run: string[];
  scanners_missing: string[];
  scanners_failed: Record<string, string>;
  counts: { critical: number; high: number; medium: number; low: number };
  total_findings: number;
  reason: string;
}

// backend/agents/cost_agent.py - only estimated for the Hardening proposal
export interface CostResults {
  skipped?: boolean;
  reason?: string;
  tool_skipped?: boolean;
  currency?: string;
  baseline_monthly_cost?: number | null;
  hardened_monthly_cost?: number | null;
  monthly_delta?: number | null;
}

// backend/tools/hardening.py
export interface HardeningChange {
  kind: string;
  resource: string;
  file: string;
  title: string;
  explanation: string;
  impact: 'safe' | 'behavior_changing';
  risk: string;
  findings: string[];
}

export interface HardeningRecommendation {
  tool?: string;
  rule_id?: string;
  severity?: string;
  resource?: string;
  description?: string;
  reason: string;
}

export interface Hardening {
  files?: Record<string, string>;
  changes?: HardeningChange[];
  recommendations?: HardeningRecommendation[];
  validated?: boolean | null;
  rejected_reason?: string | null;
  cost?: CostResults;
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
  pr_title?: string;
  branch?: string;
  repo?: string;
  base_branch?: string;
  commit_sha?: string;
  changed_files?: string[];
  status?: string;
  merged?: boolean;
  merged_at?: string;
  merge_commit_sha?: string;
  wave?: number | null;
  kind?: 'adoption' | 'hardening';
  created_at?: string;
}

export interface PrFileChange {
  filename: string;
  status?: string;
  additions: number;
  deletions: number;
  changes: number;
  patch?: string | null;
  raw_url?: string | null;
}

export interface PrReview {
  id?: number;
  user?: string;
  state: string;
  submitted_at?: string;
  body?: string;
}

export interface GithubWorkflowRun {
  id: number;
  name: string;
  status: string;
  conclusion?: string | null;
  html_url: string;
  created_at: string;
  event?: string;
  head_branch?: string;
  head_sha?: string;
}

export interface GithubPrDetails {
  job_id: string;
  repo: string;
  pr_number: number;
  title: string;
  state: string;
  html_url: string;
  body?: string | null;
  head_branch: string;
  base_branch: string;
  head_sha?: string | null;
  mergeable?: boolean | null;
  mergeable_state?: string | null;
  merged: boolean;
  merged_at?: string | null;
  merge_commit_sha?: string | null;
  additions: number;
  deletions: number;
  changed_files_count: number;
  changed_files: PrFileChange[];
  reviews: PrReview[];
  diff?: string | null;
  workflow_runs: GithubWorkflowRun[];
  created_at?: string;
  updated_at?: string;
}

export type PrKind = 'adoption' | 'hardening';

// POST /scan/{id}/pull-request/merge - always this job's own PR (by kind),
// only after a human approved it in GitHub, and only with confirm: true.
export interface MergePrPayload {
  github_token: string;
  kind: PrKind;
  confirm: boolean;
  merge_method?: 'squash' | 'merge' | 'rebase';
  commit_title?: string;
  commit_message?: string;
}

export interface MergePrResponse {
  job_id: string;
  merged: boolean;
  sha?: string;
  message: string;
  workflow_runs: GithubWorkflowRun[];
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
  user_request?: string | null;
  analyzed_intent?: IntentAnalysisResult | null;
  resources_count: number;
  resources: Array<Record<string, unknown>>;
  dependency_graph: DependencyGraphData;
  adoption_plan?: AdoptionPlan;
  resource_inventory?: ResourceInventory;
  infra_model?: InfraModel;
  requested_region?: string | null;
  validation_results: ValidationReportData;
  plan_equivalence_results?: PlanEquivalenceResult;
  security_results: SecurityReport;
  pending_approval?: PendingApproval | null;
  approval_request?: ApprovalRequest | null;
  approval_decision?: ApprovalDecision | null;
  migration_safety?: MigrationSafety | null;
  security_posture?: SecurityPosture | null;
  hardening?: Hardening;
  cost_results?: CostResults;
  github_pr?: GithubPrInfo | null;
  github_wave_prs?: Record<string, GithubPrInfo>;
  github_hardening_pr?: GithubPrInfo | null;
  zip_available: boolean;
  download_url?: string;
  zip_sha256?: string;
  zip_manifest: ZipManifestEntry[];
}

// Canonical Infra Model - backend/tools/infra_model.py
export type AdoptionDecision = 'manage' | 'reference' | 'exclude' | 'review';

export interface InfraRecord {
  id: string;
  type: string;
  name?: string | null;
  arn?: string | null;
  import_id?: string | null;
  region: string;
  dependencies: string[];
  stack?: string | null;
  decision: AdoptionDecision;
  category?: string | null;
  reasons: string[];
  evidence: { source_api?: string | null; discovered_at?: string; rule?: string };
}

export interface InfraModel {
  version: number;
  region: string;
  generated_at: string;
  summary: { total: number } & Record<AdoptionDecision, number>;
  records: InfraRecord[];
}

// AWS Resource Explorer inventory - backend/tools/resource_explorer.py
export interface InventoryResource {
  arn: string;
  type: string;
  region: string;
  name?: string | null;
  supported: boolean;
}

export interface ResourceInventory {
  available: boolean;
  reason?: string;
  aggregated?: boolean;
  index_region?: string;
  total?: number;
  truncated?: boolean;
  returned?: number;
  supported_total?: number;
  unsupported_total?: number;
  by_region?: Record<string, number>;
  by_type?: Record<string, number>;
  supported_by_region?: Record<string, number>;
  suggested_region?: string | null;
  resources?: InventoryResource[];
}
