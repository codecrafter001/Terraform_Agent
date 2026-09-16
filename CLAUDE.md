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
`backend/tools/terraform_runner.py::TerraformRunner.run_command`'s disallowed-argv check — it
substring-matches every argv token against `apply`/`destroy`/`import` and raises before the
subprocess ever starts. **This is a chokepoint only for callers that actually go through it.**
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
- **Agent Orchestration**: LangGraph (StateGraph), LangChain
- **Asynchronous Task Queue**: Celery + Redis 7 (broker & result backend) + Redis Pub/Sub for SSE live logs
- **Local LLM**: Ollama (`codellama`, `llama3`) running locally / containerized
- **Security & Validation Pipeline**: Terraform CLI (`fmt`, `init`, `validate`), `tfsec`, `Checkov`, `Trivy`, `Conftest` (OPA policies)
- **Containerization & Ingress**: Docker, Docker Compose, Nginx Reverse Proxy

---

## 8-Agent LangGraph Pipeline Architecture
1. **Intent Router**: Classifies natural language requests and sets operation modes (`generate`, `scan`, `explain`, `validate`).
2. **Cloud Discovery Agent**: Discovers live AWS resources with boto3 using read-only credentials, pagination, and exponential backoff retry.
3. **Graph Agent**: Maps cross-resource dependencies into a DAG (JSON adjacency list, Graphviz DOT, D3-compatible visualization data).
4. **Terraform Composer Agent**: Prompts local LLM (Ollama) to synthesize clean HCL (`resources.tf`, `variables.tf`, `outputs.tf`, `providers.tf`).
5. **Validation Agent**: Executes `terraform fmt -check`, `terraform init -backend=false`, and `terraform validate` in isolated temporary sandboxes.
6. **Policy & Security Agent**: Multi-layer security scans (`tfsec`, `Checkov`, `Trivy`, `Conftest` OPA) scoring findings by severity.
7. **Repair Agent**: Evaluates syntax or policy errors, applies targeted prompt-based repairs, and retries up to 2 cycles.
8. **Documentation Agent**: Generates `README.md`, `assumptions.md`, `migration_checklist.md`, import scripts, and packages the verified ZIP bundle.

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
│   ├── agents/             # 8 LangGraph agent nodes & StateGraph
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
- Allowed commands in this phase: `version`, `fmt`, `init`, `validate`.
- Blocked commands: `apply`, `destroy`, `import`. Direct execution without runner validation is strictly forbidden.

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
- If blocking findings (`destructive` or `behavior_changing`) are detected in Drift Reconciliation or Plan Equivalence:
  - Execution halts before downstream actions.
  - State is set to `status: "AWAITING_APPROVAL"`, `current_agent: "awaiting_approval"`.
  - Approval (`POST /scan/{job_id}/approve`) resumes the remaining pipeline tail (documentation, ZIP bundle packaging).
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


