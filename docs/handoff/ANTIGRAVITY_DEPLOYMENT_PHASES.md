# TerraAgent deployment mode: prompts for Phases 3–6 (Antigravity)

**How to use.** Run one phase per session, in order. Paste **Part A (shared preamble)** followed by
**one** phase prompt from Part C. Each phase ends by stopping and reporting; don't start the next
phase in the same session.

**Before pasting**, fill in the decision table in Part B. A phase whose decisions are still
`PENDING` must not be started. The prompts tell the agent to stop and ask in that case.

State of the tree when this was written (2026-10-02): Phases 1–2 are implemented and tested. Phase 3
is **partly** implemented and uncommitted (see the Phase 3 prompt for exactly what exists and what's
broken). Nothing is committed yet.

---

## Part A — Shared preamble (paste first, every session)

```text
# Role
You are a senior engineer continuing TerraAgent's deployment mode ("Code -> AWS") in this
workspace: C:\Users\USER\Desktop\AIKART\Terraform Agent (Windows host; backend is Python 3.11+
FastAPI, frontend is Next.js 16 - read frontend/AGENTS.md before frontend work, its APIs differ
from older Next.js).

# Read before editing anything
1. CLAUDE.md (repo root) - authoritative. Especially "Hard Safety Rules", "Terraform Execution
   Guardrail" and "Deployment Mode (Code -> AWS)".
2. docs/design/code-to-aws-deployment.md - the design. Sections: §1 decisions, §3.1 state machine,
   §4 work breakdown, §6.4 apply_runner, §7 IAM/STS, §8 Celery, §9 security, §10 tests. The
   "Implementation status" table at the top records where the code deliberately differs.
3. docs/aws/deploy-roles.md and backend/deploy/bootstrap/ (customer IAM setup).
4. Run: git status; git log --oneline -8; and the test commands below.
Then report what you found in 5-10 lines and WAIT for my go-ahead.

# Ground rules
- Ask me first before: adding a dependency; changing the Dockerfile or docker-compose; a refactor
  touching more than ~5 files outside backend/deploy/; anything touching a safety rule or CLAUDE.md
  hard rules; any product decision not spelled out in the phase prompt.
- Never: push, force-push, rewrite history, delete branches, touch or print .env, store/log/print
  AWS keys, session tokens or GitHub tokens, or send them to an LLM.
- Every behaviour change gets a test; every safety property gets a test that fails if it is broken.
- Deterministic code for every security decision. Do NOT add LLM calls or new LangGraph agents to
  deployment mode.
- Migration mode (backend/agents/, backend/tools/ except where a phase says otherwise,
  routers/scan.py) must stay unchanged in behaviour. agents/, tools/ and routers/scan.py must never
  import `deploy` (tests/test_deploy_guardrails.py enforces this).
- tools/terraform_runner.py::check_argv must never be widened. Every terraform/tofu call goes
  through TerraformRunner.run_command, except the single apply path Phase 4 creates (only if the
  owner approved decision D1).
- Status changes only through deploy/store.py::transition (it validates TRANSITIONS and writes a
  deployment_events row). Celery tasks take a deployment id ONLY - never credentials, tokens or
  source content.
- When done: run the checks below, update the "Implementation status" table in
  docs/design/code-to-aws-deployment.md and the "Deployment Mode" section of CLAUDE.md to match the
  code, then commit on the current branch (message: "Deploy Phase N: <summary>") and tell me
  exactly what was verified and what was not. Do not claim something works if you did not run it.

# Checks (run from backend/ unless noted)
- python -m pytest tests/test_deploy_*.py -q
- python -m pytest -q -m "not integration"            (full suite; currently 469 + deploy tests)
- ruff check deploy routers models services tests/test_deploy_*.py --output-format concise
  (the repo has ~69 pre-existing ruff errors elsewhere - do not fix unrelated ones; just add none)
- mypy --python-version 3.12 deploy routers/deployments.py routers/aws_targets.py models
  (--python-version 3.12 works around a numpy-stub error on this host; CI uses 3.11)
- Template validation (downloads the AWS provider):
  set TERRAAGENT_TEST_TERRAFORM=1 then python -m pytest tests/test_deploy_build_and_render.py -k terraform
- frontend/: npm run typecheck; npx eslint app components lib
  (components/ScanProgress.tsx has one pre-existing react-hooks error - leave it)
- Restore frontend/tsconfig.tsbuildinfo with git checkout if tsc touched it.

# Host quirks
- zipfile on Windows rewrites "\" to "/" and truncates names at NUL when WRITING archives, so tests
  that need such names must call normalize_entry_name directly.
- Use the in-memory SQLite pattern of tests/test_deploy_flow.py (call route functions directly,
  disable `limiter`), not TestClient, so the DB stays on one thread.
```

---

## Part B — Owner decisions (fill in before running a phase)

| ID | Decision | Proposed default | Owner answer | Gates |
|---|---|---|---|---|
| D1 | Amend CLAUDE.md hard rules #1 and #3 for deployment mode only (exact wording: design doc Appendix A) | Approve | **PENDING** | Phase 4A |
| D2 | Executor: TerraAgent applies with STS (4A), or opens a PR that the team's OIDC CI applies (4B) | Both behind one `Executor` interface; ship 4A first | **PENDING** | Phase 4 |
| D3 | Approver identity | oauth2-proxy (SSO) in front of nginx; trusted `X-Forwarded-Email` only when `TERRAAGENT_TRUSTED_PROXY_AUTH=true`; four-eyes optional via `TERRAAGENT_DEPLOY_FOUR_EYES=true` | **PENDING** | Phase 3 approval |
| D4 | Worker platform identity | Workers use their own task/instance role (ambient boto3 credentials) to assume customer roles with ExternalId; customers enter no keys | **PENDING** | Phase 3 plan |
| D5 | Terraform version / state locking | Upgrade backend/Dockerfile Terraform 1.8.5 -> 1.10+ and use `use_lockfile = true` | **PENDING** | Phase 4 |
| D6 | Artifact location | Platform ArtifactStore: local (self-hosted) / platform S3 + SSE-KMS | **PENDING** | Phase 6 |
| D7 | Per-deployment limits | 50 resources, $200/month estimate | **PENDING** | Phase 3 plan policy |
| D8 | Node.js in the worker image (declined once) | Add pinned Node 20 tarball install | **PENDING** | Phase 6 |

To accept every default, replace each `PENDING` with `default`.

---

## Part C — Phase prompts

### Phase 3 — Finish: deploy targets, plan, approval gate, identity

```text
# Phase 3: finish what is already started, then stop. Requires D3, D4, D7 (and D2 only for UI copy).
If any of D3, D4, D7 is PENDING in docs/handoff/ANTIGRAVITY_DEPLOYMENT_PHASES.md Part B, stop and ask.

## Goal
A deployment can go VERIFIED -> PLANNING -> AWAITING_APPROVAL against a real AWS sandbox account
using ONLY the read-only plan role, and a named human can approve or reject it. Nothing is applied.
APPROVED is a terminal state in this phase.

## Already in the tree (uncommitted) - audit it first, do not rewrite blindly
Another session was editing these files while this prompt was written (a deploy.plan task, plan/
approval request models and router changes appeared during writing). Treat both lists below as a
CHECKLIST: for every item, check it exists, matches the spec here and in the design doc, and has
tests; implement or fix only what is missing or wrong. Report the audit result before coding.
- models/orm.py: Deployment gained Phase 3 columns; AwsDeployTarget table.
- models/target.py, routers/aws_targets.py (POST/GET/DELETE, POST /{id}/verify), registered in main.py.
- services/identity.py: current_user / require_authenticated_user from X-Forwarded-Email/-User.
- tools/sts_helper.py: assume_role gained source_identity, tags, policy, duration; no keys -> ambient
  boto3 session (D4 default).
- deploy/sts.py: plan_session (900s, SourceIdentity terraagent-system) / apply_session (3600s).
- tools/terraform_runner.py: TerraformRunner.plan_saved (init with -backend-config, plan
  -out=tfplan -lock=false, show -json; all via run_command).
- deploy/plan_bundle.py, deploy/plan_policy.py, security/policies/deploy/plan_rules.rego,
  deploy/bootstrap/ (cloudformation.yaml + terraform/), docs/aws/deploy-roles.md.
- deploy/store.py: DeployStatus + TRANSITIONS extended to PLANNING/AWAITING_APPROVAL/APPROVED/
  REJECTED/EXPIRED.

## Known defects to fix first (each needs a regression test)
1. tests/test_deploy_guardrails.py::test_deploy_has_no_apply_destroy_or_import_strings FAILS:
   deploy/plan_policy.py legitimately uses "destroy" as a plan-action label. Do NOT delete or skip
   the test. Narrow it to what it protects: argv shapes - any list/tuple literal (or call argument
   list) in backend/deploy/ that contains "terraform"/"tofu"/a binary variable together with
   "apply"/"destroy"/"import"/"-destroy"/"-auto-approve". Keep a plain-string check for
   "-auto-approve". Prove the narrowed test still fails on a planted argv like
   ["terraform", "apply"] in a temp module.
2. The templates (deploy/templates/*/versions.tf) have NO `backend "s3" {}` block, so plan_saved's
   -backend-config flags are ignored and the plan uses empty LOCAL state. Add an empty partial
   `backend "s3" {}` to both templates, and make Phase 1-2 validation keep using
   `init -backend=false` (verify validate_hcl still passes). Plan must init against the target's
   state bucket. Test: rendered templates contain the backend block; plan_saved argv contains the
   three -backend-config values and never "-backend=false".
3. tools/sts_helper.assume_role clamps DurationSeconds up to 43200. Design §7.3 caps sessions at
   3600: clamp to [900, 3600]. Test it.
4. TerraformRunner.plan_saved returns "raw_plan_json" (unredacted). It must never be persisted,
   logged or returned by the API. Either drop it from the return value or ensure only the plan
   stage reads it in memory; add a test that the stored deployment record and the API response
   contain no raw plan. Redaction must also drop values Terraform marks sensitive
   (after_sensitive/before_sensitive) - CredentialScrubber alone is not enough.
5. deploy/plan_bundle.extract_plan_bundle must reuse deploy/source_intake's safe extraction rules
   (no traversal/symlinks/absolute paths) even though TerraAgent wrote the bundle.
6. models/deployment.py::ApprovalRequest.confirm defaults to True, which makes the explicit
   confirmation meaningless. It must default to False and the endpoint must reject confirm != true
   (422). Same for any future deploy/merge confirmation field. Test it.
7. Plan bundles are sensitive: encrypt them at rest (task 3.5). Ask me before adding a dependency;
   if one is needed, propose `cryptography` (Fernet) with the key from env TERRAAGENT_ARTIFACT_KEY,
   and fail closed (refuse to plan) when the key is missing.

## Remaining work
- Pipeline stage `run_plan(deployment_id)` in deploy/pipeline.py + Celery task `deploy.plan`
  (queue deploy_plan, id-only): VERIFIED -> PLANNING; extract the stored build bundle into a
  sandbox; plan_session(); plan_saved(); evaluate_plan_policy() (resource-type allowlist per target,
  terraagent:deployment-id tag on every created resource, no "Action":"*"/iam:*/Principal "*"
  except the exact CloudFront OAC bucket-policy pattern, D7 resource + cost limits, delete/replace
  => destructive); store plan_summary, redacted plan JSON, policy result, encrypted bundle +
  plan_bundle_sha256; -> AWAITING_APPROVAL, or FAILED with the policy violations listed.
  Credentials live only in local variables and the subprocess env dict.
- API (routers/deployments.py):
  POST /deployments/{id}/plan {target_id}   (status must be VERIFIED; target must be verified)
  POST /deployments/{id}/approve {plan_bundle_sha256, confirm: true, acknowledge_destructive?, reason}
     -> refuse on hash mismatch (409), expired (>24h since plan, 409), missing identity (401),
        requester == approver when four-eyes is on (403), destructive without acknowledgement (422).
        Records approved_by/approved_at/approval_reason. Does NOTHING else - no apply.
  POST /deployments/{id}/reject {reason}
  Identity comes from services/identity.py; trust proxy headers only when
  TERRAAGENT_TRUSTED_PROXY_AUTH=true (otherwise header spoofing = 401). requested_by is filled on
  create when an identity exists.
- Sweep (deploy/tasks.py::sweep_task): AWAITING_APPROVAL older than 24h -> EXPIRED and delete its
  plan bundle artifact.
- Frontend: Settings -> "Deploy targets" (create with role ARNs/region/state bucket, show the
  generated ExternalId + bootstrap instructions, Verify button); on /deployments/[id]: "Plan" button
  (target picker) when VERIFIED; plan review (per-address actions table, destructive rows
  highlighted, IAM summary, cost, findings, policy violations) and an approve dialog that displays
  the plan hash, requires typing/checking confirmation and a reason; reject button. Extend
  lib/types.ts + lib/api.ts with strict types (no `any`). StatusBadge entries for the new states.

## Tests (new file tests/test_deploy_phase3.py, plus extensions)
- moto: plan_session/apply_session send ExternalId, SourceIdentity, Tags, Policy, Duration<=3600.
- plan_saved argv: only check_argv-approved commands; init has the 3 backend-config flags; plan has
  -lock=false; no "apply"/"destroy"/"-destroy" anywhere (mock run_command, capture argv).
- plan policy on recorded plan JSON fixtures: valid static_site plan passes; foreign resource type,
  untagged resource, wildcard IAM action, Principal "*", over-limit count, replace/destroy each
  produce the right violation/destructive flag.
- approve: every refusal case above; success records identity; status APPROVED; no subprocess runs.
- state machine: illegal transitions rejected; events written for each step.
- Celery: deploy.plan signature is (deployment_id) only.
- Redaction: sensitive plan values and raw plan never reach DB/API.
- Guardrail suite still green; check_argv allowlist unchanged.

## Exit / stop
All checks pass; then STOP and report: what was verified locally, what still needs a real sandbox
account (an end-to-end plan against real AWS cannot be run from tests - tell me the exact manual
steps to do it with the bootstrap template), and any decision you had to assume.
```

### Phase 4A — Apply with STS (only if D1 = approved and D2 includes the STS executor)

```text
# Phase 4A: the single apply path. Requires D1 approved, D2 including STS apply, D5 decided.
If D1 is not "approved" in Part B, STOP - do not write any code that can run terraform apply.

## Goal
APPROVED -> APPLYING -> DEPLOYED | FAILED_PARTIAL | NEEDS_RECONCILIATION, for static_site and
lambda_http, behind the feature flag, through exactly one apply path.

## Step 0 - the rule change (first commit of this phase, nothing else in it)
Replace CLAUDE.md hard rules 1 and 3 with the exact text of Appendix A in
docs/design/code-to-aws-deployment.md, and add the apply path to the "Deployment Mode" section.
Show me the diff and wait for my OK before continuing.

## Work
1. backend/deploy/apply_runner.py - the ONLY module that may run `apply`.
   - check_apply_argv(cmd, plan_kind): binary terraform/tofu; argv must EQUAL
     [binary, "apply", "-input=false", "-lock-timeout=5m", "-no-color", "tfplan"]. Nothing else -
     no -auto-approve, -target, -replace, -var*, -destroy, destroy, import, extra args.
   - apply_approved(deployment_id) (design §6.4): refuse unless env TERRAAGENT_DEPLOY_ENABLED=="true"
     AND the current Celery task's delivery_info routing_key == "deploy_apply"; load the deployment
     with a row lock; require APPROVED, approval not expired, approved_by set; transition APPLYING
     (sets a lease); fetch + decrypt the plan bundle; recompute sha256 == plan_bundle_sha256 or
     refuse; extract into create_sandbox(); apply_session(approver as SourceIdentity, session
     policy limited to terraagent-<deployment_id>-*); TerraformRunner.run_command(init ...) through
     check_argv; then _run_apply(APPLY_ARGV) with _scoped_aws_env (TF_LOG unset), timeout
     TERRAAGENT_TF_APPLY_TIMEOUT (default 2400s), output scrubbed and streamed to publish_log;
     outputs via `show -json` through check_argv into outputs_json; transition DEPLOYED or
     FAILED_PARTIAL; delete the plan bundle; release_sandbox in finally.
   - check_argv in tools/terraform_runner.py is NOT modified.
2. Templates: now that state locking matters, set use_lockfile per D5 (S3 native locking needs
   Terraform >= 1.10; if D5 says stay on 1.8.5, use a DynamoDB table from the bootstrap instead).
   Add a committed .terraform.lock.hcl per template (generate with `terraform providers lock
   -platform=linux_amd64 -platform=windows_amd64 -platform=darwin_arm64`; ask before running if it
   needs network you don't have).
3. Celery: task deploy.apply(deployment_id) routed to queue deploy_apply; acks_late=False,
   max_retries=0, soft_time_limit = apply timeout + 120. Redis lease
   `deploy:lease:<target_id>` (SET NX EX) - one apply per target account. If the task starts and
   finds status APPLYING -> transition NEEDS_RECONCILIATION and do NOT apply.
   sweep_task: APPLYING past the lease -> NEEDS_RECONCILIATION (never FAILED).
4. docker-compose.yml (ask before editing): new service terraagent-deployer, same image, command
   `celery -A services.celery_app worker -Q deploy_apply --concurrency 1 --loglevel=info`,
   env TERRAAGENT_DEPLOY_ENABLED=true, non-root, no published ports. The existing worker keeps
   `-Q celery,deploy_plan` and must NOT get the flag.
5. API: POST /deployments/{id}/deploy {confirm: true} (identity required; status APPROVED) ->
   dispatch deploy.apply. The approval itself still never applies.
6. Frontend: Deploy button after approval (with confirmation), live logs, outputs (URL link),
   NEEDS_RECONCILIATION runbook panel (link docs/runbooks/deploy-reconciliation.md - write it:
   check the state lock holder, re-plan with the plan role, clear a stale lock by hand; TerraAgent
   never automates force-unlock).

## Tests (tests/test_deploy_apply.py) - all must exist and pass
- check_apply_argv rejects each forbidden extra/variant; accepts only the exact argv.
- apply_approved refuses: flag off; wrong queue; status != APPROVED; expired approval; missing
  approver; bundle hash mismatch (flip one byte). In each refusal no subprocess is started
  (mock create_subprocess_exec and assert not called).
- Import boundary: AST scan - only deploy/tasks.py imports deploy.apply_runner; nothing in agents/,
  tools/, routers/scan.py imports deploy.*.
- check_argv allowlist unchanged (existing pinned test).
- Redelivery: task finding APPLYING -> NEEDS_RECONCILIATION, apply not called.
- Lease: second concurrent apply on the same target refused.
- Credentials: never in Celery args, DB rows, logs (capture publish_log), or exceptions.
- LocalStack integration (mark integration): render -> plan -> apply lambda_http and the S3 part of
  static_site against LocalStack (CloudFront is not in LocalStack Community).

## Exit / stop
Report what ran locally vs what needs the real sandbox account. The design doc flags one item to
verify in the first real deployment: whether public Lambda function URLs now also need a
lambda:InvokeFunction grant in addition to lambda:InvokeFunctionUrl. Give me the manual
end-to-end steps (bootstrap -> target -> plan -> approve -> deploy -> open URL).
```

### Phase 4B — GitOps PR executor (if D2 = PR route; no D1 needed)

```text
# Phase 4B: deliver an approved deployment as a GitHub PR; the team's CI applies it.
TerraAgent never applies in this variant, so hard rules stay unchanged. Requires D2 = PR route.

## Work
- deploy/executor.py: Protocol Executor.execute(deployment_id) -> ExecutionResult; GitOpsPrExecutor.
- Reuse services/github_client.py::_publish (atomic commit -> branch -> PR, pre-flight push check,
  token used once, never stored). Commit the rendered project under a configurable target dir
  (default terraform/), including terraform.tfvars.json and the built site/ or artifacts/ files.
  Binary files (images, function.zip) need base64 blobs - extend _publish to accept bytes without
  changing the existing adoption/hardening call sites' behaviour (add tests for both).
- Optional (checkbox, default off): add .github/workflows/terraagent-plan.yml and
  terraagent-apply.yml using aws-actions/configure-aws-credentials OIDC with the target's apply
  role ARN; apply on push to the base branch with environment: production; refuse to add workflows
  when the project has no remote backend configured (CI apply with local state would recreate
  everything each run). Note: committing workflow files needs the token's "workflow" permission -
  diagnose that error clearly.
- PR body: plan summary, policy result, security posture, cost, approver identity and plan hash.
- Endpoints: POST /deployments/{id}/pull-request (APPROVED only, identity required), GET status,
  POST merge with the same gates as routers/scan.py::merge_pull_request (confirm, open+mergeable,
  approved in GitHub, no changes requested) - reuse _approved_in_github.
- New states: APPROVED -> PR_OPEN -> MERGED (CI applies; TerraAgent only reports workflow runs).
- Tests: token never persisted/logged; binary files round-trip; merge gates; no terraform apply
  anywhere (guardrail suite unchanged).
```

### Phase 5 — Rollback, teardown, ECS/Fargate

```text
# Phase 5. Requires Phase 4A (or 4B) merged. Teardown and ECS change AWS: they go through the
# same plan -> approval -> apply path, never around it.

## 5.1 Application rollback (no new resource types)
- lambda_http: publish_version = true + an aws_lambda_alias "live"; the function URL targets the
  alias (qualifier). Keep the last 3 build bundles (deployment_artifacts). Rollback = re-render
  with a previous build's package -> plan -> approval -> apply. Show "roll back to build N".
- static_site: upload each build under a versioned prefix (releases/<build_sha>/) and point
  CloudFront origin_path at it; rollback switches origin_path. Old prefixes are kept (last 3) and
  pruned via lifecycle rule in the template.
- Tests: rendered tfvars for a rollback reference the previous artifact; the plan shows only the
  alias/origin_path update (fixture plan JSON); policy treats it as non-destructive.

## 5.2 Teardown
- check_argv blocks "-destroy" everywhere, so a destroy plan cannot go through run_command. Add a
  separate, narrowly validated `plan_destroy` in deploy/apply_runner.py (argv must equal
  [binary, "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"]), plan_kind =
  "destroy" stored on the deployment, mandatory acknowledge_destructive at approval, and
  check_apply_argv accepting "tfplan.destroy" ONLY when plan_kind == "destroy". Scope: this
  deployment's own state key only. Ask me before implementing - this is a safety-rule extension.
- States: DEPLOYED -> DESTROY_PLANNING -> AWAITING_APPROVAL -> APPROVED -> DESTROYING -> DESTROYED.
- Tests: plan_destroy refuses any other argv; apply of a destroy plan refused without plan_kind;
  migration-mode check_argv still blocks -destroy.

## 5.3 ECS Fargate + ALB (container target)
- decision_engine: ecs_service eligible when a Dockerfile exists or a server entrypoint listens on
  a port (bump DECISION_RULES_VERSION; update tests).
- Image build runs in the CUSTOMER account via CodeBuild (a Dockerfile is untrusted code; it never
  runs on TerraAgent infrastructure). Template ecs_service/: ECR repo (scan on push, immutable
  tags), CodeBuild project (source = the build bundle uploaded to a per-deployment S3 object),
  ECS cluster/task definition/service, ALB with HTTPS listener only if a certificate ARN is given
  (else HTTP with a clear warning), log group, task + execution roles under /terraagent/ with the
  permissions boundary, security groups allowing only ALB -> task port.
- Starting the CodeBuild run (codebuild:StartBuild) is an AWS mutation: it happens only inside the
  approved apply step with the apply role, never during plan. Extend the bootstrap apply-role policy
  and docs/aws/deploy-roles.md minimally; plan policy allowlist per target.
- Tests: decision rules, template validate, plan-policy fixtures, apply-role policy diff reviewed.
```

### Phase 6 — Hardening

```text
# Phase 6. Each item is independent; ask before starting the ones marked (ask).

1. S3ArtifactStore (D6): put/get/delete with SSE-KMS, private bucket, lifecycle for expiries;
   default outside dev; same interface as deploy/artifacts.py::ArtifactStore. Tests with moto.
2. (ask - touches migration mode) Remove raw AWS keys from Celery args in routers/scan.py::start_scan
   / services/celery_app.run_scan_task: today they sit in the Redis broker. Pass a short-lived,
   single-use reference instead (e.g. encrypted blob in Redis with TTL, key from env) and delete it
   on read. Test: captured broker message contains no credential.
3. Per-tenant ownership: deployments and aws_deploy_targets get owner/tenant columns from
   services/identity; every read/write route checks them (404 on other tenants' ids). Tests.
4. Builder isolation (design §9.2): move deploy/builder.py's pip/npm runs into a separate
   terraagent-builder container (rootless, read-only rootfs, tmpfs workdir, CPU/mem/pid limits,
   no AWS env, no Docker socket, egress only to PyPI/npm via a proxy allowlist) - e.g. a Celery
   queue deploy_build consumed only by that container on its own network with Redis. (ask)
5. (ask - D8) Node.js in the worker/builder image so Node Lambdas with dependencies build in
   containers.
6. Scheduled end-to-end suite against the sandbox account (static site + Lambda: deploy, redeploy,
   reject, expire, forced worker kill -> NEEDS_RECONCILIATION), results to the Terraform Runs page.
7. Replace Infracost-skipped "Not estimated" with a clear setup hint in Settings when
   INFRACOST_API_KEY is missing.
```

---

## Part D — Order and dependencies

```
Phase 3 (finish) ──► Phase 4A (needs D1) ─┬─► Phase 5 ─► Phase 6
                 └─► Phase 4B (needs D2) ─┘
```

Phase 6 items 1, 2, 3 and 7 can run any time after Phase 3. Item 4 must land before deployment mode
is offered to untrusted users.
