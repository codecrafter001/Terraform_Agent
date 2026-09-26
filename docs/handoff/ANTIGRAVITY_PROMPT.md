# TerraAgent: handoff prompt for a coding agent (detailed)

Paste everything below the line as the first message in a new agent session opened on
`C:\Users\USER\Desktop\AIKART\Terraform Agent`. It is self-contained.

---

# Role and ground rules for this session

You are a senior engineer continuing work on **TerraAgent**, which lives in this workspace.

- **Before editing anything:**
  1. Read `CLAUDE.md` (repo root). It is authoritative: architecture, safety rules, conventions.
  2. Read `docs/aws/read-only-role.md` and `docs/design/phase2-change-requests/README.md`.
  3. Run `git status`, `git log --oneline -8` and the host test suite (commands below).
  4. Report what you found in 5–10 lines and wait for my go-ahead.
- **Ask me first** before:
  - adding a dependency
  - a refactor that touches more than ~5 files
  - anything that touches a safety rule
  - any product decision (anything not already spelled out here)
- **Never:** push, force-push, rewrite history, delete branches, or touch `.env`. `.env` holds real secrets, so never print or commit it.
- **Every behavior change** gets a test. Safety properties get tests too.
- **When you finish a task:** run the relevant tests, commit on the current branch with a message in the existing "Week N: ..." style, and tell me exactly what was verified and what wasn't.

---

# 1. What TerraAgent is

TerraAgent converts existing, hand-built ("ClickOps") AWS infrastructure into validated Terraform/OpenTofu, delivered as a GitHub pull request. **It never modifies AWS.** The customer's own pipeline (Atlantis, HCP Terraform or Spacelift) applies the PR after human review.

**Core principle:** AWS facts → Canonical Infra Model → deterministic generation → LLM only where required.

**Success metrics** (measured with `scripts/bench_adoption.py` against a sandbox account):
- **No-op rate:** above 90% of managed resources.
- **Destroy/replace:** zero in any adoption PR.
- **Classification accuracy:** measured against a hand-labeled answer key.
- **Human-review rate:** as low as possible while the no-op rate holds.
- **Scan time:** end to end, recorded each run.

---

# 2. Hard safety rules (non-negotiable)

1. **Never run `terraform apply`, `destroy` or `import`**, or run imports automatically.
   - Every terraform/tofu execution goes through `backend/tools/terraform_runner.py::TerraformRunner.run_command`.
   - Its `check_argv` requires the binary to be `terraform`/`tofu`.
   - It allows only these subcommands: `version fmt init validate plan show providers`.
   - It rejects `apply`/`destroy`/`import` anywhere in argv, including as flags (`plan -destroy`).
   - Never call `asyncio.create_subprocess_exec` on terraform/tofu yourself.
   - Import blocks (`imports.tf`) are reviewable text only.
2. **Read-only AWS:** only `Describe*`/`Get*`/`List*` (plus `sts:AssumeRole`).
   - `backend/tests/test_cloud_discovery.py::test_scanner_only_ever_calls_read_only_apis` records every boto operation and fails on anything else.
   - Any new AWS call needs three things: it's covered by that test, it's added to `docs/aws/read-only-policy.json` (both the Allow list and the `NotAction` list of `NeverChangeAnything`), and it's listed in the table in `docs/aws/read-only-role.md`.
3. **Credentials:**
   - Never store, log or print them, and never send them to an LLM.
   - Models use `SecretStr`.
   - Error text goes through `tools/credential_scrubber.py::CredentialScrubber`.
   - The Redis state is scrubbed.
   - Paused-run checkpoints drop credential channels and keys (`services/checkpoints.py`). A test proves no secret byte is stored.
   - The GitHub token is used once per request and never persisted.
4. **Discovered text is untrusted** (tag values are whatever someone typed in the console):
   - It reaches HCL only through `tools/hcl_render.py::hcl_str`, which escapes quotes, backslashes, newlines and `${`/`%{`.
   - It reaches PR markdown only through `services/github_client.py::_md`.
   - Tags are only ever *matched* against fixed keys, never interpreted.
5. **Adoption code must plan with zero changes** (`tools/hcl_generator.py::_compose_managed_resource`):
   - every value comes from discovery
   - a missing optional attribute is omitted (the import keeps the live value)
   - a missing required attribute makes the resource unresolved, which sends it to review
   - exact live tags; no provider `default_tags`; no security hardening
   - security fixes go only in the Hardening proposal (`tools/hardening.py`)
6. **Fail closed:**
   - A crash, timeout, missing tool, unparseable output, incomplete discovery, or re-verification without credentials gives verdict **INCOMPLETE**, never PASS.
   - Scores are `null` without evidence, never 100.
   - Missing Infracost is "not estimated", never $0.
7. **Containers and shell:** containers run as the non-root `agent` user. Never build shell commands from user input.

---

# 3. Architecture (`backend/agents/graph.py`)

```
infrastructure -> iac_engineering -> verification --PASS/INCOMPLETE/NEEDS_APPROVAL--> delivery -> END
                     ^  ^                |                                               |
                     |  +---- FAIL ------+  (validation errors only, max 2 repairs)      |
                     +------- human turned Review resources into manage/reference -------+
```

Agents are the 4 graph nodes. Each runs *steps*, which are individually unit-tested functions.

| Agent | Steps / modules | Output |
|---|---|---|
| **1. Infrastructure** | `intent_router` (+ `tools/intent_analyzer.py`); `resource_explorer_step` (Resource Explorer: `ListIndexes`+`Search` only); `cloud_discovery` (`tools/aws_scanner.py`: parallel, adaptive retries, failures in `state["discovery"]`); `graph_agent`; `classification_agent` (`tools/resource_classifier.py`: rules first) | `resources`, `dependency_graph`, `classification_results`, **Canonical Infra Model** `infra_model` (`tools/infra_model.py`), `discovery` |
| **2. IaC Engineering** | `adoption_planning_agent` (`tools/adoption_planner.py`); `terraform_composer` (`tools/hcl_generator.py`, `tools/import_blocks.py`, `terraform fmt`); on FAIL: `agents/validation_repair.py` (every fix must pass `tools/hcl_invariants.py::check_repair_invariants`) | `terraform_files` (root stack files `foundation/security/data/application.tf` + `imports.tf`), `generation_manifest` |
| **3. Verification & Risk** (judges, never edits) | `validation_agent` (fmt -check / init / validate; stops the pass if it fails); `drift_reconciliation_agent`; `plan_equivalence_agent` (opt-in `run_plan_equivalence`, real `plan` with import blocks); `config_crosscheck` (`plan -generate-config-out`); `policy_agent` (Checkov, Trivy, Conftest in parallel; report only) | `verification_iterations`, `verification_verdict`, **`migration_safety`** and **`security_posture`** (`tools/scores.py`, separate on purpose) |
| **4. Delivery & Approval** | risk gate → `interrupt()` (see below); then `hardening_agent` (`tools/hardening.py`), `cost_agent` (Infracost, hardening delta only), `documentation_agent` + `tools/zip_builder.py` | `hardening`, `cost_results`, docs, ZIP; PRs via `services/github_client.py` |

**Classification decisions:**

| Decision | Output |
|---|---|
| **manage** | `resource` + `import` block |
| **reference** | `data` block |
| **exclude** | not in code, listed in the report |
| **review** | waits for a human |

IAM roles go to review unless `TERRAAGENT_MANAGE_IAM=true`.

**Approval flow:**
1. The gate pauses on unapproved behavior-changing/destructive findings, or on any resource in Review.
2. `services/pipeline.py` saves the thread (`graph_checkpoints` table, secrets removed) and sets `status=AWAITING_APPROVAL` + `approval_request`.
3. Approve with `POST /api/scan/{id}/approve` and a body of `{reason?, resource_decisions: {id: manage|reference|exclude}, aws_access_key?, aws_secret_key?, aws_session_token?}`.
   - Every Review resource must be decided. Unsupported types can only be excluded.
   - The approval resumes the same run with `Command(resume=...)`.
   - Findings a human already approved are never asked about again.
4. `POST /api/scan/{id}/reject` writes the audit README only.

**Access:** `role_arn` + `external_id` go on the scan request. After AssumeRole, `state["aws_credentials"]` holds the role's 1-hour credentials, so drift, plan and the cross-check read the same target account. On resume, re-supplied keys are exchanged for the role again.

**Delivery:**
- **Adoption PR:** both scores, a per-resource table (address | import ID | plan action), classification decisions, reported findings, and pipeline notes.
- **Hardening PR** (`kind: "hardening"`): branched from and targeting the adoption PR's branch. It needs the adoption PR to exist first.
- All code goes under `terraform/`.

**API endpoints** (prefix `/api`; header `X-API-Key` when `TERRAAGENT_API_KEY` is set):
- `POST /scan`: starts a job. Body fields:
  - credentials: `aws_access_key`, `aws_secret_key`, `aws_session_token?`
  - account and region: `role_arn?`, `external_id?`, `region` (or `"auto"`)
  - scope: `operation`, `resource_filters`
  - options: `run_plan_equivalence`, `use_resource_explorer`, `terraform_binary` (`terraform`|`tofu`), `zip_password?`, `webhook_url?`
  - request intent: `environment?`, `user_request?`, `analyzed_intent?`
- `POST /scan/analyze-intent`
- `GET /scan/{id}/status`
- `GET /scan/{id}/results`
- `GET /scan/{id}/logs` (SSE, with replay)
- `POST /scan/{id}/approve`
- `POST /scan/{id}/reject`
- `POST /scan/{id}/pull-request` with `{github_token, repo, base_branch?, wave?, kind?}`
- `GET /download/{id}`

**Frontend** (`frontend/`, Next.js 16, TS strict, Tailwind, lucide):

| File | What it is |
|---|---|
| `app/scan/*` + `components/CredentialForm.tsx` | New request: credentials, role ARN/ExternalId, request box, intent modal |
| `components/ScanProgress.tsx` | 4 agent cards, loop, live log, approval panel |
| `app/results/[id]/page.tsx` | Results: KPI row with the two scores; tabs Overview / Inventory / Topology / Verification / "Pull request & bundle" |
| `PendingApprovalPanel.tsx`, `ScoresPanel.tsx`, `HardeningPanel.tsx`, `CreatePullRequestAction.tsx` | Main result components |
| `lib/types.ts` | All API types; keep in sync with backend models |
| `lib/api.ts` | API client |

---

# 4. Current state

- **Branch:** `feat/four-agent-restructure` (off `main`). Nothing is pushed.
- **Commits:**

| Commit | Content |
|---|---|
| `c1be045` | Week 4: import blocks, `generate-config-out` cross-check |
| `98db167` | NL request box + intent analysis; Change-Request PRs parked as Phase 2 |
| `01b8898` | Week 5: split scores, adoption vs hardening, `interrupt()` approval, adoption fidelity, HCL escaping |
| `103cdc7` | Week 6: PR-first delivery, richer adoption PR, stacked Hardening PR |

- **Week 7 is implemented but UNCOMMITTED.** 314 host tests pass; `tsc` and `eslint` pass. It contains:
  - ExternalId and role-credential propagation
  - the incomplete-discovery status
  - parallel discovery with adaptive retries
  - the read-only boto test
  - the runner subcommand allowlist and the plugin-cache `init` file lock
  - tfsec removed from `backend/Dockerfile`, and the cache dir created early
  - `docs/aws/` (guide, trust policy, read-only policy)
  - Role ARN/ExternalId fields in the form
  - `scripts/bench_adoption.py`
  - `backend/tests/test_access_and_discovery.py`
- **Containers** run the Week 5 code. **Week 7 has not been run inside the containers against real Terraform yet.**
- **Needs the user:** a sandbox AWS account with realistic resources, a read-only role per `docs/aws/read-only-role.md`, optionally Resource Explorer enabled, and a GitHub repo + token for PR tests.

---

# 5. Configuration

## 5.1 `.env` (repo root, read by `docker-compose.yml`)

Never print or commit it. `.env.example` documents every key.

| Variable | Required | Purpose |
|---|---|---|
| `REDIS_PASSWORD` | **yes** | Redis `--requirepass`. Compose builds `REDIS_URL`/`CELERY_*` from it |
| `POSTGRES_PASSWORD` | **yes** | Postgres. Compose builds `DATABASE_URL` from it |
| `GRAFANA_ADMIN_PASSWORD` | **yes** | Grafana admin |
| `TERRAAGENT_API_KEY` | recommended | Shared API key; must be identical for the backend and frontend services. Unset means no API auth |
| `INFRACOST_API_KEY` | optional | Hardening cost delta; without it, "not estimated" |
| `OLLAMA_MODEL` | optional | Default `codellama` |
| `PGADMIN_EMAIL` / `PGADMIN_PASSWORD` | optional | pgAdmin |

## 5.2 Backend tuning (environment variables, defaults in code)

| Variable | Default | Effect |
|---|---|---|
| `TERRAAGENT_MAX_REPAIR_ITERATIONS` | 2 | Repair cycles before delivering as FAIL |
| `TERRAAGENT_MANAGE_IAM` | false | `true` means IAM roles are managed instead of going to review |
| `TERRAAGENT_TF_COMMAND_TIMEOUT` | 600 | Seconds per terraform/tofu command |
| `TERRAAGENT_SCANNER_TIMEOUT` | 300 | Seconds per Checkov/Trivy/Conftest |
| `TERRAAGENT_HEARTBEAT_SECONDS` | 15 | "Still working" log interval |
| `MAX_JOB_RUNTIME_SECONDS` | 1800 | Stale RUNNING jobs are marked FAILED by the sweep |
| `MAX_LLM_REPAIRS_PER_CYCLE` | 2–3 | LLM repair calls per cycle |
| `MAX_ADOPTION_WAVE_SIZE` | 25 | Resources per migration wave |
| `DEPENDENCY_CONFIDENCE_THRESHOLD` | 0.80 | Trusted graph edges |
| `OLLAMA_HOST` / `OLLAMA_TIMEOUT_SECONDS` | compose / 300 | LLM |
| `OUTPUT_DIR` / `ZIP_EXPIRY_HOURS` | `/tmp/terraagent` / 24 | Bundles |
| `TF_PLUGIN_CACHE_DIR` | `/home/agent/.terraform.d/plugin-cache` | Shared provider cache (`init` is file-locked) |
| `CHECKOV_CONFIG_PATH`, `CONFTEST_POLICY_DIR` | `/app/security/...` | Scanner config |
| `AWS_ENDPOINT_URL` / `LOCALSTACK_URL` | unset / `http://localhost:4566` | **Tests only**: point boto3 at LocalStack |
| `DATABASE_URL` | SQLite `/tmp/terraagent/terraagent.db` | Host default; compose uses Postgres |

## 5.3 Services and ports (`docker-compose.yml`)

| Container | Access |
|---|---|
| `terraagent-nginx` | **http://localhost** (UI) and **http://localhost/api** |
| `terraagent-api` | internal :8000 |
| `terraagent-celery` | Worker; runs the graph |
| `terraagent-frontend` | :3002 → 3000 |
| `terraagent-redis` | password-protected; use `redis_service` inside the API container, not `redis-cli` |
| `terraagent-postgres` | internal |
| `terraagent-pgadmin` | :5050 |
| `terraagent-prometheus` | :9090 |
| `terraagent-grafana` | :3001 |
| `terraagent-ollama` | internal |
| `terraagent-localstack` | :4566, services `ec2,s3,iam,sts` |

Ports 3000 and 8000 on this machine belong to other apps.

## 5.4 Toolchain

- **Backend image:** Python 3.11, Terraform 1.8.5, OpenTofu 1.8.5, Trivy 0.74.0, Conftest 0.69.0, Checkov 3.3.16 (pipx), Infracost 0.10.39.
- **Key Python pins:** `langgraph==1.2.11`, `langgraph-checkpoint==4.2.0`, `langchain-core==1.6.0`, `pydantic==2.7.4`, `boto3/botocore==1.34.131`.
- **Host:** Windows 11 with Python 3.13 (host tests run there; there's no terraform binary on the host). Use Git Bash or PowerShell.

---

# 6. Commands

```bash
# Host tests (fast, no terraform binary needed)
cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py

# Frontend checks
cd frontend && npx tsc --noEmit && npx eslint .

# Build + deploy (always restart nginx afterwards, or it serves 502s)
docker compose build terraagent-api terraagent-celery terraagent-frontend
docker compose up -d --no-deps terraagent-api terraagent-celery terraagent-frontend
docker restart terraagent-nginx

# Real terraform + LocalStack, inside the worker
docker exec -e LOCALSTACK_URL=http://terraagent-localstack:4566 terraagent-celery \
  sh -c 'cd /app && python -m pytest -q -p no:cacheprovider tests/test_langgraph_pipeline.py tests/test_access_and_discovery.py tests/test_terraform_runner_plan.py'

# Test bench (sandbox account; credentials only via environment variables)
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... TERRAAGENT_ROLE_ARN=... TERRAAGENT_EXTERNAL_ID=...
python scripts/bench_adoption.py --region us-east-1 --answer-key bench/answer_key.json --out bench/results/$(date +%F).json
```

If Docker Desktop is stopped, start `C:\Users\USER\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`.

---

# 7. Repo conventions and pitfalls

- **Python:** full type hints; async FastAPI; comments explain *why*, not *what*; match surrounding style.
- **TypeScript:** strict, no `any`. Every API field lives in `lib/types.ts`, mirroring `backend/models/*.py`.
- **Line endings are LF.**
  - Python on Windows writes CRLF unless you pass `newline='\n'` (or write in binary mode).
  - `backend/Dockerfile` may differ, so check before exact-match edits.
- **Before every commit:** `git checkout -- frontend/tsconfig.tsbuildinfo` (it's a build artifact).
- **Shell heredocs** mangle `'''` and `\n` escapes. Put multi-line Python in a temp file, or use your editor's file tools.
- **Test patterns:**
  - Stub graph steps by monkeypatching `agents.graph.<step>_node` (see the `calls` fixture in `tests/test_graph_agent_loop.py`).
  - Run graphs with `InMemorySaver` and a `thread_id` (the `Run` helper in that file).
  - The GitHub client tests use a strictly ordered fake response queue (`tests/test_github_pr.py`), so a changed call sequence means updating the queue.
- **Addresses:** `f"{type}.{tools.naming.unique_clean_name(name or id, id)}"`. Names carry a hash suffix; never hardcode them in tests.
- **Dev server:** use `localhost`, not `127.0.0.1`; Next blocks dev resources for 127.0.0.1.
- **Screenshots:** headless Chrome `--screenshot` freezes pages with an open SSE stream. Use a real-time CDP script instead.

---

# 8. Tasks, in order, with steps and acceptance criteria

## Task A: Verify and commit Week 7

1. Run the host tests plus `tsc` and `eslint`; everything must pass.
2. Build and deploy the three images (section 6). Confirm the build log no longer contains `plugin cache dir ... cannot be opened`, and that tfsec isn't installed.
3. Run the container test command in section 6.
   - **Expect:** the LocalStack e2e passes.
   - **Expect:** `test_scanner_only_ever_calls_read_only_apis` passes.
   - **Expect:** the policy test is skipped (`docs/` isn't in the image).
4. **Smoke-test the UI at http://localhost:**
   - Start a scan with LocalStack-style dummy keys; the no-credentials path uses a mock inventory.
   - Check that the 4 agent cards progress.
   - If the job pauses, the approval panel lists Review resources with choices, and "Approve" stays disabled until each one is decided.
   - The results page shows the Migration Safety and Security Posture cards and the "Pull request & bundle" tab.
5. Commit: `Week 7: read-only access hardening, incomplete discovery, parallel scans, runner allowlist`.

**Done when:** host and container tests are green, the UI smoke test works, and there's one commit with no `.env` or `tsbuildinfo` changes.

## Task B: Sandbox test bench (needs the user's sandbox; ask for it)

1. With the user, create the role per `docs/aws/read-only-role.md`. Confirm `aws sts assume-role --role-arn ... --external-id ...` works from their credentials.
2. Write `bench/answer_key.json` with the user: the resource id and expected decision for every sandbox resource. Don't guess; the user labels it.
3. Run `scripts/bench_adoption.py` (with `run_plan_equivalence` on, which the script does itself). Save the output to `bench/results/<date>.json`. Commit the results, never credentials.
4. For every entry in `changing_resources`, find the attribute causing the diff. Sources: the `terraform plan` output in `plan_equivalence_results.checks`, and `config_crosscheck.mismatches`.
5. Fix it in `tools/hcl_generator.py` / `tools/aws_scanner.py` (add read-only discovery for the attribute, or omit it). Add a fidelity test in `tests/test_adoption_fidelity.py`, update `docs/aws` if you added an API call, and re-run the bench.
6. Repeat until no-op is above 90% and destroy/replace is 0. Classification mismatches go to `tools/resource_classifier.py` rules (with tests), never to an LLM.

**Done when:** the bench JSON meets the targets and each fix has a test.

## Task C: Known generator gaps (do the ones the bench exposes first)

1. **Route-table subnet associations:**
   - Generate `aws_route_table_association` with import blocks; the import ID is `subnet-id/rtb-id`.
   - Add the ID to `tools/infra_model.py::IMPORT_ID_FIELDS` handling, or give associations their own import entries in `generate_project`.
   - Remove the "left unmanaged" warning.
   - Test: an association plans as import with no create.
2. **Types that have templates but no discovery** (IGW, NAT with EIP allocation, ALB, DynamoDB with key type, KMS, SQS, SNS):
   - Add read-only discovery for each, then add the type to `DISCOVERY_RESOURCE_TYPES` in the classifier.
   - Also update: `IMPORT_ID_FIELDS`, `SOURCE_API`, the read-only boto test (via moto), and `docs/aws/*`.
3. **`github_client`:** use the Git Data API (blobs → tree → commit → ref) for one atomic commit. Keep the branch-cleanup-on-failure behavior and the token discipline, and update the fake queue in the tests.
4. Remove the legacy `agents/repair_agent.py`. Its deterministic S3 fix already lives in `tools/hardening.py`.

## Task D: Week 8 CEO demo

1. Check the demo script runs on the sandbox:
   - start a scan through the role
   - 4 agents working, repair loop visible if triggered
   - pause at the gate, decide the Review resources, resume
   - end summary like "N found: X managed, Y referenced, Z excluded · Migration Safety 100% · zero changes to production · K security findings in a separate Hardening PR"
   - open the adoption PR on GitHub, then the Hardening PR
2. Dashboard polish (user-approved): a "Nothing found" badge for completed 0-resource scans, a latest-scan summary card, and archive/delete for old test jobs (an API endpoint plus UI; soft-delete in the DB).
3. Rehearse twice. Record timings, and write `docs/demo/runbook.md`: setup, steps, fallback if AWS throttles, and the expected numbers taken from the last bench run.

## Task E: Phase 2, Change Request PRs (only when the user says so)

Follow `docs/design/phase2-change-requests/README.md`:
- Apply `analyzed_intent.requested_changes` to the *adopted* HCL deterministically.
- Plan against the adoption baseline: every change must be an in-place `update`; replace or destroy is destructive and always goes through approval.
- Show the Infracost delta.
- Open a separate Change Request PR type.
- Use the parked UI design at `docs/design/phase2-change-requests/ModifyInfrastructureView.tsx.txt`, fed by real plan, diff and cost data.
- The same safety rules apply: TerraAgent never applies.

---

# 9. Definition of done (every task)

- [ ] Host tests green; container tests green if terraform, discovery or graph code changed.
- [ ] `tsc` + `eslint` clean if the frontend changed; `lib/types.ts` matches the backend models.
- [ ] New behavior covered by tests, including a negative or safety test where it applies.
- [ ] `CLAUDE.md` updated if architecture, rules, env vars or endpoints changed.
- [ ] `docs/aws/*` updated if AWS permissions changed.
- [ ] No secrets in code, logs, tests, commits or PR bodies. `.env` and `tsbuildinfo` untouched.
- [ ] One focused commit on `feat/four-agent-restructure`, not pushed. Report to the user: what changed, what was verified (with test counts), what wasn't, and the next step.
