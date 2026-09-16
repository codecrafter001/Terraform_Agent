# Design: Plan/Drift Boundary, Graph Confidence Semantics, Cost Baseline, and the Terraform Execution Guardrail

Phase 0 design lock for two upcoming features (drift reconciliation, cost-delta reporting) that
change the data shape of two already-shipped agents (`plan_equivalence_agent`, `cost_agent`).
No code changes ship with this document — every "Decision" below is what Phase 1+ implementation
must follow; every "Current state" is grounded against the actual code as of this writing, not
aspirational. See `docs/design/cloud-inventory-and-adoption-planning.md` for the sibling design
doc this one extends (adoption planning, classification, and the dependency graph itself).

---

## Part A — The #7 / #9 boundary: `plan_equivalence_agent` vs. `drift_reconciliation_agent`

### Current state (exact)

`backend/agents/plan_equivalence_agent.py` runs a real `terraform init && plan && show -json`
against live AWS. Verified empirically this session (a real `terraform plan` against a fresh
sandbox describing an already-existing S3 bucket): every resource in a state-less sandbox plan
reports `create`, **never** `replace`/`destroy`/`update`. This isn't a bug to fix — it's
structural. Terraform has no way to know a resource already exists without either a populated
state file or a native `import {}` block binding the address to a real ID, and this codebase can
have neither: `terraform_composer.py` never emits `import {}` blocks, and `CLAUDE.md`'s hard rule
#2 forbids ever running `terraform import` automatically, even into a throwaway sandbox nobody
will ever see. The module's own docstring already documents this:

> "the replace/destroy tallying below is kept for the day this codebase gains real import-block
> support, but it is NOT the thing that actually protects anyone today - treat it as
> defense-in-depth, not the primary signal."

The one signal that *is* real and reachable today: a `plan` (or `show`) step that fails after
`init` succeeds means the AWS provider itself rejected something about the configuration at the
API level — something `terraform validate` (schema-only, no network call) can never catch.

**Known stale artifact**: `backend/models/adoption.py::PlanEquivalenceResult` /
`PlanEquivalenceAction` (fields: `confidence_score`, `actions: List[...Literal["no-op", "create",
"update", "replace", "destroy"]]`, `blocking_actions ... # the replace/destroy subset`) is
imported nowhere except a bare type-hint comment in `agents/graph.py:50`
(`plan_equivalence_results: dict  # PlanEquivalenceResult-shaped, only populated if
run_plan_equivalence`). The real runtime shape (`TerraformRunner.plan_json`'s return dict:
`create`/`update`/`replace`/`destroy`/`no_op`/`blocking_actions`/`checks`/`skipped`) has different
field names and never validates against this model. This Pydantic model is exactly the kind of
copy the "#7" feature description must stop implying — it should be removed or rewritten in
Phase 1, not now.

### Decision

Two agents, two genuinely different mechanisms, never conflated again:

**`plan_equivalence_agent` (existing, scope unchanged)** — "does this generated HCL, run
completely fresh with no state, get *accepted* by the real AWS provider." Proves API-level
correctness beyond what `terraform validate` can reach. **`create`-only is the correct, expected
outcome, not a gap.** A clean run here must never be read as "no drift" or "safe to adopt
as-is" — only "this HCL is syntactically and API-acceptable." Feature copy for #7 (chat spec text,
`github_client.py::_build_pr_body`'s "Plan Equivalence" section, any frontend label) must say
*proves the generated HCL is accepted by AWS*, never *detects replacements or destroys*.

**`drift_reconciliation_agent` (new)** — the actual "does the generated IaC accurately describe
the resource as it really exists" check, and the **only** place a replace/destroy-*equivalent*
finding may come from. Mechanism: a pure Python dict diff, no `terraform` binary, no state, no
plan, no new AWS calls beyond what `cloud_discovery_node` already made:

1. Compare each adopted resource's *discovered* attributes (already in `state["resources"]`, from
   `aws_scanner.py`) against the *same data `terraform_composer.py` consumed* to generate that
   resource's HCL — diffing against the composer's own input snapshot, not re-parsing the HCL
   text it emitted, since HCL re-parsing is fragile and lossy and the input snapshot is exact and
   free (no extra work — it already exists in memory when the composer runs).
2. For every attribute that differs, classify it against a small, explicit, per-resource-type
   table of which Terraform arguments are `ForceNew` for that AWS provider resource — this is
   public, static knowledge from the provider's own schema (e.g. `aws_db_instance.engine`,
   `aws_instance.availability_zone` force replacement; `aws_instance.tags` does not). A `ForceNew`
   mismatch is the real "replace-equivalent" finding; a non-`ForceNew` mismatch is
   "update-equivalent" (in-place fixable, lower severity).
3. Findings route through the *same* machinery already built for plan/repair escalation — same
   `RepairTier` literal, same `pending_approval` shape, same `AWAITING_APPROVAL` halt via
   `graph.py`'s conditional edges. No new approval mechanism.

**Wiring**: after `terraform_composer` (needs its attribute snapshot to diff against) and before
`policy_agent`, so a drift finding can halt the pipeline through the exact same gate
`plan_equivalence_agent` already halts through — this is a placement decision, not yet
implemented; Phase 1 concern.

---

## Part B — Dependency-graph confidence scores (#2)

### Current state (exact)

`backend/tools/graph_builder.py::DependencyGraphBuilder.build_graph()` assigns `confidence` on
exactly two tiers today, both from direct-or-near-direct AWS reference fields:

```python
# vpc_id, subnet_id, security_groups - literal AWS ID cross-reference via has_node lookup
self.graph.add_edge(vpc_id, src_id, relation="contains", confidence=1.0)
self.graph.add_edge(subnet_id, src_id, relation="hosted_in", confidence=1.0)
self.graph.add_edge(sg_id, src_id, relation="secured_by", confidence=1.0)

# IamInstanceProfile.Arn names the PROFILE, not the ROLE - AWS decouples the
# two, though the console almost always names them identically. Deriving the
# role name by string-splitting the ARN and checking whether a role node
# with that name happens to exist is a naming-convention match, not a
# resolved reference - hence < 1.0.
self.graph.add_edge(role_name, src_id, relation="assumes_role", confidence=0.8)
```

No edge is ever emitted below `0.8`. No tag- or naming-match edge exists at all today — the one
candidate for that (an IAM trust-policy service-principal match) is deliberately stored as node
metadata (`trusted_by_service_types`), never turned into a graph edge. There is no Pydantic model
documenting `confidence` — the `links` list is a plain dict, assembled ad hoc at serialization
time (`confidence: d.get("confidence", 1.0)`).

`tools/adoption_planner.py::_local_depths()` (the function `_build_waves` uses to order migration
waves) currently treats **every** edge as equally authoritative for ordering, regardless of
confidence — `low_confidence_ids` (edges with `confidence < 1.0`) is computed separately and used
*only* to flag a wave's `risk_level`/`risk_signals` as medium-risk, never to exclude an edge from
the depth computation itself.

The existing `docs/design/cloud-inventory-and-adoption-planning.md` already proposes a confidence
scale (§"Confidence scores — a simple, defensible formula") but its `0.6` tier for trust-policy
edges is aspirational — the current code never emits that edge at all, consistent with that same
doc's "never below 0.5 — don't emit the edge" principle, just one tier tighter in practice (nothing
below `0.8` exists yet, not `0.5`).

### Decision

`confidence` measures **confidence of edge existence** — does this dependency relationship
actually hold in reality — and explicitly **not** confidence of stack grouping (which stack file a
resource lands in is `graph_builder.py::_compute_stacks`'s separate, already-deterministic logic,
unrelated to per-edge confidence).

Scoring tiers, formalized as the definition going forward:
- **1.0** — direct AWS ID cross-reference: the source field literally names the target's real
  identifier (`vpc_id`, `subnet_id`, security-group membership).
- **0.8** — derived-but-verifiable: the edge requires one deterministic transformation of a real
  AWS field before the comparison (e.g. stripping an instance-profile ARN to a bare name), and
  that transformation is fully deterministic, but AWS's own data model doesn't *guarantee* it's
  correct (a profile name legally can differ from its role's name; it usually doesn't).
- **No edge below 0.8 today.** Per the existing design doc's own principle: a rule uncertain
  enough to need a lower tier shouldn't become a graph edge at all — store it as informational
  node metadata instead (the `trusted_by_service_types` precedent already does this).

**Threshold for #3's wave ordering** (Phase 1+ code change, not shipped by this document):
introduce `MIN_WAVE_ORDERING_CONFIDENCE` (proposed default `0.8` — today's floor, so this is a
no-op until a sub-0.8 tier is ever introduced) below which an edge is excluded from
`_local_depths()`'s ordering computation entirely — still rendered in the dependency graph
visualization, just no longer able to *force* sequencing between two waves. This extends, rather
than replaces, the existing `low_confidence_ids` risk-flagging behavior in `_build_waves`.

---

## Part C — Cost baseline (#5)

### Current state (exact)

Cost estimation in this codebase is **100% Infracost-only**. `backend/tools/infracost_runner.py`
runs `infracost breakdown` against the *generated* Terraform HCL in an isolated sandbox — no AWS
credentials involved, no AWS API calls of any kind. There is no Cost Explorer / billing
integration anywhere in `backend/` (confirmed by exhaustive grep — zero matches for `boto3.client
("ce"`, `GetCostAndUsage`, `CostExplorer`, or `billing`). There is also no documented minimal
read-only IAM policy anywhere in this repository — no README, no `security/` doc, no CLAUDE.md
section lists what IAM actions a scanning principal needs. (`agents/cloud_discovery.py`'s
`"attached_policy_arns": ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"]` is sample/mock
response data returned by the scanner, not a documented recommendation.)

### Decision

"Cost difference" = **real current AWS spend (Cost Explorer) on the resources actually being
adopted, minus Infracost's estimate of the equivalent generated IaC.** Not old-plan-vs-new-plan —
nothing is ever applied in this codebase, so that framing has no referent here.

**Feasibility is explicitly NOT confirmed** — flagged as an open blocker before Phase 3 scoping,
for two independent reasons:

1. **Permissions.** `ce:GetCostAndUsage` (and likely `ce:GetDimensionValues` for filtering) is a
   `Get*`-prefixed API, so it's compliant with CLAUDE.md's read-only-API rule in principle — but
   since no minimal IAM policy is documented anywhere in this repo today, it cannot be assumed a
   user's scan credentials include Cost Explorer access. Any Cost Explorer call must degrade the
   same way every other optional tool in this pipeline already does: `tool_skipped=True` on
   `AccessDenied`, never a silently omitted comparison and never a fabricated `$0` (the same
   honesty convention `infracost_runner.py`/`tfsec_runner.py`/etc. already established).
2. **Granularity mismatch — the harder problem.** Infracost's estimate is *per-resource*.
   `GetCostAndUsage` is natively aggregated by `SERVICE`/`USAGE_TYPE`/`LINKED_ACCOUNT`/tag, **not**
   by individual resource ID, for most services. A true apples-to-apples per-resource cost diff
   needs AWS Cost and Usage Reports (CUR) with resource-ID granularity enabled — a separate,
   materially bigger integration (S3 export + Athena/Glue), and one TerraAgent cannot enable
   itself via a read-only API call; it's an account-level billing configuration decision the
   account owner has to make independently.

Phase 3 must scope to an **account- or service-level aggregate comparison** ("this account's EC2
spend, last 30 days: $X, vs. Infracost's EC2 total for the generated IaC: $Y") and must never
claim per-resource billing accuracy unless CUR is confirmed present for the target account.

---

## Part D — The Terraform execution guardrail, formalized

### Current state (exact)

`CLAUDE.md`'s existing "Hard Safety Rules & Constraints" already ban `apply`/`destroy` (#1) and
automatic `import` (#2), but name no enforcement *mechanism*. The actual mechanism —
`backend/tools/terraform_runner.py::TerraformRunner.run_command`'s disallowed-argv check
(`apply`/`destroy`/`import` substring-matched against every argv token, `ValueError` raised
before the subprocess ever starts) — is real, but investigation for this document found it is
**not** a universal chokepoint: five other tools (`checkov_runner.py`, `conftest_runner.py`,
`trivy_runner.py`, `tfsec_runner.py`, `infracost_runner.py`) each shell out via their own
independent `asyncio.create_subprocess_exec` call with fixed, hardcoded argv for entirely
different binaries, bypassing `run_command` entirely. This is currently benign — none of the five
can construct a `terraform apply/destroy/import` invocation, since their argv never names the
`terraform`/`tofu` binary at all — but it means "every new agent goes through the same runner
wrapper" is a **new, forward-looking requirement**, not a restatement of an already-universal
fact. `validation_agent.py` additionally runs a second, independent, content-level tripwire
(`_assert_no_mutating_commands`) that regex-scans generated HCL for a `command =
"...terraform (apply|destroy|import)..."` provisioner string before it's ever written to a
sandbox — worth keeping in mind as a second, complementary layer, not a substitute for the argv
check.

### Decision

Written directly into `CLAUDE.md` as its own section (see the actual edit to that file,
made alongside this document): any new agent that shells out to the `terraform`/`tofu` binary
(`drift_reconciliation_agent`, if it ever needs to — Part A's design does not require it to, but a
future revision might) **must** call `TerraformRunner.run_command`, never construct its own
`asyncio.create_subprocess_exec` call. Any new agent that talks to a non-Terraform tool or AWS API
(a Cost Explorer client, for instance) doesn't need `run_command` itself — wrong binary — but
carries the same underlying obligation: read-only APIs only (`Get*`/`Describe*`/`List*`), and it
must never become capable of mutating AWS or invoking `terraform apply/destroy/import` by any
path.
