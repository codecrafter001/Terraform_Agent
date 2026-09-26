# TerraAgent: handoff prompt for a new Claude Code CLI session

Open Claude Code in `C:\Users\USER\Desktop\AIKART\Terraform Agent` and paste everything below the line
as the first message. It is self-contained. Written 2026-09-26, at commit `78b0260`.

---

# You are continuing TerraAgent. Read this fully before acting.

## 0. First actions (do these, then report back before editing anything)

1. Read `CLAUDE.md` (repo root). It is authoritative for architecture, safety rules and conventions,
   and it's kept up to date with every commit. Also read:
   - `docs/plans/remaining-work.md` (the phase plan)
   - `docs/aws/read-only-role.md`
   - `docs/design/phase2-change-requests/README.md`
2. Run `git status` and `git log --oneline -12`. Expect a clean tree at `78b0260` on
   `feat/four-agent-restructure`.
   - If there are changes you didn't make, **stop and ask**. Another agent (Antigravity) has edited
     this branch before.
   - Only one agent works on the branch at a time.
3. Run the host tests: `cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py`.
   Expect **314 passed**.
4. Run the frontend checks: `cd frontend && npx tsc --noEmit && npx eslint .`. Expect both clean.
5. Tell me in 5–10 lines what you found, which task you'll start (section 6), and wait for my OK.

## 1. What TerraAgent is

TerraAgent converts existing, hand-built ("ClickOps") AWS infrastructure into validated Terraform/OpenTofu,
delivered as a GitHub pull request. **It never modifies AWS.**
- The customer's own pipeline (Atlantis / HCP Terraform / Spacelift / GitHub Actions) applies after
  human review.
- **Core principle:** AWS facts → Canonical Infra Model → deterministic generation → LLM only where
  required.
- **Success metrics:**
  - no-op rate above 90% of managed resources
  - zero destroy/replace in any adoption PR
  - classification accuracy against a hand-labeled answer key
  - low human-review rate
  - scan time recorded

**Stack:**
- Backend: FastAPI + Celery/Redis + Postgres, LangGraph 1.2.11, Ollama
- Frontend: Next.js 16 (TS strict, Tailwind, lucide)
- Deployment: Docker Compose behind nginx
- Host: Windows 11, with Git Bash and PowerShell available

## 2. Hard safety rules (never break)

1. **Never run `terraform apply`, `destroy` or `import`.**
   - Every terraform/tofu call goes through `backend/tools/terraform_runner.py::TerraformRunner.run_command`.
   - Its `check_argv` allowlist: binary `terraform|tofu`; subcommands
     `version fmt init validate plan show providers`; `apply/destroy/import` refused anywhere in argv,
     including as flags.
   - Never call `create_subprocess_exec` on terraform yourself.
   - Import blocks (`imports.tf`) are reviewable text only.
2. **Read-only AWS:** only `Describe*/Get*/List*`, plus `sts:AssumeRole`.
   - `tests/test_cloud_discovery.py::test_scanner_only_ever_calls_read_only_apis` (moto + boto
     `before-call` hook) fails on anything else.
   - Every new AWS call must be:
     - covered by that test
     - added to `docs/aws/read-only-policy.json`, in both the `DiscoveryAndPlanReadOnly` Allow list
       and the `NotAction` list of `NeverChangeAnything`
     - listed in the table in `docs/aws/read-only-role.md`
3. **Credentials:**
   - Never store, log or print them, and never send them to an LLM.
   - Use `SecretStr` in models and `CredentialScrubber` on error text.
   - Paused-run checkpoints strip them (`services/checkpoints.py`, tested).
   - GitHub tokens are used once per request (merge body `SecretStr`, or the `X-GitHub-Token` header
     for GET) and never persisted.
4. **Discovered text is untrusted** (names, tags, descriptions, policies):
   - It goes into HCL only through `tools/hcl_render.py::hcl_str`.
   - It goes into PR markdown only through `services/github_client.py::_md`.
   - Tags are only matched against fixed keys.
5. **Adoption code must plan with zero changes** (`tools/hcl_generator.py::_compose_managed_resource`):
   - exact live values and tags
   - a missing optional attribute is omitted
   - a missing required attribute makes the resource unresolved, which sends it to review
   - no provider `default_tags`, no hardening
   - security fixes go only in the Hardening proposal (`tools/hardening.py`)
6. **Fail closed:**
   - A crash, timeout, missing tool, unparseable output, incomplete discovery, or re-verification
     without credentials gives **INCOMPLETE**, never PASS.
   - Scores are `null` without evidence.
   - Missing Infracost means "not estimated", never $0.
7. **Containers and shell:** containers run as the non-root `agent` user. Never build shell commands
   from user input.
8. **PR lifecycle:**
   - No in-app PR approval: TerraAgent opens the PR, so approving it here would be self-review.
   - Merge only this job's own PR, only with `confirm: true`, GitHub reporting it open + mergeable,
     APPROVED with no CHANGES_REQUESTED, and (hardening) after the adoption PR is merged and the
     hardening PR retargeted onto its base.

## 3. Architecture (`backend/agents/graph.py`)

```
infrastructure -> iac_engineering -> verification --PASS/INCOMPLETE/NEEDS_APPROVAL--> delivery -> END
                     ^  ^                |                                               |
                     |  +---- FAIL ------+  (validation errors only, max 2 repairs)      |
                     +------- human turned Review resources into manage/reference -------+
```

1. **Infrastructure:**
   - Steps: `intent_router`; `resource_explorer_step`; `cloud_discovery` (`tools/aws_scanner.py`);
     `graph_agent`; `classification_agent` (`tools/resource_classifier.py`, rules first).
   - `cloud_discovery` runs service scans in parallel with adaptive retries. Failures are recorded in
     `state["discovery"]`, and an incomplete discovery means INCOMPLETE.
   - Output: the Canonical Infra Model (`tools/infra_model.py`, `IMPORT_ID_FIELDS`, `SOURCE_API`).
   - Access: `role_arn` + `external_id`. After AssumeRole, `state["aws_credentials"]` holds the role's
     temporary credentials, so drift, plan and the cross-check read the same account.
2. **IaC Engineering:**
   - `adoption_planning_agent`, then `terraform_composer` (`tools/hcl_generator.py`: root stack files
     `foundation/security/data/application.tf` + `imports.tf`; `terraform fmt` applied).
   - Route-table subnet associations are their own resources with `subnet-id/rtb-id` import blocks.
   - On FAIL: `agents/validation_repair.py`; every fix must pass `tools/hcl_invariants.py`.
3. **Verification & Risk** (judges, never edits):
   - `validation_agent` → `drift_reconciliation_agent` → `plan_equivalence_agent` (opt-in real plan) →
     `config_crosscheck` → `policy_agent` (Checkov/Trivy/Conftest, report only).
   - Two separate scores (`tools/scores.py`): **Migration Safety** and **Security Posture**.
4. **Delivery & Approval:**
   - Risk gate: LangGraph `interrupt()` on unapproved risky findings or Review resources.
     `services/pipeline.py` + `services/checkpoints.py` persist the pause.
     `POST /api/scan/{id}/approve|reject` resumes it.
   - Then `hardening_agent`, `cost_agent` (Infracost, hardening delta only), and `documentation_agent`.
   - Delivery is PR-first (`services/github_client.py`):
     - each PR is **one atomic commit** via the Git Data API (`_publish`)
     - the **Adoption PR** covers the whole job or one wave
     - the **Hardening PR** is stacked on the adoption branch
   - PR lifecycle page: `/results/{id}/pr` (`GitHubPrViewer`, gated merge).

**API** (prefix `/api`; header `X-API-Key` when `TERRAAGENT_API_KEY` is set):
- `POST /scan`
- `POST /scan/analyze-intent`
- `GET /scan/{id}/status`
- `GET /scan/{id}/results`
- `GET /scan/{id}/logs` (SSE)
- `POST /scan/{id}/approve`
- `POST /scan/{id}/reject`
- `POST /scan/{id}/pull-request` with `{github_token, repo, base_branch?, wave?, kind: adoption|hardening}`
- `GET /scan/{id}/pull-request?kind=`
- `POST /scan/{id}/pull-request/merge` with `{github_token, kind, confirm, merge_method}`
- `GET /jobs` (includes PR links)
- `GET /download/{id}`

**Frontend pages:**
- `/` dashboard
- `/scan` new request
- `/scan/[id]` progress
- `/results/[id]` tabs: Overview / Inventory / Topology / Verification / Pull request & bundle
- `/results/[id]/pr`
- `/results/[id]/graph`
- `/pull-requests`

The mock pages (`/requests`, `/runs`, `/settings`) were moved to `docs/design/ui-mockups/` on purpose.
Don't restore them without real data. `lib/types.ts` mirrors `backend/models/*.py`.

## 4. Configuration

**`.env` (repo root, never print or commit):**
- Required: `REDIS_PASSWORD`, `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`
- Recommended: `TERRAAGENT_API_KEY` (same value for backend and frontend)
- Optional: `INFRACOST_API_KEY`, `OLLAMA_MODEL` (default `codellama`)

**Backend tuning variables** (defaults in code):

| Variable | Default |
|---|---|
| `TERRAAGENT_MAX_REPAIR_ITERATIONS` | 2 |
| `TERRAAGENT_MANAGE_IAM` | false |
| `TERRAAGENT_TF_COMMAND_TIMEOUT` | 600 |
| `TERRAAGENT_SCANNER_TIMEOUT` | 300 |
| `TERRAAGENT_HEARTBEAT_SECONDS` | 15 |
| `MAX_JOB_RUNTIME_SECONDS` | 1800 |
| `MAX_ADOPTION_WAVE_SIZE` | 25 |
| `DEPENDENCY_CONFIDENCE_THRESHOLD` | 0.80 |
| `OLLAMA_TIMEOUT_SECONDS` | 300 |
| `OUTPUT_DIR` | `/tmp/terraagent` |
| `ZIP_EXPIRY_HOURS` | 24 |
| `TF_PLUGIN_CACHE_DIR` | `/home/agent/.terraform.d/plugin-cache` (`init` is file-locked) |
| `AWS_ENDPOINT_URL` / `LOCALSTACK_URL` | tests only |

**Containers and ports:**

| Container | Access |
|---|---|
| `terraagent-nginx` | http://localhost (UI) and http://localhost/api |
| `terraagent-api` | internal :8000 |
| `terraagent-celery` | worker; runs the graph |
| `terraagent-frontend` | :3002 → 3000 |
| `terraagent-redis` | password-protected; use `redis_service` inside the API container, not `redis-cli` |
| `terraagent-postgres` | internal |
| `terraagent-pgadmin` | :5050 |
| `terraagent-prometheus` | :9090 |
| `terraagent-grafana` | :3001 |
| `terraagent-ollama` | internal |
| `terraagent-localstack` | :4566, test-only AWS emulator |

Ports 3000 and 8000 belong to other apps on this machine.

**Toolchain:**
- Image: Python 3.11, Terraform 1.8.5, OpenTofu 1.8.5, Trivy 0.74.0, Conftest 0.69.0,
  Checkov 3.3.16 (pipx), Infracost 0.10.39. tfsec is removed.
- Host: Python 3.13, no terraform binary.

## 5. Commands and pitfalls

```bash
# Host tests
cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py
# Frontend checks
cd frontend && npx tsc --noEmit && npx eslint .
# Build + deploy (ALWAYS restart nginx afterwards, or it serves 502s)
docker compose build terraagent-api terraagent-celery terraagent-frontend
docker compose up -d --no-deps terraagent-api terraagent-celery terraagent-frontend
docker restart terraagent-nginx
# Real terraform + LocalStack, inside the worker (use -rs: a skip means LocalStack isn't running)
docker exec -e LOCALSTACK_URL=http://terraagent-localstack:4566 terraagent-celery \
  sh -c 'cd /app && python -m pytest -q -rs -p no:cacheprovider tests/test_langgraph_pipeline.py tests/test_access_and_discovery.py tests/test_terraform_runner_plan.py tests/test_cloud_discovery.py'
# If LocalStack is missing:
docker compose up -d terraagent-localstack
# Test bench against a sandbox account (credentials via environment only)
python scripts/bench_adoption.py --region us-east-1 --answer-key bench/answer_key.json --out bench/results/<date>.json
```

**Pitfalls (learned the hard way):**
- **Line endings are LF.** Write files with `newline='\n'`. `backend/Dockerfile` may differ, so use
  the Edit tool there.
- **Before every commit:** `git checkout -- frontend/tsconfig.tsbuildinfo frontend/next-env.d.ts`
  (build artifacts).
- **Bash heredocs mangle `\n` and `'''`.** Put multi-line edit scripts in a scratchpad `.py` file, or
  use the Edit tool.
- **Scripted splices:** when replacing a region, make sure the end marker isn't duplicated. Check with
  `python -c "import ast; ast.parse(open(f).read())"`.
- **Stale Next types:** `.next/types` can go stale after moving pages. Deleting `.next/types` is safe
  (it's regenerated).
- **Test helpers:**
  - Graph routing: stub steps by monkeypatching `agents.graph.<step>_node`, and use the `Run` helper
    in `tests/test_graph_agent_loop.py` (checkpointer + thread id).
  - GitHub client: `tests/test_github_pr.py::FakeGitHub` answers by method + path; don't assert exact
    call sequences.
  - Addresses: build them with `f"{type}.{tools.naming.unique_clean_name(name or id, id)}"`. Names
    carry a hash suffix, so never hardcode them.
- **Dev server:** open it at `localhost`, not `127.0.0.1`.
- **Git:** commit on `feat/four-agent-restructure` in the "Week N: ..." / descriptive style, ending with
  the co-author trailer. **Never push without asking.**
- **Code style:** full type hints; comments explain *why*; tests for every behavior change, safety
  properties included. Update `CLAUDE.md` when architecture, rules, environment variables or endpoints
  change.

## 6. Status of the plan (`docs/plans/remaining-work.md`) and your tasks

**Done:**
- Phases 1–3: Week 7 committed; the PR-lifecycle work reviewed and hardened; verified in the containers.
- Phase 5: 5.1 (route-table associations), 5.3 (Git Data API), 5.4 and 5.5 (legacy repair node and
  tfsec removed).

**Not deployed yet:** commits `ac7af38` and `78b0260` aren't in the running containers. Rebuild,
deploy, and re-run the container tests first.

### Task A: deploy and verify the last two commits (small)

Build and deploy, run the container tests (the e2e test must *pass*, not skip), and smoke-test
http://localhost, `/scan`, `/pull-requests`, `/results/<job>` and `/results/<job>/pr`.

### Task B: Phase 5.2, discovery for 7 more resource types (was in progress; no code written yet)

The templates exist in `tools/hcl_generator.py`, `IMPORT_ID_FIELDS` already has the import IDs, and
moto emulates every service (checked). Plan, per type:

| Type | Read-only discovery | Fields to record (adoption must be exact) |
|---|---|---|
| `aws_internet_gateway` | `ec2 DescribeInternetGateways` (paginator) + `DescribeVpcs` filter `isDefault` | id, vpc_id (from Attachments), tags, `is_default` = attached to the default VPC |
| `aws_nat_gateway` | `ec2 DescribeNatGateways` (only state `available`) | id, subnet_id, vpc_id, allocation_id (NatGatewayAddresses[0]), connectivity_type, tags |
| `aws_lb` | `elbv2 DescribeLoadBalancers` + `DescribeTags` (≤20 ARNs per call) | arn (import ID), name, scheme, type, subnets (from AvailabilityZones), security_groups, vpc_id, tags |
| `aws_dynamodb_table` | `ListTables` + `DescribeTable` + `ListTagsOfResource` | name, billing_mode, hash_key + type, range_key + type, read/write capacity (PROVISIONED), tags; **GSIs/LSIs → unresolved (review)** |
| `aws_kms_key` | `ListKeys` + `DescribeKey` + `GetKeyRotationStatus` + `ListResourceTags` | key_id, description, enable_key_rotation, tags; `KeyManager == "AWS"` or `PendingDeletion` → mark and **exclude** in the classifier |
| `aws_sqs_queue` | `ListQueues` (paginator) + `GetQueueAttributes(All)` + `ListQueueTags` | url (import ID), name, every scalar attribute (visibility timeout, retention, delay, max size, receive wait, fifo, content dedup, SSE/KMS, redrive policy) |
| `aws_sns_topic` | `ListTopics` + `GetTopicAttributes` + `ListTagsForResource` | arn (import ID), name, display_name, fifo, kms_master_key_id, tags |

Steps:
1. Put the scans in a new module (for example `tools/aws_scanner_extra.py`), or in `aws_scanner.py`
   using `self._client(...)` and `self._failed(...)`, so failures mark the scan incomplete.
2. Register them in `AWSScanner.scan_all` under new filter categories: `ELB`, `DYNAMODB`, `KMS`, `SQS`,
   `SNS`. IGW and NAT go under `VPC`.
3. Update the templates in `_compose_managed_resource`:
   - DynamoDB: range key, capacity, and GSIs/LSIs → unresolved.
   - SQS and SNS: exact attributes.
   - Everything via `hcl_str` and `tags_block`.
4. Update the classifier (`tools/resource_classifier.py`):
   - add the types to `DISCOVERY_RESOURCE_TYPES`
   - the default VPC's IGW → same rule as the default VPC
   - AWS-managed or pending-deletion KMS keys → exclude
   - `SOURCE_API` entries in `tools/infra_model.py`
5. Graph (`tools/graph_builder.py`): add edges for a load balancer's `subnets` list, and NAT →
   allocation if useful.
6. Tests:
   - extend the read-only boto test with all new filters (moto)
   - per-type fidelity tests in `tests/test_adoption_fidelity.py`
   - classifier tests
7. Update `docs/aws/read-only-policy.json` (Allow + `NotAction`), `docs/aws/read-only-role.md`, and
   `CLAUDE.md`.
8. Frontend: add the new categories to the resource chips in `components/CredentialForm.tsx` /
   `UserRequestSection.tsx`, and to the default `resource_filters` in `backend/models/scan.py`.

**Done when:** host tests are green, the read-only test covers every new API, and there's one commit.

### Task C: Phase 6.1–6.2, demo polish (no sandbox needed)

- **"Nothing found" badge:** on dashboard rows for completed 0-resource scans.
- **Latest-scan summary card:** at the top of the dashboard.
- **Archive/delete old jobs:** soft-delete column `archived` on `JobRecord` (`init_db` adds columns
  automatically), `DELETE /api/jobs/{id}` (soft), filtered out of `GET /api/jobs` by default, plus UI.
- **End-of-run summary line on `/scan/[id]`:** "N found: X managed, Y referenced, Z excluded ·
  Migration Safety N% · zero changes to production · K findings in a separate Hardening PR", from
  `infra_model.summary`, `migration_safety` and `hardening`.

### Task D: Phase 4, sandbox test bench (BLOCKED on the user)

**Needs from me:** a sandbox AWS account, a read-only role per `docs/aws/read-only-role.md` (role ARN +
ExternalId), a hand-labeled `bench/answer_key.json`, and a GitHub test repo + token.

**Loop:**
1. Run `scripts/bench_adoption.py`.
2. For each entry in `migration_safety.changing_resources`, find the attribute (plan output /
   `config_crosscheck.mismatches`).
3. Fix discovery or the template, and add a fidelity test.
4. Re-run until no-op ≥ 90% and destroy/replace = 0.

Commit the bench results, never credentials.

### Task E: Phase 6.3–6.4, CEO demo runbook (after D)

`docs/demo/runbook.md`: setup, a click-by-click script, the expected numbers from the last bench, and
fallbacks (throttling → re-run; Ollama slow → deterministic paths; GitHub down → ZIP). Then two
rehearsals.

### Task F: Phase 7, Change Request PRs (only when I explicitly ask)

Per `docs/design/phase2-change-requests/README.md`. Today modify jobs show `ChangeRequestNotice` (the
real parsed requested changes plus an honest "not applied in this run" note).

## 7. Definition of done (every task)

- [ ] Host tests green; container tests green (e2e *passed*, not skipped) if terraform, discovery,
      graph or GitHub code changed.
- [ ] `tsc` + `eslint` clean if the frontend changed; `lib/types.ts` matches the backend models.
- [ ] Tests for the new behavior, including safety/negative tests.
- [ ] `CLAUDE.md` and `docs/aws/*` updated where relevant.
- [ ] No secrets anywhere. `.env`, `tsbuildinfo` and `next-env.d.ts` untouched in the commit.
- [ ] One focused commit per task on `feat/four-agent-restructure`, not pushed.
- [ ] Report to me: what changed, what was verified (with test counts), what wasn't, and the next step.
