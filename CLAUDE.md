# CLAUDE.md — TerraAgent Project Instructions & Architecture

## Project Overview
**TerraAgent** is a stateful multi-agent system built with LangGraph, FastAPI, Next.js 14, and local LLMs (Ollama) that safely converts AWS ClickOps infrastructure into validated, modular Terraform/OpenTofu code without ever modifying live cloud resources.

---

## Hard Safety Rules & Constraints
1. **NEVER** run `terraform apply` or `terraform destroy` under any circumstance.
2. **NEVER** execute `terraform import` automatically — only generate import commands as reviewable text for human operators.
3. **NEVER** create, modify, or delete AWS resources. All AWS operations must use read-only APIs (`Describe*`, `Get*`, `List*`).
4. **NEVER** store AWS access keys, secret keys, or session tokens in any database, log file, console output, or error message.
5. **NEVER** pass raw AWS credentials to LLMs (Ollama / Anthropic / OpenAI).
6. **NEVER** run container processes as `root`. Always use the non-privileged `agent` user.
7. **NEVER** execute arbitrary shell commands constructed directly from unvalidated user input.

---

## Terraform Execution Guardrail (Mandatory for Every Agent)

Rules #1–#2 above are enforced by one real, code-level mechanism, not just convention:
`backend/tools/terraform_runner.py::check_argv`, called by `TerraformRunner.run_command` before the
subprocess ever starts: the binary must be `terraform`/`tofu`, the subcommand must be one of
`version`/`fmt`/`init`/`validate`/`plan`/`show`/`providers`, and no argv token may be
`apply`/`destroy`/`import`, bare or as a flag (`plan -destroy`) - otherwise it raises. **This is a chokepoint only for callers that actually go through it.**
Verified: five other tools (`checkov_runner.py`, `conftest_runner.py`, `trivy_runner.py`,
`tfsec_runner.py`, `infracost_runner.py`) already shell out via their own independent
`asyncio.create_subprocess_exec` calls with fixed argv, bypassing `run_command` entirely — benign
today only because none of them ever names the `terraform`/`tofu` binary.

Going forward, this is a hard requirement, not a suggestion:
- **Any agent or tool that shells out to the `terraform`/`tofu` binary must call
  `TerraformRunner.run_command` — never construct its own `asyncio.create_subprocess_exec` call
  against that binary.** No exceptions, including future agents (e.g. a drift-reconciliation or
  cost-delta agent) that might otherwise be tempted to add a quick one-off subprocess call.
- Any agent or tool that talks to a non-Terraform binary or an AWS API directly (a Cost Explorer
  client, for instance) doesn't need `run_command` itself — wrong binary — but carries the same
  underlying obligation as rule #3: read-only APIs only, and it must never become capable of
  mutating AWS or of invoking `terraform apply`/`destroy`/`import` by any path.
- `validation_agent.py`'s `_assert_no_mutating_commands` (a regex scan of generated HCL for a
  `command = "...terraform (apply|destroy|import)..."` provisioner string) is a second,
  complementary, content-level tripwire — keep it, but it's not a substitute for the argv check
  above; new agents need both where applicable.

See `docs/design/phase0-plan-drift-cost-and-confidence.md` for the full Phase 0 design lock this
section is part of, including why `plan_equivalence_agent` deliberately never reports
replace/destroy (that's the planned `drift_reconciliation_agent`'s job), what dependency-graph
`confidence` scores actually measure, and why a real cost-delta feature isn't scoped yet.

---

## Tech Stack
- **Frontend**: Next.js 14 (App Router), TypeScript (Strict Mode), Tailwind CSS, D3.js (Dependency Graphs), Lucide Icons
- **Backend API**: FastAPI (Python 3.11+ async), Pydantic v2 (SecretStr for credentials), Uvicorn
- **Agent Orchestration**: LangGraph 1.x (StateGraph, `interrupt()` + checkpointer for the approval gate)
- **Asynchronous Task Queue**: Celery + Redis 7 (broker & result backend) + Redis Pub/Sub for SSE live logs
- **Local LLM**: Ollama (`codellama`, `llama3`) running locally / containerized
- **Security & Validation Pipeline**: Terraform CLI (`fmt`, `init`, `validate`), `Checkov`, `Trivy` (includes the former tfsec rules), `Conftest` (OPA policies)
- **Containerization & Ingress**: Docker, Docker Compose, Nginx Reverse Proxy

---

## 4-Agent LangGraph Architecture (`backend/agents/graph.py`)

Rule: something is an **agent** only if it's a stage the UI presents as a unit of reasoning; the
per-concern node functions it runs are **steps** (mostly deterministic tool calls). Each step
module stays individually unit-tested; the graph only wires agents.

```
infrastructure -> iac_engineering -> verification --PASS / INCOMPLETE / NEEDS_APPROVAL--> delivery -> END
                     ^  ^                |                                                  |
                     |  +---- FAIL ------+   (validation errors only, max N repair cycles)  |
                     +------- a human turned Review resources into manage/reference --------+
```

1. **Infrastructure Agent** - steps: `intent_router`, `resource_explorer`, `cloud_discovery` (boto3, read-only), `graph_agent`, `classification_agent`.
   **Access**: `role_arn` + per-tenant `external_id` (`sts:AssumeRole`, trust-policy condition); the
   caller's keys are used only for AssumeRole, then `state["aws_credentials"]` becomes the role's 1-hour
   credentials, so drift / plan / cross-check read the same target account. Customer setup:
   `docs/aws/read-only-role.md` (read-only allow list + explicit denies on data reads and on anything
   else). `cloud_discovery` runs the service scans in parallel with adaptive retries; every call that
   still fails is recorded in `state["discovery"]` and makes the verdict **INCOMPLETE** (a failed call
   never looks like an empty account). A test records every boto operation and fails on anything not
   `Describe*`/`Get*`/`List*`.
   `resource_explorer` (`tools/resource_explorer.py`) queries AWS Resource Explorer for an all-region
   inventory using only `ListIndexes` + `Search` (`READ_ONLY_OPERATIONS`, enforced by tests). It never
   creates/changes indexes or views - if Resource Explorer isn't turned on it reports why and discovery
   carries on. `region="auto"` resolves to the region with the most supported resources; an explicit
   region is never overridden, only warned about.
   `classification_agent` (`tools/resource_classifier.py`) gives every resource a **decision**, rules
   first, no LLM: **manage** (resource + import block), **reference** (owned elsewhere - Terraform-tagged,
   shared marker, or an AWS default something depends on: data block), **exclude** (CloudFormation-managed,
   service-linked roles, unreferenced AWS defaults, unsupported types: not in code, in the report),
   **review** (IAM roles unless `TERRAAGENT_MANAGE_IAM=true`, orphaned in the graph, malformed data).
   Tags are untrusted text - only ever matched against fixed keys. The planner maps decisions onto the
   composer's P2 categories (manage->safe_to_import, reference->use_data_source, exclude->do_not_manage
   or unsupported, review->review_required).
   Output: the **Canonical Infra Model** (`tools/infra_model.py`, state `infra_model`, `infra_model.json`
   in the bundle) - one record per resource: type, ARN, import ID (fixed `IMPORT_ID_FIELDS` lookup, never
   an LLM), region, attributes, dependencies, stack, decision, reasons, evidence (source API, discovery
   time, rule).
2. **IaC Engineering Agent** - first visit: `adoption_planning_agent`, `terraform_composer`. The
   generator writes `imports.tf`: an `import {}` block per managed resource (root-module addresses,
   adoption-plan order), IDs from `IMPORT_ID_FIELDS` only (`tools/import_blocks.py`). A managed resource
   with no derivable import ID goes to review instead of being generated. Import blocks are reviewable
   text - they take effect only when someone applies in their own pipeline (rule #2); `plan` with them is
   read-only. Per-wave GitHub PRs filter `imports.tf` to that wave's resources.
   **Adoption code is exactly what is live** (the adoption PR must plan with zero changes): every value
   from discovery, an optional attribute that wasn't discovered is omitted (the import keeps the live
   value), a required one that wasn't makes the resource review. Exact live tags, **no provider
   `default_tags`**, and **no hardening** (encryption, IMDSv2, public access blocks...) - that's the
   Hardening proposal's job. Every discovered string reaches HCL only through
   `tools/hcl_render.py::hcl_str` (escapes quotes, newlines and `${`/`%{`) - names, tags, descriptions and
   policy documents are untrusted text. Security-group rules keep every source (IPv4/IPv6, prefix
   lists, other groups, self); routes keep every target kind or send the table to review. When the
   verifier returns FAIL: `repair_agent` step = `agents/validation_repair.py`, which fixes only blocks
   that fail `terraform validate`/`init` (mapped via `tools/validation_diagnostics.py`) with the
   value-preserving `prompts/repair_validation.txt`. **Every fix must pass
   `tools/hcl_invariants.py::check_repair_invariants`** (no resources removed, no `ignore_changes`, no
   scanner suppressions, no `external` data sources or provisioners, no literal secrets) or it is
   rejected and the block left as it was.
3. **Verification & Risk Agent** - judges, **never edits**. `validation_agent` first; if it fails the
   pass stops there (verdict FAIL). Otherwise `drift_reconciliation_agent`, `plan_equivalence_agent`,
   `config_crosscheck` (with plan equivalence, opt-in: `TerraformRunner.generate_config` runs
   `plan -generate-config-out` on just the import blocks and `tools/config_crosscheck.py` compares every
   top-level literal we generated with what Terraform generated from the live resource - report only),
   `policy_agent` (Checkov, Trivy, Conftest - tfsec dropped, its rules live in Trivy). Security findings
   are **reported, never auto-fixed** in adoption code. **Fails closed**: a crash, timeout, missing tool,
   or unparseable output (runner `tool_error` -> `security_results.scanners_failed`), a `system`
   validation check, or a plan-equivalence init failure makes the verdict INCOMPLETE, never PASS.
   Verdicts: PASS | FAIL | INCOMPLETE | NEEDS_APPROVAL, one `verification_iterations` entry per pass.
   Two **separate** scores per pass (`tools/scores.py`): **Migration Safety** (will adopting change
   anything? - from the plan's per-address `changes`, else drift; SAFE / CHANGES / DESTRUCTIVE /
   UNVERIFIED) and **Security Posture** (what's wrong today? - scanner findings only). Security findings
   never move Migration Safety and vice versa; no evidence = `score: null`, never 100. A plan `replace`
   is destructive, same as `destroy`.
4. **Delivery & Approval Agent** - risk gate first: unapproved behavior-changing/destructive findings
   (`pending_approval` minus `approved_finding_keys`) or resources still in **Review** pause the run with
   LangGraph `interrupt()`. `services/pipeline.py` then saves the thread (`services/checkpoints.py`, one
   `graph_checkpoints` row per paused job, **credential channels and credential-named keys removed** -
   rule #4) and sets `AWAITING_APPROVAL` with `approval_request` {findings, review_resources}.
   `POST /scan/{id}/approve` must decide every Review resource (manage/reference/exclude; unsupported
   types: exclude only) and resumes the same run with `Command(resume=...)`; optional re-supplied AWS
   credentials go into that run's memory only. Review decisions that add code route back to
   iac_engineering -> verification (without credentials, live checks can't be redone -> INCOMPLETE).
   `/reject` writes the audit README only. Then it packages: `hardening_agent` (the optional **Hardening
   proposal**, `tools/hardening.py`: deterministic, explained fixes only where discovery shows the setting
   missing - S3 public access block, S3 default encryption, EC2 IMDSv2 - plus manual recommendations;
   must pass `check_repair_invariants` and `validate` or no files ship; `hardening/` in the bundle, never
   merged into `terraform/`), `cost_agent` (Infracost **only** for the hardening delta - the adoption has
   no cost delta), `documentation_agent`. Approval never runs apply or import.
   **Delivery is PR-first** (`services/github_client.py`, `POST /scan/{id}/pull-request`, token used once,
   never stored): the **Adoption PR** (whole job or one wave) carries both scores, a per-resource table
   (address, import ID, plan action), the classification decisions and the reported findings; the
   **Hardening PR** (`kind: "hardening"`) is stacked on the adoption PR's branch so its diff is only the
   fixes, and needs the adoption PR first. Code lives under `terraform/` so Atlantis / HCP Terraform /
   Spacelift run it as-is - TerraAgent never applies. The encrypted ZIP stays as a secondary download.

The old security-driven `agents/repair_agent.py::repair_agent_node` is no longer in the graph (its
deterministic S3 fix now lives in `tools/hardening.py`). Routing: `route_after_verification` (FAIL and
`repair_attempts < max_repair_iterations` -> iac_engineering; everything else -> delivery;
env `TERRAAGENT_MAX_REPAIR_ITERATIONS`, default 2) and `route_after_delivery` (`regenerate_requested`
-> iac_engineering, else END).

Progress fields for the UI: `current_stage`, `completed_stages`, `stage_summaries`,
`verification_iterations`, `verification_verdict`, `repair_history`, `max_repair_iterations`,
`migration_safety`, `security_posture` (exposed by `GET /scan/{id}/status`; `/results` adds
`approval_request`, `hardening`, `cost_results`). Step-level `current_agent`/`completed_agents`/`agent_timings` are kept for
metrics.

Live logs: `redis_service.publish_log` stores a per-job, sequence-numbered history
(`job:{id}:loghist`) alongside pub/sub; `GET /scan/{id}/logs` replays it on connect. Slow steps emit a
heartbeat line every `TERRAAGENT_HEARTBEAT_SECONDS` (default 15). Every terraform/tofu command has a
`TERRAAGENT_TF_COMMAND_TIMEOUT` (default 600s) and every scanner a `TERRAAGENT_SCANNER_TIMEOUT`
(default 300s).

---

## Directory Structure
```
terraagent/
├── frontend/               # Next.js 14 App Router + Tailwind CSS + D3.js
│   ├── app/                # Next.js pages (/scan, /results/[id], /api)
│   ├── components/         # CredentialForm, ScanProgress, DependencyGraph, ValidationReport, ZipDownload
│   ├── lib/api.ts          # API Client with SSE streaming handler
│   └── Dockerfile          # Multi-stage Node.js build
├── backend/                # FastAPI application
│   ├── main.py             # FastAPI entrypoint, CORS, lifespan & healthcheck
│   ├── routers/            # scan.py, jobs.py, download.py
│   ├── agents/             # 4 agents (graph.py) + the step modules they run
│   ├── tools/              # AWS scanner, runners (terraform, tfsec, checkov, trivy, conftest, zip)
│   ├── models/             # Pydantic models (ScanRequest with SecretStr) & SQLAlchemy DB models
│   ├── services/           # Celery app, Redis async client, Ollama client
│   ├── prompts/            # HCL generation, repair, and documentation prompt templates
│   ├── requirements.txt    # Python dependencies
│   └── Dockerfile          # Python 3.11-slim + Terraform CLI + tfsec + non-root user
├── docker/                 # Orchestration & reverse proxy
│   ├── docker-compose.yml  # Multi-service composition (API, Frontend, Redis, Celery, Ollama, Nginx)
│   ├── docker-compose.dev.yml
│   └── nginx/nginx.conf    # Upstream routing (/api -> FastAPI:8000, / -> Next.js:3000)
├── security/               # Security policies & tool configurations
│   ├── .tfsec/config.yml
│   ├── checkov/.checkov.yaml
│   └── policies/           # Rego policies (no_public_s3.rego, no_open_security_group.rego)
├── scripts/                # Utility scripts (init_dev.sh, run_scan.sh, test_pipeline.sh)
└── CLAUDE.md
```

---

---

## Phase 2 (P2) — Production Terraform / OpenTofu Engine Rules

### 1. IaC Engine Abstraction
- Supports both **Terraform** (`terraform`) and **OpenTofu** (`tofu`) via `IaCEngine` interface (`TerraformEngine` and `OpenTofuEngine`).
- Binary path execution is controlled strictly on the backend (`get_iac_engine(engine_name)`), never permitting user-supplied binary paths.
- The same HCL is generated identically for both engines unless a dialect difference requires specialization.

### 2. Adoption Plan Contract
- The Terraform Composer **must consume** the P1 Adoption Plan:
  - `safe_to_import`: Synthesize managed resource block.
  - `use_data_source`: Synthesize data source block (`data "..." "..."`).
  - `do_not_manage`: Omit resource block entirely.
  - `review_required`: Synthesize resource block but record unresolved attributes / warnings and mark for review.

### 3. Elimination of Fake Defaults
- **Zero fake/placeholder infrastructure attributes** (no default CIDRs, fake AMIs, dummy instance classes).
- If a required attribute cannot be discovered or deterministically derived:
  - The attribute is flagged as unresolved.
  - The resource status is set to `review_required`.
  - Documented in `reports/generation_manifest.json`.

### 4. Deterministic & Dependency-Aware Generation
- Resource names and file ordering are completely deterministic (sorted by resource address and type).
- Explicit and implicit cross-resource relationships are resolved into real Terraform references (e.g. `aws_vpc.vpc_xxx.id`, `aws_subnet.subnet_xxx.id`, `aws_security_group.sg_xxx.id`) using topological mapping, avoiding hardcoded duplicate literal strings.

### 5. Centralized Runner Safety
- All execution of `terraform` or `tofu` CLI commands MUST go through `IaCEngine.run_command()` / `TerraformRunner.run_command()`.
- Allowed subcommands: `version`, `fmt`, `init`, `validate`, `plan`, `show`, `providers` (`check_argv` allowlist).
- Blocked anywhere in argv: `apply`, `destroy`, `import` (also as flags). Direct execution without runner validation is strictly forbidden.
- `init` takes an exclusive file lock on `TF_PLUGIN_CACHE_DIR` (shared by all worker processes; Terraform's cache isn't safe for concurrent inits).

### 6. Validation Flow & Status Granularity
- Validation executes in temporary isolated sandboxes:
  1. `fmt -check`
  2. `init -backend=false`
  3. `validate`
- Validation results clearly distinguish `PASS`, `FAIL`, and `PARTIAL`.

### 7. Generation Manifest & Artifact Structure
- Every generation run produces a machine-readable `generation_manifest.json` under `reports/`.
- Manifest includes: job_id, engine, engine_version, discovered count, generated count, skipped count, review count, unsupported count, failed count, adoption outcomes, unresolved attributes, warnings, and file list.
- Artifact ZIP contains clean modular project files (`terraform/`, `inventory.json`, `inventory.csv`, `dependency_graph.json`, `reports/generation_manifest.json`, `reports/validation_report.json`, `migration/import_plan.md`, `migration/migration_checklist.md`, `README.md`).
- Zero credentials or state secrets in output artifacts.

---

## Phase 3 (P3) — Drift Reconciliation + Plan Equivalence + Approval Safety

### 1. Distinct Verification Responsibilities
- **Plan Equivalence**: Proves what Terraform/OpenTofu *intends to create/change* using the generated HCL (`create`, `update`, `replace`, `destroy`, `no_op`).
- **Drift Reconciliation**: Proves whether existing adopted AWS resources *match reality* without changing their behavior.
- Only Drift Reconciliation produces `behavior_changing` or `destructive_equivalent` findings for existing adopted resources.

### 2. Risk Levels & Severity Mapping
- `informational`: Cosmetic tag-only or metadata differences; non-blocking.
- `behavior_changing`: Real configuration mismatches on non-ForceNew attributes or security group rule differences; mapped to `behavior_changing` tier in `pending_approval`.
- `destructive_equivalent`: Mismatches on ForceNew attributes (e.g. VPC CIDR, subnet CIDR, AMI) or resources missing/deleted in live AWS; mapped to `destructive` tier in `pending_approval`.

### 3. Human Approval Gate
- If blocking findings (`destructive` or `behavior_changing`) are detected in Drift Reconciliation or Plan Equivalence, or resources are in Review:
  - The Delivery & Approval Agent pauses the run with LangGraph `interrupt()`; the checkpoint is stored without credentials.
  - State is set to `status: "AWAITING_APPROVAL"`, `current_agent: "awaiting_approval"`, with `approval_request`.
  - Approval (`POST /scan/{job_id}/approve`, deciding every Review resource) resumes the same run; findings a human approved are never asked about again.
  - Approval **NEVER** runs `terraform apply` or `terraform import`.

### 4. Runner Safety & Sandbox Isolation
- Plan execution runs in isolated temporary sandboxes via `TerraformRunner.plan_json` with scoped credentials passed in an isolated `env` dict.
- `TF_LOG` debug logging is explicitly disabled.
- Standard disallowed argv checks strictly block `apply`, `destroy`, `import`.

### 5. Evidence & Reporting
- Every drift finding captures: `resource_id`, `resource_type`, `terraform_address`, `attribute`, `live_value`, `generated_value`, `impact`, `tier`, `reason`, `evidence`.
- Generates `reports/drift_results.json` and `drift_report.md` included in the artifact bundle.

---

## Coding Conventions
- **Python**: Strict type hints everywhere (`typing.List`, `typing.Dict`, `Optional`, `TypedDict`). Asynchronous endpoints in FastAPI. Use Pydantic `SecretStr` for sensitive keys. Redact credentials in all logging pipelines.
- **TypeScript**: Strict mode enabled (`strict: true`). Avoid `any` types; define comprehensive TypeScript interfaces for all API payloads and graph models.
- **Docker**: Healthchecks enabled for services, non-root users, explicit resource boundaries, environment-driven configurations.


