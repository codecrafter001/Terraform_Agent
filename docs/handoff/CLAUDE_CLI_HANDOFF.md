# TerraAgent: universal handoff prompt (full context + phase-by-phase plan)

Open Claude Code (or any coding agent) in `C:\Users\USER\Desktop\AIKART\Terraform Agent` and paste
everything below the line as the first message. It is self-contained.
Written 2026-09-26. Last commit `2dfa73d` on branch `feat/four-agent-restructure`.

---

# You are continuing TerraAgent. Read all of this before acting.

## 0. First actions (then report back and wait for my OK)

1. Read these, in this order:
   - `CLAUDE.md` (repo root): authoritative architecture, safety rules and conventions
   - `docs/aws/read-only-role.md`
   - `docs/design/phase2-change-requests/README.md`
2. Run `git status` and `git log --oneline -12`. Expect a clean tree at `2dfa73d` (or this handoff file
   as the only change).
   - If there are changes you didn't make, **stop and ask**. Only one agent works on this branch at a
     time.
3. Run the host tests: `cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py`.
   Expect **314 passed**.
4. Run the frontend checks: `cd frontend && npx tsc --noEmit && npx eslint .`. Expect both clean.
5. Report in 5–10 lines: the state, anything unexpected, and which phase you'll start (section 7). Then
   wait for my OK.

---

## 1. Product

TerraAgent converts existing, hand-built ("ClickOps") AWS infrastructure into validated
Terraform/OpenTofu, delivered as a GitHub pull request. **It never modifies AWS.**
- The customer's own pipeline (Atlantis / HCP Terraform / Spacelift / GitHub Actions) applies after
  human review.
- **Core principle:** AWS facts → Canonical Infra Model → deterministic generation → LLM only where
  required.

**Success metrics** (measured with `scripts/bench_adoption.py` on a sandbox account):

| Metric | Target |
|---|---|
| No-op rate | above 90% of managed resources |
| Destroy/replace | zero in any adoption PR |
| Classification accuracy | recorded, against a hand-labeled answer key |
| Human-review rate | as low as possible while the no-op rate holds |
| Scan time | recorded |

**The demo the product is heading to** (Week 8, for the CEO):
1. Point TerraAgent at the sandbox.
2. The 4 agents work on screen, with the repair loop visible.
3. It pauses at the risk gate; a human decides the Review resources; it resumes.
4. It ends with "38 resources found: 30 managed, 5 referenced, 3 excluded · Migration Safety 100% ·
   zero changes to production · 4 security findings in a separate Hardening PR".
5. The adoption PR opens on GitHub, then the Hardening PR.

**Stack:**
- Backend: FastAPI + Celery/Redis + Postgres, LangGraph 1.2.11, Ollama
- Frontend: Next.js 16 (TypeScript strict, Tailwind, lucide)
- Deployment: Docker Compose behind nginx
- Host: Windows 11, with Git Bash and PowerShell

---

## 2. Hard safety rules (never break)

1. **Never run `terraform apply`, `destroy` or `import`.**
   - Every terraform/tofu call goes through `backend/tools/terraform_runner.py::TerraformRunner.run_command`.
   - Its `check_argv` enforces: binary `terraform|tofu`; subcommands
     `version fmt init validate plan show providers`; `apply/destroy/import` refused anywhere in argv,
     including as flags (`plan -destroy`).
   - Never call `create_subprocess_exec` on terraform yourself.
   - `imports.tf` import blocks are reviewable text only.
2. **Read-only AWS:** only `Describe*/Get*/List*`, plus `sts:AssumeRole`.
   - `backend/tests/test_cloud_discovery.py::test_scanner_only_ever_calls_read_only_apis` records every
     boto operation (moto + `before-call` hook) and fails on anything else.
   - Every new AWS call must be:
     - covered by that test
     - added to `docs/aws/read-only-policy.json`, in both the Allow list `DiscoveryAndPlanReadOnly` and
       the `NotAction` list of `NeverChangeAnything`
     - listed in the table in `docs/aws/read-only-role.md`
3. **Credentials:**
   - Never store, log or print them, and never send them to an LLM.
   - Use `SecretStr` in models and `CredentialScrubber` on any error text.
   - Paused-run checkpoints strip them (`services/checkpoints.py`; a test proves no secret byte is
     stored).
   - GitHub tokens are used once per request (merge body `SecretStr`, or the `X-GitHub-Token` header on
     GET) and never persisted.
4. **Discovered text is untrusted** (names, tags, descriptions, policy documents):
   - It goes into HCL only through `tools/hcl_render.py::hcl_str` (escapes quotes, backslashes,
     newlines and `${`/`%{`).
   - It goes into PR markdown only through `services/github_client.py::_md`.
   - Tags are only matched against fixed keys.
5. **Adoption code must plan with ZERO changes** (`tools/hcl_generator.py::_compose_managed_resource`):
   - exact live values and exact live tags (`tags_block`)
   - a missing optional attribute is omitted (the import keeps the live value)
   - a missing required attribute makes the resource unresolved, which sends it to review
   - no provider `default_tags`, no security hardening
   - security fixes go only in the Hardening proposal (`tools/hardening.py`)
6. **Fail closed:**
   - A crash, timeout, missing tool, unparseable output, incomplete discovery, or re-verification
     without credentials gives verdict **INCOMPLETE**, never PASS.
   - Scores are `null` without evidence, never 100.
   - Missing Infracost means "not estimated", never $0.
7. **Containers and shell:** containers run as the non-root `agent` user. Never build shell commands
   from user input.
8. **PR lifecycle:**
   - No in-app PR approval: TerraAgent opens the PR, so approving it here would be self-review.
   - Merge only this job's own PR, and only with `confirm: true`, GitHub reporting it open + mergeable,
     an APPROVED review with no CHANGES_REQUESTED, and (hardening) the adoption PR merged and the
     hardening PR retargeted onto its base.
   - TerraAgent never applies; merging may start the team's apply-on-merge pipeline, and the UI says
     so.

---

## 3. Architecture (`backend/agents/graph.py`)

```
infrastructure -> iac_engineering -> verification --PASS/INCOMPLETE/NEEDS_APPROVAL--> delivery -> END
                     ^  ^                |                                               |
                     |  +---- FAIL ------+  (validation errors only, max 2 repairs)      |
                     +------- human turned Review resources into manage/reference -------+
```

**1. Infrastructure Agent**
- Steps:
  - `intent_router` (+ `tools/intent_analyzer.py`)
  - `resource_explorer_step` (Resource Explorer, `ListIndexes`/`Search` only)
  - `cloud_discovery` (`tools/aws_scanner.py`: service scans in parallel via a thread pool, adaptive
    retries; every failed call goes into `state["discovery"]`, and incomplete means INCOMPLETE)
  - `graph_agent` (`tools/graph_builder.py`)
  - `classification_agent` (`tools/resource_classifier.py`: rules first, **manage / reference /
    exclude / review**; IAM roles go to review unless `TERRAAGENT_MANAGE_IAM=true`)
- Output: the **Canonical Infra Model** (`tools/infra_model.py`; `IMPORT_ID_FIELDS` gives import IDs,
  never an LLM; `SOURCE_API`).
- **Access:** `role_arn` + `external_id` on the scan request. After AssumeRole,
  `state["aws_credentials"]` holds the role's 1-hour credentials, so every later live check reads the
  target account.

**2. IaC Engineering Agent**
- `adoption_planning_agent` (`tools/adoption_planner.py`), then `terraform_composer`
  (`tools/hcl_generator.py`: root stack files `foundation/security/data/application.tf` +
  `imports.tf`; `terraform fmt`).
- Route-table subnet associations are their own resources with `subnet-id/rtb-id` import blocks.
- On FAIL: `agents/validation_repair.py`. Every fix must pass
  `tools/hcl_invariants.py::check_repair_invariants`.

**3. Verification & Risk Agent** (judges, never edits)
- `validation_agent` → `drift_reconciliation_agent` → `plan_equivalence_agent` (opt-in real plan with
  import blocks) → `config_crosscheck` (`plan -generate-config-out`) → `policy_agent`
  (Checkov/Trivy/Conftest, report only).
- Two separate scores (`tools/scores.py`): **Migration Safety** (SAFE/CHANGES/DESTRUCTIVE/UNVERIFIED)
  and **Security Posture**.

**4. Delivery & Approval Agent**
- **Risk gate:** LangGraph `interrupt()` on unapproved risky findings or Review resources.
  `services/pipeline.py` + `services/checkpoints.py` persist the pause. `POST /api/scan/{id}/approve`
  (deciding every Review resource) or `/reject` resumes it.
- Then `hardening_agent` (`tools/hardening.py`: S3 public-access block, S3 encryption, EC2 IMDSv2, only
  where missing; invariant-checked + validated), `cost_agent` (Infracost, hardening delta only), and
  `documentation_agent`.
- **Delivery is PR-first** (`services/github_client.py`):
  - one atomic commit per PR via the Git Data API (`_publish`)
  - the Adoption PR (whole job or one wave), with both scores, a per-resource plan table, decisions
    and findings
  - the Hardening PR, stacked on the adoption branch
  - the ZIP is secondary

**API** (prefix `/api`; header `X-API-Key` when `TERRAAGENT_API_KEY` is set):
- `POST /scan`
- `POST /scan/analyze-intent`
- `GET /scan/{id}/status`
- `GET /scan/{id}/results`
- `GET /scan/{id}/logs` (SSE with replay)
- `POST /scan/{id}/approve`
- `POST /scan/{id}/reject`
- `POST /scan/{id}/pull-request` with `{github_token, repo, base_branch?, wave?, kind: adoption|hardening}`
- `GET /scan/{id}/pull-request?kind=`
- `POST /scan/{id}/pull-request/merge` with `{github_token, kind, confirm, merge_method}`
- `GET /jobs` (includes PR links)
- `GET /download/{id}`

**Frontend** (`frontend/`):

| Route | Page |
|---|---|
| `/` | dashboard |
| `/scan` | new request (credentials, role ARN/ExternalId, NL request box, intent modal) |
| `/scan/[id]` | 4 agent cards, loop, live log, approval panel |
| `/results/[id]` | tabs: Overview / Inventory / Topology / Verification / Pull request & bundle |
| `/results/[id]/pr` | `GitHubPrViewer`: live status, diff, reviews, gated merge |
| `/results/[id]/graph` | full-screen dependency graph |
| `/pull-requests` | real list from `/api/jobs` |

- Mock pages live in `docs/design/ui-mockups/` on purpose. Don't restore them without real data.
- `lib/types.ts` mirrors `backend/models/*.py`.
- Modify/fix jobs show `ChangeRequestNotice`: the real parsed requested changes, plus a "not applied
  in this run" note.

---

## 4. Configuration

**`.env` (repo root; never print or commit):**

| Key | Required | Notes |
|---|---|---|
| `REDIS_PASSWORD` | yes | Compose builds the Redis/Celery URLs from it |
| `POSTGRES_PASSWORD` | yes | Compose builds `DATABASE_URL` from it |
| `GRAFANA_ADMIN_PASSWORD` | yes | Grafana admin |
| `TERRAAGENT_API_KEY` | recommended | Must be the same value for the backend and frontend services |
| `INFRACOST_API_KEY` | optional | Without it, the hardening cost delta is "not estimated" |
| `OLLAMA_MODEL` | optional | Default `codellama` |

**Backend tuning** (defaults in code):

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
| `terraagent-nginx` | **http://localhost** (UI) and **http://localhost/api** |
| `terraagent-api` | internal :8000 |
| `terraagent-celery` | worker; runs the graph |
| `terraagent-frontend` | :3002 |
| `terraagent-redis` | password-protected; use `redis_service` inside the API container |
| `terraagent-postgres` | internal |
| `terraagent-pgadmin` | :5050 |
| `terraagent-prometheus` | :9090 |
| `terraagent-grafana` | :3001 |
| `terraagent-ollama` | internal |
| `terraagent-localstack` | :4566, test-only AWS emulator (it has disappeared once; recreate it if missing) |

Ports 3000 and 8000 on this machine belong to other apps.

**Toolchain:**
- Image: Python 3.11, Terraform 1.8.5, OpenTofu 1.8.5, Trivy 0.74.0, Conftest 0.69.0,
  Checkov 3.3.16 (pipx), Infracost 0.10.39. tfsec was removed.
- Python pins: `langgraph==1.2.11`, `langgraph-checkpoint==4.2.0`, `langchain-core==1.6.0`,
  `pydantic==2.7.4`, `boto3==1.34.131`.
- Host: Python 3.13, no terraform binary.

---

## 5. Commands

```bash
# Host tests (expect 314 passed)
cd backend && python -m pytest -q -p no:warnings --ignore=tests/test_langgraph_pipeline.py
# Frontend
cd frontend && npx tsc --noEmit && npx eslint .
# Build + deploy (ALWAYS restart nginx afterwards, or 502s)
docker compose build terraagent-api terraagent-celery terraagent-frontend
docker compose up -d --no-deps terraagent-api terraagent-celery terraagent-frontend
docker restart terraagent-nginx
# Real terraform + LocalStack in the worker. Use -rs: the e2e test must PASS, not SKIP
docker compose up -d terraagent-localstack   # if missing
docker exec -e LOCALSTACK_URL=http://terraagent-localstack:4566 terraagent-celery \
  sh -c 'cd /app && python -m pytest -q -rs -p no:cacheprovider tests/test_langgraph_pipeline.py tests/test_access_and_discovery.py tests/test_terraform_runner_plan.py tests/test_cloud_discovery.py tests/test_github_pr.py tests/test_adoption_fidelity.py'
# Smoke test
for u in / /scan /pull-requests /api/health; do curl -s -o /dev/null -w "$u %{http_code}\n" http://localhost$u; done
# Test bench (sandbox; credentials only via environment)
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... TERRAAGENT_ROLE_ARN=... TERRAAGENT_EXTERNAL_ID=...
python scripts/bench_adoption.py --region us-east-1 --answer-key bench/answer_key.json --out bench/results/<date>.json
```

**Pitfalls (learned the hard way):**
- **Line endings are LF.** Write files with `newline='\n'`. `backend/Dockerfile` may differ, so edit it
  with an exact-match editor.
- **Before every commit:** `git checkout -- frontend/tsconfig.tsbuildinfo frontend/next-env.d.ts`
  (build artifacts).
- **Bash heredocs mangle `\n` and `'''`.** Put multi-line edit scripts in a scratch `.py` file, or use
  the file-edit tool.
- **Scripted splices:** they have duplicated end markers before. Verify with
  `python -c "import ast; ast.parse(open(f, encoding='utf-8').read())"`.
- **Stale Next types:** `.next/types` can go stale after moving pages; delete it (it's regenerated).
- **Test helpers:**
  - Graph tests: monkeypatch `agents.graph.<step>_node`, and use the `Run` helper in
    `tests/test_graph_agent_loop.py`.
  - GitHub tests: `tests/test_github_pr.py::FakeGitHub` answers by method + path.
  - Addresses: build them with `f"{type}.{tools.naming.unique_clean_name(name or id, id)}"`. Names
    carry a hash suffix, so never hardcode them.
- **Dev server:** use `localhost`, not `127.0.0.1`.
- **Git:** commit on `feat/four-agent-restructure` with descriptive messages ending in the co-author
  trailer. **Never push without asking.**
- **Code style:** full type hints; comments explain *why*; tests for every behavior change, safety
  properties included. Update `CLAUDE.md` when architecture, rules, environment variables or endpoints
  change.

---

## 6. History (what's done)

| Commit | Content |
|---|---|
| `5871455`, `9e4f174`, `c1be045` | Weeks 2–4: four agents, repair moved and fail-closed, Canonical Infra Model + classification, import blocks + `generate-config-out` cross-check |
| `98db167` | NL request box + intent analysis; Change-Request PRs parked as Phase 2 |
| `01b8898` | Week 5: split scores, `interrupt()` approval with Review decisions and credential-free checkpoints, adoption fidelity (no fake tags/hardening, HCL escaping, EC2 AMI discovery), Hardening proposal, Infracost only for hardening |
| `103cdc7` | Week 6: PR-first delivery, richer adoption PR, stacked Hardening PR |
| `65088cb` | Week 7: ExternalId + role credentials for every live check, incomplete-discovery status, parallel scans + adaptive retries, read-only boto test, runner subcommand allowlist + plugin-cache init lock, tfsec out of the image, `docs/aws` role guide + policies, bench script |
| `12bb6ab` | PR lifecycle: live PR status/diff, guarded merge, no self-approval, real `/pull-requests`, mock screens removed |
| `354f9a3` | Removed the legacy security repair node and the tfsec runner |
| `ac7af38` | Route-table subnet associations adopted with import blocks |
| `78b0260` | PRs are one atomic commit via the Git Data API |
| `2dfa73d` | This handoff |

**Deployment state:**
- The containers were rebuilt and redeployed at `2dfa73d` (build OK, services restarted).
- The container test run for that build was cut off, so **re-run Phase A step 2 to confirm**.

---

## 7. Phase-by-phase plan for the remaining work

Do the phases in order unless I say otherwise. Each phase ends with its own commit, and you report to
me.

### Phase A: Verify the current deployment (small, ~15 min)

1. Check `docker ps`: api, celery, frontend, nginx, redis, postgres and localstack are all up. Recreate
   LocalStack if it's missing.
2. Run the container test command (section 5).
   - **Expect:** everything passes, and only `test_published_read_only_policy_never_allows_a_write`
     is skipped (`docs/` isn't in the image).
   - The LocalStack e2e test must **pass**.
3. Smoke test: `/`, `/scan`, `/pull-requests` and `/api/health` return 200. `/requests`, `/runs` and
   `/settings` return 404 (intended). Open one `/results/<job>` and its `/pr` page.

**Done when:** all green; nothing to commit unless something needed fixing.

### Phase B: Discovery for 7 more resource types (medium, main remaining engineering)

**Why:** templates exist for these in `tools/hcl_generator.py`, and `IMPORT_ID_FIELDS` has their import
IDs, but discovery never finds them, so the classifier marks them unsupported. moto emulates all these
services (checked). **No code has been written for this phase yet.**

**Design:**
- Add a mixin module `backend/tools/aws_scanner_services.py` with class `ExtraServiceScans`, whose scan
  methods use `self._client(service)` and `self._failed(scope, error)` exactly like `aws_scanner.py`.
- Make `AWSScanner` inherit it: `class AWSScanner(ExtraServiceScans, CloudDiscoveryInterface)`.
- Register the scans in `AWSScanner.scan_all`:
  - `VPC` filter: also `internet_gateways`, `nat_gateways`
  - new filters: `ELB` → `load_balancers`, `DYNAMODB` → `dynamodb_tables`, `KMS` → `kms_keys`,
    `SQS` → `sqs_queues`, `SNS` → `sns_topics`
- Every call is read-only and every failure goes through `_failed`.

**Per type** (record exactly what the adoption template needs for a zero-change plan):

| Type | Read-only calls | Record | Notes |
|---|---|---|---|
| `aws_internet_gateway` | `ec2 describe_internet_gateways` (paginator), `ec2 describe_vpcs` (filter `isDefault=true`) | id, name (Name tag), vpc_id (`Attachments[0].VpcId`), tags, `is_default` (attached to the default VPC) | default VPC's IGW = AWS default |
| `aws_nat_gateway` | `ec2 describe_nat_gateways` (paginator; keep state `available`) | id, name, subnet_id, vpc_id, allocation_id (`NatGatewayAddresses[0].AllocationId`), connectivity_type, tags | public NAT without allocation → unresolved (template already) |
| `aws_lb` | `elbv2 describe_load_balancers` (paginator), `elbv2 describe_tags` (≤20 ARNs per call) | arn (import ID), id = arn, name, scheme, load_balancer_type (`Type`), subnets (`AvailabilityZones[].SubnetId`), security_groups, vpc_id, tags | template already renders these |
| `aws_dynamodb_table` | `dynamodb list_tables` (paginator), `describe_table`, `list_tags_of_resource` | name (import ID), id = name, billing_mode (`BillingModeSummary.BillingMode`, default `PROVISIONED`), hash_key + type, range_key + type, read/write capacity when PROVISIONED, has_indexes, tags | template: add `range_key` + its attribute block, `read_capacity`/`write_capacity` when PROVISIONED; **GSIs/LSIs → unresolved (review)** |
| `aws_kms_key` | `kms list_keys` (paginator), `describe_key`, `get_key_rotation_status` (customer keys only), `list_resource_tags` | key_id (import ID), id, description, enable_key_rotation, key_manager, key_state, tags | classifier: `key_manager == "AWS"` or `key_state == "PendingDeletion"` → **exclude** |
| `aws_sqs_queue` | `sqs list_queues` (paginator), `get_queue_attributes(AttributeNames=["All"])`, `list_queue_tags` | url (import ID), id = url, name, and every scalar attribute: `VisibilityTimeout`, `MessageRetentionPeriod`, `DelaySeconds`, `MaximumMessageSize`, `ReceiveMessageWaitTimeSeconds`, `FifoQueue`, `ContentBasedDeduplication`, `SqsManagedSseEnabled`, `KmsMasterKeyId`, `RedrivePolicy`, tags | template: render each as the matching `aws_sqs_queue` argument (`visibility_timeout_seconds`, `message_retention_seconds`, `delay_seconds`, `max_message_size`, `receive_wait_time_seconds`, `fifo_queue`, `content_based_deduplication`, `sqs_managed_sse_enabled`, `kms_master_key_id`, `redrive_policy` as `jsonencode`), all via `hcl_str`/ints/bools |
| `aws_sns_topic` | `sns list_topics` (paginator), `get_topic_attributes`, `list_tags_for_resource` | arn (import ID), id = arn, name (last ARN segment), display_name, fifo_topic, kms_master_key_id, tags | template: add `display_name`, `fifo_topic`, `kms_master_key_id` when present |

**Other files to update:**
- `tools/resource_classifier.py`:
  - add the 7 types to `DISCOVERY_RESOURCE_TYPES`
  - extend the default rule (`aws_vpc` + `is_default`) to `aws_internet_gateway` + `is_default`
  - KMS AWS-managed / pending-deletion → exclude, with evidence rule `aws_managed_key`
- `tools/infra_model.py::SOURCE_API`: add the 7 source APIs.
- `tools/graph_builder.py`: add edges from a load balancer's `subnets` list (like the `security_groups`
  loop).
- `backend/models/scan.py`: add the new categories to the default `resource_filters`. Also the
  frontend chips in `components/CredentialForm.tsx` and `components/UserRequestSection.tsx`.
- `docs/aws/read-only-policy.json`: add `ec2:Describe*` (already), `elasticloadbalancing:Describe*`,
  `dynamodb:ListTables`, `dynamodb:DescribeTable`, `dynamodb:ListTagsOfResource`, `kms:ListKeys`,
  `kms:DescribeKey`, `kms:GetKeyRotationStatus`, `kms:ListResourceTags`, `sqs:ListQueues`,
  `sqs:GetQueueAttributes`, `sqs:ListQueueTags`, `sns:ListTopics`, `sns:GetTopicAttributes`,
  `sns:ListTagsForResource`.
  - Put them in **both** the Allow list and the `NotAction` list.
  - Keep the `NeverReadData` denies (`dynamodb:GetItem/Query/Scan`, `kms:Decrypt`,
    `sqs:ReceiveMessage` stay denied).
  - Update the table in `docs/aws/read-only-role.md`.

**Tests:**
- Extend `test_scanner_only_ever_calls_read_only_apis`: create one of each resource in moto, scan with
  filters `VPC,SG,EC2,S3,RDS,IAM,ELB,DYNAMODB,KMS,SQS,SNS`, and assert only Describe/Get/List
  operations were called.
- One fidelity test per type in `tests/test_adoption_fidelity.py` (exact attributes, tags, import ID).
- Classifier tests: default IGW → reference/exclude; AWS-managed KMS key → exclude; DynamoDB with a GSI
  → review.
- A failure test: a failing service marks discovery incomplete.

**Done when:** host tests are green, the container tests are green (rebuild), `CLAUDE.md` +
`docs/aws/*` are updated, and there's one commit: "Discover IGW, NAT, ALB, DynamoDB, KMS, SQS, SNS".

### Phase C: Demo polish (medium, no sandbox needed)

1. **Dashboard (`frontend/app/page.tsx`):**
   - a "Nothing found" badge on completed jobs with `resources_discovered == 0`
   - a latest-scan summary card at the top: status, resources, Migration Safety, PR link
2. **Archive old test jobs:**
   - Add a nullable `archived` Boolean column to `JobRecord` (`models/orm.py`; `init_db` adds missing
     columns automatically).
   - Add `DELETE /api/jobs/{id}` in `routers/jobs.py` as a soft delete (`archived = true`).
   - `GET /api/jobs` hides archived jobs unless `?include_archived=true`.
   - Add an "Archive" button on dashboard rows, with a confirm.
   - Tests.
3. **End-of-run summary line on `/scan/[id]` (`components/ScanProgress.tsx`)** once COMPLETE:
   - format: "N found: X managed, Y referenced, Z excluded · Migration Safety N% · zero changes to
     production · K findings in a separate Hardening PR"
   - sources: `infra_model.summary`, `migration_safety`, `security_posture` / `hardening`
   - say "not measured" instead of inventing numbers
4. Type check, lint, tests, rebuild, smoke test, commit.

### Phase D: Sandbox test bench (BLOCKED on me, the user; ask for these items)

**What you need from me:**
- a sandbox AWS account with realistic resources: VPC, subnets, route tables, security groups, EC2, S3,
  RDS, a few IAM roles, and one each of the Phase B types
- a read-only role per `docs/aws/read-only-role.md`, plus its **role ARN + ExternalId**
- **`bench/answer_key.json`**: `{resource_id: manage|reference|exclude|review}`, labeled by me
- a **GitHub test repo + token** for the PR flow

**Loop:**
1. Confirm the role: `aws sts assume-role --role-arn <arn> --role-session-name check --external-id <id>`.
2. Run the bench (section 5). Save to `bench/results/<date>.json`.
3. For each entry in `changing_resources`:
   - find the diffing attribute (plan output in `plan_equivalence_results.checks`, or
     `config_crosscheck.mismatches`)
   - fix it: add read-only discovery of the attribute, or omit it
   - add a fidelity test
4. Fix classification mismatches with classifier rules plus tests. Never with an LLM.
5. Repeat until **no-op ≥ 90% and destroy/replace = 0**. Commit the results (never credentials).
6. Run one full PR flow against the test repo: adoption PR → review in GitHub → guarded merge; then the
   hardening PR.

### Phase E: CEO demo runbook + rehearsal (after D)

1. Write `docs/demo/runbook.md`:
   - setup checklist: containers, LocalStack not needed, sandbox role, GitHub repo, tokens ready,
     Ollama warmed up
   - a click-by-click script matching section 1's demo
   - expected numbers from the last bench run
   - fallbacks: AWS throttling → re-run, fewer filters; Ollama slow → deterministic paths still work;
     GitHub down → ZIP download
2. Two full rehearsals on the sandbox. Record timings in the runbook, fix anything rough, commit.

### Phase F: Change Request PRs (ONLY when I explicitly ask)

Per `docs/design/phase2-change-requests/README.md`:
1. The IaC agent makes deterministic edits of `analyzed_intent.requested_changes` to the *adopted* HCL.
   Use the LLM only to resolve ambiguous targets. Record every edited attribute.
2. Verification plans against the adoption baseline. Every change must be an in-place `update`; a
   replace or destroy is destructive and goes through approval. Estimate the Infracost delta.
3. Delivery opens a new PR kind, `change_request`, stacked on the merged adoption.
4. UI: replace `ChangeRequestNotice` with the real diff/plan/cost review screen. The layout reference
   is `docs/design/phase2-change-requests/ModifyInfrastructureView.tsx.txt`.

The same safety rules apply: TerraAgent never applies.

---

## 8. Definition of done (every phase)

- [ ] Host tests green; container tests green (LocalStack e2e **passed**, not skipped) if terraform,
      discovery, graph or GitHub code changed.
- [ ] `tsc` + `eslint` clean if the frontend changed; `lib/types.ts` matches the backend models.
- [ ] Tests for every new behavior, including negative/safety tests.
- [ ] `CLAUDE.md` updated (architecture, rules, environment variables, endpoints); `docs/aws/*` updated
      for any AWS permission change.
- [ ] No secrets anywhere. `.env`, `frontend/tsconfig.tsbuildinfo` and `frontend/next-env.d.ts` not in
      the commit.
- [ ] One focused commit per phase on `feat/four-agent-restructure`. Never push without asking.
- [ ] Report to me: what changed, what was verified (test counts), what wasn't, and the next phase.
