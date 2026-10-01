aok# TerraAgent: implementation plan for the remaining work

Status as of 2026-09-26. Branch `feat/four-agent-restructure`, nothing pushed.
Committed: Weeks 2–6 (last commit `103cdc7`). Everything below is either uncommitted or not started.

Every phase follows the rules in `CLAUDE.md` and the definition of done in
`docs/handoff/ANTIGRAVITY_PROMPT.md` §9.

---

## 0. Coordination rule (do this first)

Two agents edited this branch at the same time today: Claude (Week 7) and a second session (PR
lifecycle and new pages). Three files hold both sets of changes:
- `backend/routers/scan.py`
- `backend/models/scan.py`
- `frontend/lib/api.ts`

**From now on, one agent works on the branch at a time.** The agent that holds the branch finishes,
commits, and hands over. Before starting, always run `git status` and check file modification times.
If files changed that you didn't touch, stop and ask.

---

## Phase 1: Stabilize and split the uncommitted work

**Goal:** turn today's mixed working tree into clean, reviewable commits without losing anything.

| Step | Action | Files |
|---|---|---|
| 1.1 | Confirm the second session has stopped editing (no file saved for 10+ minutes) | - |
| 1.2 | Run the host tests, `tsc`, `eslint`. Record the results | - |
| 1.3 | Commit **Week 7** alone, staging only its hunks (`git add -p` for the three mixed files) | see list A |
| 1.4 | Leave the PR-lifecycle and pages work uncommitted until Phase 2 has reviewed it | see list B |

**List A (Week 7: access hardening, incomplete discovery, parallel scans, runner allowlist):**
- **Whole files:**
  - `CLAUDE.md`
  - `backend/Dockerfile`
  - `backend/agents/cloud_discovery.py`, `backend/agents/graph.py`, `backend/agents/resource_explorer_step.py`
  - `backend/services/pipeline.py`
  - `backend/tools/aws_scanner.py`, `backend/tools/sts_helper.py`, `backend/tools/terraform_runner.py`
  - `backend/tests/test_cloud_discovery.py`, `backend/tests/test_terraform_runner_plan.py`, `backend/tests/test_access_and_discovery.py`
  - `docs/aws/`, `scripts/bench_adoption.py`
  - `frontend/components/CredentialForm.tsx`
  - `docs/handoff/ANTIGRAVITY_PROMPT.md`, `docs/plans/remaining-work.md`
- **Hunks only:**
  - `backend/models/scan.py`: the `external_id` field
  - `backend/routers/scan.py`: `"external_id"` in `request_dict`
  - `frontend/lib/api.ts`: `role_arn` / `external_id` in `ScanRequestPayload`

**List B (second session, reviewed in Phase 2):**
- **Backend:**
  - `backend/services/github_client.py`: `get_pr_details`, `get_pr_diff`, `approve_pr`, `merge_pr`, `get_workflow_runs`
  - `backend/routers/scan.py`: `GET/POST /pull-request`, `/pull-request/approve`, `/pull-request/merge`
  - `backend/models/scan.py` and `backend/models/job.py`: the PR models
  - `backend/tests/test_pull_request_endpoint.py`
- **Frontend:**
  - `frontend/components/GitHubPrViewer.tsx`, `frontend/app/results/[id]/pr/`
  - `frontend/components/PendingApprovalPanel.tsx`, `frontend/components/CreatePullRequestAction.tsx`, `frontend/components/Sidebar.tsx`
  - `frontend/app/results/[id]/page.tsx`, `frontend/lib/types.ts`, `frontend/lib/api.ts` (its non-Week 7 hunks)
  - `frontend/app/{requests,runs,pull-requests,settings}/`
  - `frontend/components/ModifyInfrastructureView.tsx`

**Acceptance criteria:**
- The Week 7 commit contains only list A.
- After the commit, `git stash` of list B leaves a tree where the host tests, `tsc` and `eslint` all pass. Then run `git stash pop`.

---

## Phase 2: Review and harden the PR-lifecycle work (list B)

**Goal:** keep what's real, make it safe, remove mocks from production screens.

### 2.1 Decisions needed from the user (ask before implementing)

| # | Question | Recommendation |
|---|---|---|
| D1 | Should TerraAgent be able to **merge** PRs at all? If the team's Atlantis or HCP Terraform setup applies on merge, a merge button in TerraAgent is an indirect way to trigger an apply | **Keep it, gated:** only the PR TerraAgent opened for this job, only after GitHub reports it mergeable and approved, with an explicit typed confirmation and a warning about auto-apply. Otherwise drop merge and link to GitHub |
| D2 | Approving PRs from TerraAgent: the same account that opened the PR would approve it | **Drop approve:** GitHub rules usually block self-approval, and it weakens four-eyes review. Link to the PR instead |
| D3 | `/requests`, `/pull-requests`, `/runs`, `/settings` show hardcoded data | **Make `/pull-requests` real** (it lists `github_pr` / `github_hardening_pr` across jobs). **Remove** the other three from the sidebar until they have real data |
| D4 | `ModifyInfrastructureView` is back as a tab on every results page | **Show it only when `operation === "modify"`**, labeled Phase 2 preview. Otherwise remove it (Phase 7 builds the real one) |

### 2.2 Safety fixes (required whatever the decisions above)

| Step | Fix | Test |
|---|---|---|
| 2.2.1 | `merge`/`approve`/`GET` must act only on **this job's own PR**: repo + number have to match `state.github_pr` or `state.github_hardening_pr`, otherwise 403. Right now the body's `repo`/`pr_number` can target any PR the token can reach | Endpoint test with a foreign repo/PR, expect 403 |
| 2.2.2 | **Merge order:** the Hardening PR can't merge before the adoption PR is merged | Test, expect 409 |
| 2.2.3 | The GitHub token in the `X-GitHub-Token` header must never be logged. Check access logs and the rate-limiter key (for example, slowapi may log headers). Keep the token in the JSON body (`SecretStr`) for POST endpoints | Test that the token never appears in the response, Redis state or a captured log |
| 2.2.4 | `create_adoption_pr` returns no `repo` field, but `GET /pull-request` expects `github_pr.repo`. Add `repo` to both PR create functions and to the persisted state | Test that GET works without query params after a create |
| 2.2.5 | GitHub API errors go through `_handle_api_error` and `CredentialScrubber` before reaching the client | Existing pattern, extend the tests |
| 2.2.6 | Discovered and PR text shown in `GitHubPrViewer` (diff, body) is rendered as text, never with `dangerouslySetInnerHTML` | grep check + review |
| 2.2.7 | `CLAUDE.md`: document the PR-lifecycle endpoints and the D1/D2 decisions | - |

### 2.3 Wiring and cleanup
1. The `PendingApprovalPanel` rework ("set all" buttons) must keep the rules: approve stays disabled until every Review resource is decided, and unsupported types offer only "exclude". Check against `approval_request.review_resources[].choices`.
2. `lib/types.ts` must match the new backend models field for field.
3. Commit: `PR lifecycle: in-app PR status, diff and gated merge; real PR list`.

**Acceptance criteria:**
- Host tests pass, including the new 403/409/no-token-leak tests.
- `tsc` and `eslint` pass.
- No hardcoded demo data reachable from the sidebar or the results page (except the labeled Phase 2 preview, if kept).

---

## Phase 3: Verify in the containers and deploy

| Step | Action |
|---|---|
| 3.1 | `docker compose build terraagent-api terraagent-celery terraagent-frontend`. The build log must not contain `plugin cache dir ... cannot be opened`, and must not install tfsec |
| 3.2 | `docker compose up -d --no-deps terraagent-api terraagent-celery terraagent-frontend && docker restart terraagent-nginx` |
| 3.3 | Inside `terraagent-celery`, run `tests/test_langgraph_pipeline.py`, `tests/test_access_and_discovery.py` and `tests/test_terraform_runner_plan.py` (with `LOCALSTACK_URL=http://terraagent-localstack:4566`) |
| 3.4 | UI smoke test at http://localhost: start a scan; the 4 agent cards move; the approval panel, results scores, "Pull request & bundle" tab and PR viewer all work; no mock pages |
| 3.5 | Fix anything found, re-run, then commit the fixes separately |

**Acceptance criteria:** the container tests pass, the UI smoke test passes, and the deployed containers match `HEAD`.

---

## Phase 4: Sandbox test bench (blocked on the user)

**Needs from the user:**
- a sandbox AWS account with realistic resources: VPC, subnets, route tables, security groups, EC2, S3, RDS, a few IAM roles
- a read-only role created per `docs/aws/read-only-role.md`, with its role ARN and ExternalId
- a hand-labeled answer key
- a GitHub test repo and a token

| Step | Action | Files |
|---|---|---|
| 4.1 | Confirm the role: `aws sts assume-role --role-arn <arn> --role-session-name check --external-id <id>` | - |
| 4.2 | Write the answer key with the user: `{resource_id: manage/reference/exclude/review}` | `bench/answer_key.json` |
| 4.3 | Run `python scripts/bench_adoption.py --region <r> --answer-key bench/answer_key.json --out bench/results/<date>.json` | `bench/results/` |
| 4.4 | For each resource in `changing_resources`: find the attribute in the plan output or `config_crosscheck.mismatches`, fix it in `tools/aws_scanner.py` / `tools/hcl_generator.py`, and add a fidelity test | `tests/test_adoption_fidelity.py` |
| 4.5 | Fix classification mismatches with rules in `tools/resource_classifier.py` plus tests. Never with an LLM | `tests/test_resource_classification.py` |
| 4.6 | Update `docs/aws/*` for any new read-only API, and extend the read-only boto test | `docs/aws/`, `tests/test_cloud_discovery.py` |
| 4.7 | Repeat until the targets are met, then commit the bench results (never credentials) | - |

**Acceptance criteria:** no-op ≥ 90% of managed resources, 0 destroy/replace, classification accuracy recorded, review rate recorded, and scan time recorded.

---

## Phase 5: Generator and delivery gaps

Do the ones the bench exposes first. Each item is its own commit with tests.

| # | Gap | Steps | Acceptance |
|---|---|---|---|
| 5.1 | Route-table subnet associations are left unmanaged | Generate `aws_route_table_association` with import blocks (ID `subnet-id/rtb-id`) in `hcl_generator.generate_project`; drop the warning | Plan shows import, no create |
| 5.2 | IGW, NAT, ALB, DynamoDB, KMS, SQS and SNS have templates but no discovery | For each: read-only discovery in `aws_scanner.py` (with key type / EIP allocation), `DISCOVERY_RESOURCE_TYPES`, `IMPORT_ID_FIELDS`, `SOURCE_API`, moto coverage in the read-only test, `docs/aws` permissions | Discovered, classified, imported, and the read-only test passes |
| 5.3 | PRs commit one file per API call | Move to the Git Data API (blobs → tree → commit → ref). Keep branch cleanup on failure. Update the fake queue in `test_github_pr.py` | One commit per PR, tests pass |
| 5.4 | Legacy `agents/repair_agent.py` is unused | Delete it and its test; `hardening.py` owns the S3 fix | Suite passes |
| 5.5 | `tools/tfsec_runner.py` is still imported in `tools/__init__.py` | Remove the module and its import. Update the `CLAUDE.md` guardrail list | Suite passes |

---

## Phase 6: Week 8, the CEO demo

| Step | Action |
|---|---|
| 6.1 | Dashboard polish (already approved): a "Nothing found" badge on completed 0-resource scans, a latest-scan summary card, and archive/delete for old jobs (soft-delete column on `JobRecord` + `DELETE /api/jobs/{id}` + UI) |
| 6.2 | End-of-run summary line on the progress page: "N found: X managed, Y referenced, Z excluded · Migration Safety N% · zero changes to production · K findings in a separate Hardening PR" |
| 6.3 | Write `docs/demo/runbook.md`: setup checklist, click-by-click script, expected numbers from the last bench run, and fallbacks (AWS throttling → re-run, Ollama slow → deterministic paths, GitHub down → ZIP) |
| 6.4 | Two full rehearsals on the sandbox; record the timings in the runbook |

**Acceptance criteria:** two clean rehearsals, and the numbers on screen match the bench output.

---

## Phase 7: Phase 2 Change Request PRs (only when the user says so)

Follow `docs/design/phase2-change-requests/README.md`:
1. **IaC Engineering:** deterministic edits of `analyzed_intent.requested_changes` to the *adopted* HCL. Use the LLM only to resolve ambiguous targets, and record every edited attribute.
2. **Verification:** plan against the adoption baseline. Every change must be an in-place `update`. A replace or destroy is destructive and goes through approval. Estimate the Infracost delta.
3. **Delivery:** a new `kind: "change_request"` PR, stacked on the merged adoption.
4. **UI:** replace the `ModifyInfrastructureView` mock with real plan, diff and cost data.

The same safety rules apply: TerraAgent never applies.

---

## Order and dependencies

```
Phase 1 -> Phase 2 (needs D1-D4) -> Phase 3 -> Phase 4 (needs sandbox) -> Phase 5 (bench-driven) -> Phase 6
                                                                                 Phase 7 only on request
```

| Phase | Blocked on | Rough size |
|---|---|---|
| 1 | other session idle | small |
| 2 | decisions D1–D4 | medium |
| 3 | - | small |
| 4 | sandbox account, role, answer key | medium, iterative |
| 5 | bench results | medium |
| 6 | Phase 4 numbers | medium |
| 7 | user go-ahead | large |
