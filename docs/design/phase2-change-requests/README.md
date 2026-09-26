# Phase 2: Change Request PRs (parked design)

Status: **parked until the 8-week adoption plan ships** (decided 2026-09-26).

## What it is

An engineer types a natural-language change ("increase the EC2 web server from t2.micro to
t2.medium and scale Fargate from 2 to 4 tasks"). TerraAgent parses it, edits the *already adopted*
Terraform, verifies the change, and opens a **Change Request PR** showing the diff, the plan
("0 to destroy, 2 to update in-place"), policy results and the cost delta.

## Why it waits

- **Adoption first.** You can only edit `instance_type` in `ec2.tf` once an adoption run has
  produced that file with a zero-change plan. Change requests run on top of an adoption baseline.
- **Separate PR type.** An adoption PR must plan with zero changes. A change request is
  behavior-changing by definition, so it is its own PR type (like the Hardening PR). It always goes
  through the approval gate and is never mixed into an adoption PR.
- **Safety rules still hold.** TerraAgent only opens a PR, and the customer's pipeline applies it.
  No `apply`, `destroy` or `import` is ever run (CLAUDE.md rules 1 to 3).
- **tfsec is not coming back.** The source spec lists it, but its rules live in Trivy now.

## What already exists (committed)

- `POST /scan/analyze-intent` (`backend/tools/intent_analyzer.py`, `prompts/intent_analysis.txt`):
  an LLM with a keyword fallback. It extracts the operation, target resources, before/after
  attribute changes, risk and confidence.
- `UserRequestSection.tsx` (region, environment, resource chips, prompt box with templates) and
  `IntentAnalysisModal.tsx` (confirm before the pipeline starts).
- `ScanRequest.user_request` / `analyzed_intent` / `environment`, stored in graph state by
  `intent_router`. Today `modify` and `fix` run the normal adoption pipeline. Nothing downstream
  acts on `requested_changes` yet.

## What has to be built

1. **IaC Engineering:** apply `requested_changes` to the adopted HCL. Deterministic attribute
   edits come first. Use the LLM only for ambiguous targets. Record every edited attribute.
2. **Verification:** plan against the adopted baseline. Every change must be `update` (in-place).
   Any `replace` or `destroy` raises NEEDS_APPROVAL at the destructive tier. Infracost gives the
   delta. Checkov, Trivy and Conftest run as usual.
3. **Delivery:** a Change Request PR. The body holds the per-resource diff, plan counts, cost delta
   and scan results.
4. **UI:** the review screen designed in `ModifyInfrastructureView.tsx.txt`, wired to real
   plan, diff and cost output instead of the hardcoded values.

## UI reference

`ModifyInfrastructureView.tsx.txt` is the original mock (hardcoded ec2.tf / ecs-fargate.tf diff,
`+$27.25/mo`). It is kept as a layout reference only. It was removed from the results page because
it showed a fake diff on every scan.

## Source spec (summary)

- Request box: region (with auto), environment, resource chips (EC2, ECS, VPC, SG, S3, RDS, IAM),
  and quick-fill templates.
- Intent analysis: operation (scan, generate, modify, explain, fix, validate), targets, parameter
  diffs, risk and confidence, then a confirmation modal.
- The same four agents: Infrastructure, IaC Engineering, Verification & Risk, Delivery & Approval.
- Review screen:
  - top bar with status, "Download Bundle" and "Approve & Create PR"
  - 4-step stepper
  - left: per-file HCL diff
  - right: update/destroy counts, scan status, cost delta, and a zero-mutation badge
  - a PR modal with repository and branch
