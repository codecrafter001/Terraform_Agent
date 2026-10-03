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

// backend/tools/run_summary.py - kept on the audit record; no command output.
export interface RunCommand {
  name: string;
  command: string;
  passed: boolean;
}

export interface RunStage {
  stage: "validation" | "plan" | "generate_config";
  skipped: boolean;
  reason?: string | null;
  seconds?: number | null;
  commands: RunCommand[];
  counts?: { create: number | null; update: number | null; replace: number | null; destroy: number | null; imported: number | null };
  mismatches?: number;
}

export interface RunIteration {
  iteration: number;
  verdict: VerificationVerdict;
  validation_passed?: boolean;
  passed?: boolean;
  at?: string;
}

export interface RunsSummary {
  engine: string;
  verdict?: VerificationVerdict | null;
  repair_attempts: number;
  iterations: RunIteration[];
  stages: RunStage[];
}

export interface JobRunsEntry {
  job_id: string;
  operation: string;
  region: string;
  status: string;
  created_at: string;
  completed_at?: string | null;
  runs: RunsSummary;
}

// backend/routers/settings.py
export interface WorkspaceDefaults {
  region: string;
  resource_filters: string[];
  terraform_binary: "terraform" | "tofu";
  github_repo?: string | null;
  base_branch: string;
}

export interface WorkspaceSettings {
  defaults: WorkspaceDefaults;
  configuration: {
    safety: {
      allowed_terraform_subcommands: string[];
      blocked_terraform_commands: string[];
      aws_api_access: string;
      api_key_required: boolean;
    };
    pipeline: Record<string, number | boolean>;
    llm: { provider: string; model: string };
    integrations: { infracost_api_key_set: boolean };
    tools: Record<string, boolean>;
  };
}

// ---------------------------------------------------------------------------
// Deployment mode (backend/deploy/, routers/deployments.py)
// ---------------------------------------------------------------------------

// backend/deploy/store.py::DeployStatus
export type DeploymentStatus =
  | 'SOURCE_RECEIVED'
  | 'ANALYZING'
  | 'ANALYZED'
  | 'BUILDING'
  | 'VERIFYING'
  | 'VERIFIED'
  | 'PLANNING'
  | 'AWAITING_APPROVAL'
  | 'APPROVED'
  | 'PR_OPEN'
  | 'MERGED'
  | 'APPLYING'
  | 'DEPLOYED'
  | 'DESTROY_PLANNING'
  | 'DESTROYING'
  | 'DESTROYED'
  | 'FAILED_PARTIAL'
  | 'NEEDS_RECONCILIATION'
  | 'REJECTED'
  | 'EXPIRED'
  | 'FAILED';

export type DeploymentTarget = 'static_site' | 'lambda_http' | 'ecs_service' | 'fullstack_app';

export interface AwsDeployTarget {
  id: string;
  name: string;
  account_id: string;
  region: string;
  plan_role_arn: string;
  apply_role_arn: string;
  permissions_boundary_arn?: string | null;
  state_bucket: string;
  external_id: string;
  verified_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AwsDeployTargetCreate {
  name: string;
  account_id: string;
  region: string;
  plan_role_arn: string;
  apply_role_arn: string;
  permissions_boundary_arn?: string | null;
  state_bucket: string;
}

export interface AwsDeployTargetVerifyResult {
  target_id: string;
  verified: boolean;
  plan_role_ok: boolean;
  apply_role_ok: boolean;
  bucket_ok: boolean;
  caller_identity?: { account?: string; arn?: string; user_id?: string } | null;
  message: string;
}

export interface DeployEvidence {
  file: string;
  rule: string;
}

// backend/deploy/analyzer.py::ProjectProfile
export interface ProjectProfile {
  runtime: 'python' | 'node' | 'static' | 'container' | 'unknown';
  runtime_version: string | null;
  framework: string | null;
  lambda_handler: string | null;
  server_entrypoint: boolean;
  listens_on_port: number | null;
  build_required: boolean;
  static_output_dir: string | null;
  dependency_manifest: string | null;
  dependencies: string[];
  has_lockfile: boolean;
  native_dependencies: string[];
  source_bytes: number;
  has_dockerfile: boolean;
  evidence: Record<string, DeployEvidence[]>;
  warnings: string[];
  fullstack?: FullstackLayout | null;
}

// backend/deploy/fullstack.py::FullstackLayout
export interface FullstackLayout {
  backend: {
    dir: string;
    runtime: string;
    framework: string | null;
    port: number;
    has_dockerfile: boolean;
    uses_api_prefix: boolean;
  };
  frontend: {
    dir: string;
    framework: string | null;
    build_required: boolean;
    static_output_dir: string | null;
    api_url_env: string[];
  } | null;
  database: {
    engine: 'postgres' | 'mysql' | 'mongodb' | 'sqlite';
    url_scheme: string | null;
    rds_supported: boolean;
    evidence: DeployEvidence[];
  } | null;
  migration: { command: string[]; evidence: DeployEvidence[] } | null;
  env_keys: string[];
  warnings: string[];
}

// backend/deploy/decision_engine.py::Decision
export interface DecisionReason {
  code: string;
  target: string | null;
  message: string;
}

export interface DeploymentDecision {
  rules_version: number;
  eligible: DeploymentTarget[];
  recommended: DeploymentTarget | null;
  reasons: DecisionReason[];
  blocked: boolean;
}

export interface SecretHit {
  path: string;
  line: number;
  kind: string;
}

export interface DeploymentIntake {
  file_count: number;
  total_bytes: number;
  dropped_count: number;
  dropped: { path: string; reason: string }[];
  stripped_prefix: string | null;
  secret_hits?: SecretHit[];
}

export interface DeploymentBuild {
  kind: 'static_site' | 'lambda_zip';
  runtime: string | null;
  handler: string | null;
  file_count: number;
  package_bytes: number;
  package_sha256_b64: string | null;
  warnings: string[];
  log: string[];
}

export interface DeploymentCost {
  tool_skipped: boolean;
  total_monthly_cost?: number;
  currency?: string;
  resources?: { name: string; resource_type: string; monthly_cost: number }[];
}

// backend/deploy/verify.py
export interface DeploymentVerification {
  verdict: 'PASS' | 'INCOMPLETE' | 'FAIL';
  incomplete_reasons: string[];
  validation: { passed: boolean; checks: ValidationCheck[] };
  security: SecurityReport & { findings_truncated?: boolean; scanners_failed?: Record<string, string> };
  security_posture: SecurityPosture;
  cost: DeploymentCost;
}

export interface StaticSiteSettings {
  price_class: 'PriceClass_100' | 'PriceClass_200' | 'PriceClass_All';
  spa_mode: boolean;
}

export interface LambdaSettings {
  memory_mb: number;
  timeout_s: number;
  public_url: boolean;
}

export interface EcsSettings {
  container_port?: number;
  cpu?: number;
  memory_mb?: number;
  desired_count?: number;
  image_tag?: string;
  health_check_path?: string;
  certificate_arn?: string | null;
}

// backend/models/deployment.py::FullstackSettings
export type FullstackDatabaseMode = 'rds' | 'external' | 'none';
export type FullstackPreset = 'dev' | 'staging' | 'production';
export interface FullstackSettings {
  preset?: FullstackPreset | null;
  cdn_enabled: boolean;
  container_port?: number;
  cpu: number;
  memory_mb: number;
  desired_count: number;
  health_check_path: string;
  price_class: 'PriceClass_100' | 'PriceClass_200' | 'PriceClass_All';
  database: FullstackDatabaseMode;
  db_instance_class: 'db.t4g.micro' | 'db.t4g.small' | 'db.t4g.medium' | 'db.t4g.large' | 'db.m7g.large';
  db_allocated_storage_gb: number;
  db_multi_az: boolean;
  db_backup_retention_days: number;
  db_final_snapshot: boolean;
  run_migrations: boolean;
  secret_env_keys: string[];
}

// backend/deploy/estimates.py::Estimate and POST /deployments/{id}/estimate
export interface DeploymentEstimate {
  monthly_usd: number;
  lines: { item: string; monthly_usd: number }[];
  minutes_low: number;
  minutes_high: number;
  cdn: boolean;
  notes: string[];
}

export type FullstackPresetValues = Pick<
  FullstackSettings,
  'cdn_enabled' | 'cpu' | 'memory_mb' | 'desired_count' | 'db_instance_class' | 'db_allocated_storage_gb' |
  'db_multi_az' | 'db_backup_retention_days' | 'db_final_snapshot'
>;

export interface DeploymentEstimateResponse {
  estimate: DeploymentEstimate;
  presets: Record<FullstackPreset, FullstackPresetValues>;
  preset_descriptions: Record<FullstackPreset, string>;
  suggested_preset: FullstackPreset;
}

export type PrepareDeploymentPayload =
  | { target: 'static_site'; settings: StaticSiteSettings }
  | { target: 'lambda_http'; settings: LambdaSettings }
  | { target: 'ecs_service'; settings: EcsSettings }
  | { target: 'fullstack_app'; settings: FullstackSettings };

export interface PlanResourceChange {
  address: string;
  action: 'create' | 'update' | 'replace' | 'destroy' | 'delete' | string;
  type?: string;
}

export interface PlanSummary {
  counts: {
    create: number;
    update: number;
    replace: number;
    destroy: number;
    no_op?: number;
  };
  changes: PlanResourceChange[];
  is_destructive: boolean;
  // backend/deploy/code_update.py::is_code_only - only the new source, image and task revision change
  code_only?: boolean;
}

export interface PlanPolicyResult {
  passed: boolean;
  violations: string[];
  warnings: string[];
  is_destructive: boolean;
  destructive_changes: PlanResourceChange[];
  summary?: {
    resource_count: number;
    target_type: string;
    target_allowed: boolean;
  };
}

export interface ApprovalPayload {
  plan_bundle_sha256: string;
  confirm: boolean;
  acknowledge_destructive?: boolean;
  reason?: string;
}

export interface RejectionPayload {
  reason?: string;
}

export interface DeploymentPullRequestInfo {
  number: number;
  html_url: string;
  branch: string;
  repo: string;
  commit_sha: string;
  base_branch: string;
  title: string;
  created_at?: string;
}

export interface CreatePullRequestPayload {
  github_token: string;
  repo: string;
  base_branch?: string;
  target_dir?: string;
  add_workflows?: boolean;
}

export interface MergePullRequestPayload {
  github_token: string;
  merge_method?: 'squash' | 'merge' | 'rebase';
  commit_title?: string;
  commit_message?: string;
}

export interface DeploymentSummary {
  id: string;
  status: DeploymentStatus;
  source_kind: 'zip' | 'github';
  source_name: string;
  region: string;
  environment: string;
  target_type: DeploymentTarget | null;
  target_id?: string | null;
  plan_bundle_sha256?: string | null;
  plan_kind?: 'plan' | 'destroy' | string | null;
  is_destructive?: boolean;
  approved_by?: string | null;
  approved_at?: string | null;
  applied_at?: string | null;
  verdict: DeploymentVerification['verdict'] | null;
  error: string | null;
  pr?: DeploymentPullRequestInfo | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface DeploymentEvent {
  from_status: DeploymentStatus | null;
  to_status: DeploymentStatus;
  actor: string;
  reason: string | null;
  created_at: string;
}

export interface RollbackPayload {
  target_artifact_id?: string;
  target_release_id?: string;
  reason?: string;
}

export interface BuildHistoryItem {
  artifact_id: string;
  deployment_id: string;
  kind: string;
  sha256: string;
  size: number;
  created_at: string;
  release_id?: string | null;
}

export interface DeploymentDetail extends DeploymentSummary {
  source_sha256: string | null;
  requested_by?: string | null;
  intake: DeploymentIntake | null;
  profile: ProjectProfile | null;
  decision: DeploymentDecision | null;
  settings: Partial<StaticSiteSettings & LambdaSettings & EcsSettings & FullstackSettings> | null;
  build: DeploymentBuild | null;
  verification: DeploymentVerification | null;
  plan?: Record<string, unknown> | null;
  plan_summary?: PlanSummary | null;
  plan_policy?: PlanPolicyResult | null;
  outputs?: Record<string, unknown> | null;
  approval_reason?: string | null;
  rejection_reason?: string | null;
  rendered_files: string[];
  events: DeploymentEvent[];
  can_prepare: boolean;
  can_plan: boolean;
  can_approve: boolean;
  can_deploy: boolean;
  can_open_pr?: boolean;
  can_merge_pr?: boolean;
  can_rollback?: boolean;
  can_destroy?: boolean;
  can_update_code?: boolean;
  // backend/deploy/code_update.py: present while new code is being rolled out to this deployment
  code_update?: {
    active: boolean;
    started_at: string;
    requested_by: string | null;
    previous_source_sha256: string | null;
    previous_image_tag: string | null;
  } | null;
}

export interface DeploymentAccepted {
  deployment_id: string;
  status: DeploymentStatus;
  message: string;
}


