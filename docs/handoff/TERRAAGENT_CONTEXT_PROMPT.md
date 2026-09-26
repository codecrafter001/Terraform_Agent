# TerraAgent: session handoff / universal context prompt

Paste this whole file as the first message in a new Claude Code session opened in
`C:\Users\USER\Desktop\AIKART\Terraform Agent`. It summarizes everything decided and built so far.
Read `CLAUDE.md` (repo root) and `frontend/AGENTS.md` too: they are authoritative and kept up to date.

---

## 1. What TerraAgent is

A LangGraph multi-agent system (FastAPI + Celery/Redis + Postgres + Ollama + Next.js 16 frontend,
all in Docker Compose behind nginx) that converts existing, click-ops AWS infrastructure into
validated Terraform/OpenTofu **without ever modifying AWS**. The hard safety rules in `CLAUDE.md`
are non-negotiable, above all: never run `terraform apply/destroy/import`, read-only AWS APIs only,
never log, store or send credentials to an LLM, and every terraform/tofu invocation must go through
`TerraformRunner.run_command` (its argv guard blocks apply/destroy/import).

## 2. Where things stand

- **Branch:** `feat/four-agent-restructure` (off `main`). Nothing is pushed. Commits, oldest first:
  - `80ad140` Checkpoint: UI redesign, 4-stage graph, log replay, Resource Explorer (also includes a parallel session's modular HCL generator and migration confidence scorer)
  - `5871455` Week 2: restructure into the plan's four agents, move repair, fail closed
  - `9e4f174` Week 3: Canonical Infra Model and Manage / Reference / Exclude / Review
  - `c1be045` Week 4: import blocks and the `plan -generate-config-out` cross-check
- **Deployed:** the Docker stack runs `c1be045`. UI at http://localhost (nginx :80), API at
  http://localhost/api.
- **Tests:** 259 backend tests pass on the host. In the worker container, the LocalStack end-to-end
  test (`tests/test_langgraph_pipeline.py`) passes with real terraform. Frontend `tsc` and `eslint`
  are clean.

## 3. The plan being followed (8 weeks; the user asked to follow it)

**Principle:** AWS facts → infra model → deterministic generation → LLM only where required.
Something is an **agent** only if an LLM or reasoning step decides something there; everything else
is a **step or tool**.

**Four agents** (implemented in `backend/agents/graph.py`):

```
infrastructure → iac_engineering → verification ──PASS/INCOMPLETE/NEEDS_APPROVAL──> delivery → END
                       ^                │
                       └──── FAIL ──────┘  (validation errors only, max N repair cycles)
```

1. **Infrastructure Agent**
   - Steps: `intent_router`, `resource_explorer` (AWS Resource Explorer, `ListIndexes` + `Search` only), `cloud_discovery` (boto3), `graph_agent`, `classification_agent`.
   - Output: the **Canonical Infra Model** (`tools/infra_model.py`, state `infra_model`, `infra_model.json` in the bundle).
2. **IaC Engineering Agent**
   - Steps: `adoption_planning_agent`, `terraform_composer` (writes `imports.tf`).
   - On a FAIL verdict it runs **repair** (`agents/validation_repair.py`): it fixes only blocks that fail `validate`/`init`, and every fix must pass `tools/hcl_invariants.py` or it is rejected.
3. **Verification & Risk Agent** (judges, never edits)
   - Runs `validation_agent` first and stops there if validation fails.
   - Then `drift_reconciliation_agent`, `plan_equivalence_agent`, `config_crosscheck`, and `policy_agent` (Checkov, Trivy, Conftest; tfsec dropped).
   - **Fails closed:** a crash, timeout, missing tool or unparseable output makes the verdict INCOMPLETE.
   - Verdicts: PASS / FAIL / INCOMPLETE / NEEDS_APPROVAL.
4. **Delivery & Approval Agent**
   - A risk gate first (a `pending_approval` pauses the job at `AWAITING_APPROVAL`; `POST /scan/{id}/approve` re-runs this agent), then `cost_agent` and `documentation_agent`.

**Classification decisions** (Week 3, rules first, no LLM):

| Decision | Output | Rules |
|---|---|---|
| **manage** | `resource` + import block | discovered, supported, not owned elsewhere |
| **reference** | `data` block | Terraform-managed elsewhere, shared marker, or an AWS default something depends on |
| **exclude** | not in code, listed in the report | CloudFormation-managed, service-linked roles, unreferenced AWS defaults, unsupported types |
| **review** | waits for a human | IAM roles unless `TERRAAGENT_MANAGE_IAM=true`, orphaned in the graph, malformed data |

Tags are untrusted text and are only matched against fixed keys.

**Timeline:**

| Week | Content | Status |
|---|---|---|
| 1 | Sandbox AWS account + nightly no-op-rate run; SSE live-log fix | SSE **done**; sandbox **needs the user** |
| 2 | 4 agents, repair moved, invariants, fail-closed | **done** |
| 3 | Canonical Infra Model + classification | **done** |
| 4 | Import blocks, ID lookup table, `generate-config-out` cross-check | **done** |
| 5 | Split scores (Migration Safety vs Security Posture); adoption vs hardening output; approval via LangGraph `interrupt` (also pause when resources are in Review) | **NEXT** |
| 6 | GitHub PR delivery as the primary output; UI polish for the loop | todo |
| 7 | Hardening, performance (provider cache, parallel scans), re-run the test bench | todo |
| 8 | CEO demo with real numbers | todo |

**Success metrics:**
- no-op rate above 90% of managed resources
- zero destroy/replace in any adoption PR
- classification accuracy against a hand-labelled answer key
- human-review rate
- end-to-end scan time

**Integrations are phased:**
- **MVP:** GitHub, AWS Config / Resource Explorer, and making the output repo work as-is with Atlantis, HCP Terraform or Spacelift.
- **Later:** CloudTrail, Security Hub, Slack, and so on.

## 4. Key files

- **Backend graph and state:** `backend/agents/graph.py`
  - `build_initial_state` is shared by the Celery task and the inline runner.
  - `_run_steps` / `_timed` publish progress, and `_with_heartbeat` logs "Still working…" every 15s.
- **Classification:** `backend/tools/resource_classifier.py` and `models/adoption.py` (`decision` and `evidence` fields). `tools/adoption_planner.py` maps decisions onto the composer's P2 categories.
- **Generation:**
  - `tools/hcl_generator.py` (root stack files plus `imports.tf`)
  - `tools/import_blocks.py`
  - `tools/infra_model.py` (`IMPORT_ID_FIELDS`, `SOURCE_API`)
- **Repair:** `agents/validation_repair.py`, `tools/validation_diagnostics.py`, `tools/hcl_invariants.py`, `prompts/repair_validation.txt` (value-preserving). The old security-driven `agents/repair_agent.py` is kept for the future hardening PR and is not wired into the graph.
- **Runners:**
  - `tools/terraform_runner.py`: `run_command` has a 600s timeout (`TERRAAGENT_TF_COMMAND_TIMEOUT`); `plan_json` counts `imported`; `generate_config`; `_scoped_aws_env`.
  - Scanners `tools/{checkov,trivy,conftest}_runner.py` report `tool_error` and have a 300s timeout (`TERRAAGENT_SCANNER_TIMEOUT`).
- **Cross-check:** `agents/config_crosscheck_agent.py` and `tools/config_crosscheck.py`.
- **Resource Explorer:** `tools/resource_explorer.py` and `agents/resource_explorer_step.py`. `region="auto"` picks the region with the most supported resources.
- **Live logs:** `services/redis_client.py`. Per-job sequence-numbered history (`job:{id}:loghist`) is replayed on SSE connect; the frontend de-duplicates by `seq`.
- **API:** `routers/scan.py`. `/status` exposes `current_stage`, `completed_stages`, `stage_summaries`, `verification_iterations`, `verification_verdict`, `repair_history`, `max_repair_iterations`. `/results` adds `infra_model`, `config_crosscheck` and `resource_inventory`.
- **Frontend:**
  - `components/Sidebar.tsx`, `ui.tsx` (PageHeader, StatusBadge, StatCard), `app/globals.css` (`.card`, `.btn-*`, `.field-input`)
  - `ScanProgress.tsx` (4 agent cards, loop, IterationTimeline, live log)
  - `ResultsTabs.tsx` (Overview / Inventory / Topology / Verification / Deliverables, hash-linked)
  - `DecisionsPanel.tsx`, `InventoryPanel.tsx`, `CredentialForm.tsx`
  - `lib/types.ts` holds all API types.

## 5. How to work in this repo (learned the hard way)

- **Tests (host):** `cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py`. The host has no terraform binary, so the LocalStack test only passes in the container.
- **Tests (container, real terraform):**
  `docker exec -e LOCALSTACK_URL=http://terraagent-localstack:4566 terraagent-celery sh -c 'cd "$(python -c "import tools,os;print(os.path.dirname(os.path.dirname(tools.__file__)))")" && python -m pytest -q -p no:cacheprovider tests/test_langgraph_pipeline.py'`
- **Frontend checks:** `cd frontend && npx tsc --noEmit && npx eslint .`. Next.js here is v16, which differs from older versions; read `node_modules/next/dist/docs/` before using unfamiliar APIs.
- **Deploy:** `docker compose build terraagent-api terraagent-celery terraagent-frontend && docker compose up -d --no-deps terraagent-api terraagent-celery terraagent-frontend`.
  - **Then always run `docker restart terraagent-nginx`.** nginx caches the old container IPs and returns 502 otherwise.
- **Docker Desktop** may be stopped. Start it with `C:\Users\USER\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`.
- **Other services on this machine:**
  - port 3000 is an unrelated aiKart trading app
  - port 8000 is `finsightai`
  - the frontend container is published on 3002
  - Redis needs auth, so use the API container's `redis_service` rather than `redis-cli`
- **Line endings:** backend and frontend files are LF. Python `open(..., 'w')` on Windows writes CRLF, so write with `newline='\n'` or in binary mode. Revert `frontend/tsconfig.tsbuildinfo` before committing (`git checkout -- frontend/tsconfig.tsbuildinfo`).
- **Shell:** Bash heredocs containing `'''` or `\\n` get mangled by the tool. Put multi-line Python in a scratchpad file and run it, or use the Edit/Write tools.
- **Dev server:** open it at `localhost`, not `127.0.0.1`. Next blocks dev resources for 127.0.0.1, so the page never hydrates.
- **Screenshots:** headless Chrome's `--screenshot` / `--virtual-time-budget` freezes pages that hold an open SSE stream. Use a real-time Chrome DevTools Protocol script instead: a Node script launches Chrome with `--remote-debugging-port`, waits about 7s, then calls `Page.captureScreenshot`. For visual checks, seed a temporary job state in Redis via `redis_service.set_job_state` and `publish_log` inside `terraagent-api`, then delete the keys afterwards.
- **Commit style:** commit on the feature branch with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push without asking.
- **Parallel sessions:** another Claude session edited this repo in parallel before. Before large edits, check `git status` for changes you didn't make, and ask.

## 6. Open issues and recommendations

1. **Duplicate modules.** `tools/hcl_generator.py` writes every resource twice: in the root stack files and in `modules/*/main.tf`. The root never calls those modules, so Terraform ignores them, but the scanners scan them, so findings are doubled. Either wire the root to call the modules or stop emitting the copies. **Ask the user which.**
2. **Shared provider cache.** Concurrent scans can stall on the shared `TF_PLUGIN_CACHE_DIR`, which Terraform documents as not concurrency-safe. Give each sandbox its own cache copy or serialize `init` (Week 7).
3. **Dashboard polish** (user-approved ideas, not done yet):
   - a "Nothing found" badge for completed scans with 0 resources
   - a latest-scan summary card on top
   - archive/delete of old test jobs
4. **Plan equivalence and the cross-check** only run when `run_plan_equivalence` is true and real credentials are provided.
5. **Week 1 needs the user:** a sandbox AWS account with realistic resources, and turning on Resource Explorer (aggregator index + default view) with `resource-explorer-2:ListIndexes` and `Search` permissions for the scan role.

## 7. Next task: Week 5

1. **Split the score into two:**
   - **Migration Safety:** will anything change? Built from plan no-op/update/replace counts, drift, cross-check mismatches and the verdict.
   - **Security Posture:** what's wrong with the current setup? Built from Checkov/Trivy/OPA findings. Keep them separate.
   - `tools/confidence_scorer.py` (from the parallel session) may be reusable. Read it first.
2. **Separate the outputs:**
   - **Adoption:** zero-change code plus import blocks, and the findings reported in its description.
   - **Hardening:** optional security fixes, each explained, with a cost delta. The old `agents/repair_agent.py` security fixes and Infracost belong here.
3. **Approval via LangGraph `interrupt`:**
   - Replace the halt-to-END plus manual resume in `routers/scan.py::_resume_after_decision` with `interrupt()` and a checkpointer.
   - Also pause when resources are in **Review**, so a human can decide each one.
   - Approval must never run apply or import.
4. **Update everything that depends on these changes:** tests, `CLAUDE.md`, and the UI (results page score cards, review decisions in the approval panel).
